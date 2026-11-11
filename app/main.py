"""Main engine — multi-strategy with independent firing + daily loss gate.

LIVE strategies (real orders, all fire independently):
  1. (ORB_5MIN now runs as PAPER only — see paper_strategies; it places no real orders)
  2. PREV_DAY_BREAKOUT       — previous day H/L breakout, RR=settings.RR_TARGET
  3. PREV_WEEK_BREAKOUT_LIVE — previous week H/L breakout, RR=1.5

  FVG_RETEST removed 2026-09-22: 12-month backtest showed it claimed 56.6% of
  daily trade slots (MAX_TRADES_PER_DAY=1) ahead of PREV_WEEK_BREAKOUT, which
  is a stronger strategy when not starved (+0.333 avg R freed vs -0.318
  starved). Removing FVG flipped combined 12mo return -1.94% -> +5.52% and
  cut max drawdown 43.2% -> 28.2%. See data/backtest_12m_report.txt.

ENTRY TIME CUTOFF (PREV_DAY_BREAKOUT ONLY):
  - No NEW PREV_DAY_BREAKOUT entries at/after settings.PREV_DAY_NO_ENTRY_AFTER_UTC_HOUR
    (UTC, same clock the strategies use internally). ORB_5MIN and PREV_WEEK_BREAKOUT
    are NOT time-gated — ORB in particular legitimately enters in
    the afternoon UTC window. Already-open trades run to their SL/TP untouched.

DAILY LOSS GATE:
  - First trade of the day: allowed
  - After a WIN: next trade allowed (no cap on wins)
  - After a LOSS: one recovery trade allowed
  - If recovery also loses: blocked until next day
  - Any WIN resets the recovery slot

MULTI-SYMBOL (TRADE_SYMBOLS, see app/symbols.py):
  - Every symbol in TRADE_SYMBOLS is watched on its own M1 chart with its own
    copy of each live strategy; whichever symbol's setup triggers is traded.
  - Per symbol: the daily loss gate above, MAX_TRADES_PER_DAY (its own
    <SYM>_MAX_TRADES_PER_DAY) and MAX_CONCURRENT_TRADES open positions.
  - Account-wide (shared): daily/weekly loss caps, consecutive-loss cool-down,
    kill switch, FTMO guard.
  - Paper strategies (ORB, TWK) run on PRIMARY_SYMBOL only.
  - With TRADE_SYMBOLS unset, only PRIMARY_SYMBOL trades and every decision
    is made exactly as in the single-symbol engine.
"""
from __future__ import annotations

import os
import signal
import threading
import time as time_mod
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Dict, List, Optional
from app.strategy.twk_momentum import TWKMomentumStrategy
import uvicorn
from loguru import logger

from app.api.server import app as fastapi_app
from app.api.state import engine_state
from app.config import settings
from app.core.types import Bar, Side
from app.db.repo import init_db, close_trade_by_ticket
from app.execution.base import Broker
from app.execution.paper_broker import PaperBroker
from app.monitoring.logging_setup import setup_logging
from app.notify.telegram import Telegram
from app.strategy.broker_time import confirmed_broker_offset, detect_broker_offset_hours, has_confirmed_broker_offset, to_utc
from app.strategy.prev_day_breakout import PrevDayBreakoutStrategy
from app.strategy.prev_week_breakout import PrevWeekBreakoutStrategy
from app.strategy.orb_5min import ORB5MinStrategy
from app.strategy.risk import RiskManager
from app.strategy.ftmo_guard import FTMOGuard, FTMOConfig
from app.symbols import load_symbol_configs
from app import briefing
from app.strategy.sizing import order_block_reason
from app.strategy.news_filter import usd_high_impact_events


KILL_FILE = "./KILL"


@dataclass
class _SymbolState:
    """Per-symbol engine state (each symbol has its own chart, day and gate)."""
    last_bar_ts: Optional[datetime] = None
    current_day: str = ""
    last_outcome_today: Optional[str] = None   # 'WIN' | 'LOSS' | None
    recovery_trade_taken: bool = False         # one-shot after a loss
    trades_today: int = 0


def _make_broker() -> Broker:
    if settings.MODE == "live":
        from app.execution.mt5_broker import MT5Broker
        return MT5Broker()
    return PaperBroker(starting_equity=settings.STARTING_EQUITY)


def _run_api() -> None:
    try:
        uvicorn.run(fastapi_app, host=settings.API_HOST,
                    port=settings.API_PORT, log_level="warning", access_log=False)
    except Exception as e:
        logger.warning(f"API server failed: {e}")


def _is_paper(strategy) -> bool:
    """True if this strategy handles its own paper simulation (no real orders)."""
    return getattr(strategy, 'paper', False)


class Engine:
    def __init__(self, broker: Broker, notifier: Telegram):
        self.broker   = broker
        self.notifier = notifier

        # ── Symbols + strategy registry ────────────────────────────────────
        # One copy of each live strategy per traded symbol (TRADE_SYMBOLS).
        # The primary symbol's defaults are the global settings, so with
        # TRADE_SYMBOLS unset this is exactly the original gold-only registry:
        # PREV_DAY (rr=RR_TARGET) then PREV_WEEK (rr=1.5), in that priority.
        self.symbol_cfgs = load_symbol_configs()
        self._cfg = {c.symbol: c for c in self.symbol_cfgs}
        self.symbols = [c.symbol for c in self.symbol_cfgs]
        self._multi = len(self.symbols) > 1
        self._live_by_symbol: Dict[str, list] = {}
        for c in self.symbol_cfgs:
            strategies = []
            if "PREV_DAY" in c.strategies:
                strategies.append(PrevDayBreakoutStrategy(rr=c.rr_prev_day, risk_pct=c.risk_per_trade,
                                                          symbol=c.symbol, price_scale=c.price_scale))
            if "PREV_WEEK" in c.strategies:
                strategies.append(PrevWeekBreakoutStrategy(rr=c.rr_prev_week, risk_pct=c.risk_per_trade, paper=False,
                                                           symbol=c.symbol, price_scale=c.price_scale))
            self._live_by_symbol[c.symbol] = strategies
        self.live_strategies = [s for sym in self.symbols for s in self._live_by_symbol[sym]]

        self.paper_strategies = [
            TWKMomentumStrategy(rr=1.5, risk_pct=settings.RISK_PER_TRADE, paper=True),
            # ORB_5MIN moved from live to paper. A 3-year DST-aware engine backtest
            # (Feb 2024 - Sep 2026) had it at -0.077R/trade over 322 trades inside the
            # live mix, and the book without it did better (+22R vs -5R). That
            # difference is suggestive, not statistically conclusive, so it keeps
            # running here as paper (logs/orb5min_paper.jsonl) for monitoring instead
            # of being deleted.
            ORB5MinStrategy(rr=settings.RR_TARGET, risk_pct=settings.RISK_PER_TRADE, paper=True),
        ]

        # Inject broker into strategies that need MT5 history
        for s in self.live_strategies + self.paper_strategies:
            if hasattr(s, "set_broker"):
                s.set_broker(broker)

        # Same broker-time detection the strategies use internally (broker_time.py),
        # so the PREV_DAY_BREAKOUT entry-time cutoff below reads the identical
        # clock they already gate their own session windows on. Not a new/second
        # conversion.
        self._broker_offset = detect_broker_offset_hours(broker)
        # A strategy whose own symbol had no fresh tick at startup (gold at the
        # weekend) fell back to the configured default; if any symbol confirmed
        # the real offset, give it to everyone (one server clock for all).
        self._apply_confirmed_offset()
        self._offset_checked_at: Optional[datetime] = None   # last hourly re-check (UTC)

        # Account-wide caps = totals across symbols (identical to the settings
        # values when only the primary symbol trades); each symbol's own cap is
        # enforced in _on_bar so one symbol can't use up another's slot.
        self.risk   = RiskManager(
            starting_equity=broker.account_equity() or settings.STARTING_EQUITY,
            max_trades_per_day=sum(c.max_trades_per_day for c in self.symbol_cfgs),
            max_concurrent_trades=settings.MAX_CONCURRENT_TRADES * len(self.symbols),
        )
        self.symbol = settings.PRIMARY_SYMBOL.upper()        # primary: paper strategies + legacy callers

        self._sym: Dict[str, _SymbolState] = {s: _SymbolState() for s in self.symbols}
        self._risk_day = ""                                  # account-level day roll happens once per day
        self._briefing_day = None                            # New York date of the last pre-session briefing
        self._open_positions: Dict[str, dict] = {}           # strategy_name (unique per symbol) → meta

        self._stop   = threading.Event()
        self._errors = 0

        engine_state.broker  = broker
        engine_state.risk    = self.risk
        engine_state.running = True

        # ── FTMO prop-challenge guard (active only when FTMO_MODE is set) ──
        # When enabled via .env, enforces equity-based daily loss, static
        # overall drawdown, profit-target stop, and min-trading-days — all
        # with conservative buffers BELOW the FTMO hard limits. Disabled by
        # default so the normal demo/live behaviour is byte-for-byte unchanged.
        self.ftmo = None
        if getattr(settings, "FTMO_MODE", False):
            init_bal = self.broker.account_balance() or self.broker.account_equity() \
                       or settings.STARTING_EQUITY
            self.ftmo = FTMOGuard(FTMOConfig(
                initial_balance=float(init_bal),
                daily_loss_pct=getattr(settings, "FTMO_DAILY_LOSS_PCT", 0.05),
                max_loss_pct=getattr(settings, "FTMO_MAX_LOSS_PCT", 0.10),
                profit_target_pct=getattr(settings, "FTMO_PROFIT_TARGET_PCT", 0.05),
                min_trading_days=getattr(settings, "FTMO_MIN_TRADING_DAYS", 2),
                daily_buffer_pct=getattr(settings, "FTMO_DAILY_BUFFER_PCT", 0.04),
                max_buffer_pct=getattr(settings, "FTMO_MAX_BUFFER_PCT", 0.08),
            ))

    @property
    def _all_strategies(self):
        return self.live_strategies + self.paper_strategies

    def start(self) -> None:
        live_names  = ", ".join(s.strategy_name for s in self.live_strategies)
        paper_names = ", ".join(s.strategy_name for s in self.paper_strategies) or "(none)"
        self.notifier.send(
            f"🟢 ORB Bot v2 — *{settings.MODE}*\n"
            f"📊 Live: {live_names}\n"
            f"📋 Paper: {paper_names}\n"
            f"Equity: ${self.broker.account_equity():,.2f}"
        )
        logger.info(f"Engine starting. Mode={settings.MODE}")
        logger.info(f"Live strategies: {live_names}")
        logger.info(f"Paper strategies: {paper_names}")
        if self._multi:
            for c in self.symbol_cfgs:
                logger.info(f"Symbol {c.symbol}: strategies={','.join(c.strategies)} price_scale={c.price_scale} "
                            f"risk={c.risk_per_trade} rr_pd={c.rr_prev_day} rr_pw={c.rr_prev_week} "
                            f"max_trades/day={c.max_trades_per_day}")
        logger.info(f"Order guard: refuse any order losing > {settings.MAX_TRADE_RISK_PCT * 100:.1f}% of equity at its stop, "
                    f"lot caps " + ", ".join(f"{c.symbol}={c.max_lots:.2f}" for c in self.symbol_cfgs))
        if self.ftmo is not None:
            logger.info(f"FTMO GUARD ARMED — initial=${self.ftmo.cfg.initial_balance:,.0f} "
                        f"daily_stop={self.ftmo.cfg.daily_buffer_pct*100:.0f}% "
                        f"overall_stop={self.ftmo.cfg.max_buffer_pct*100:.0f}% "
                        f"target={self.ftmo.cfg.profit_target_pct*100:.0f}%")
            self.notifier.send(f"🛡️ FTMO guard armed: ${self.ftmo.cfg.initial_balance:,.0f} "
                               f"account, stop at {self.ftmo.cfg.daily_buffer_pct*100:.0f}% daily / "
                               f"{self.ftmo.cfg.max_buffer_pct*100:.0f}% overall")
        self._ensure_symbol()

        while not self._stop.is_set():
            try:
                self._tick()
                self._errors = 0
                time_mod.sleep(1.0)
            except KeyboardInterrupt:
                break
            except Exception as e:
                self._errors += 1
                logger.exception(f"Engine error ({self._errors}): {e}")
                if self._errors >= 10:
                    self.risk.pause(f"errors x{self._errors}: {e}")
                    self.notifier.send(f"🔴 Bot paused: {self._errors} errors\n{e}")
                time_mod.sleep(min(30, 2 ** min(self._errors, 5)))

        self._shutdown()

    def _shutdown(self) -> None:
        self._stop.set()
        engine_state.running = False
        try: self.broker.disconnect()
        except Exception: pass
        self.notifier.send("🛑 ORB Bot stopped.")
        logger.info("Shutdown complete.")

    def _ensure_symbol(self) -> None:
        for sym in self.symbols:
            try:
                info = self.broker.symbol_info(sym)
                logger.info(f"Symbol {sym}: point={info['point']}, spread={info['spread_points']}")
            except Exception as e:
                logger.error(f"Symbol {sym} not available: {e}")

    def _can_open_new_trade(self, symbol: Optional[str] = None) -> bool:
        """
        Trading gate logic (per symbol):
        - No trades yet today          → allow
        - Last trade was WIN           → allow
        - Last trade was LOSS          → allow one recovery trade only
        - Recovery trade already taken → block for the rest of the day
        """
        st = self._sym[symbol or self.symbol]
        if st.last_outcome_today is None:
            return True
        if st.last_outcome_today == "WIN":
            return True
        # last outcome was LOSS
        return not st.recovery_trade_taken

    def _open_count(self, symbol: str) -> int:
        return sum(1 for m in self._open_positions.values() if m.get("symbol", self.symbol) == symbol)

    def _tick(self) -> None:
        if os.path.exists(KILL_FILE):
            if not self.risk.state.paused:
                self.risk.pause("kill switch")
                self.notifier.send("🔴 Kill switch activated")
            return

        # Each symbol has its own chart: process every symbol that has a new
        # closed M1 bar. Symbols are independent; one without data is skipped.
        for sym in self.symbols:
            bars = self.broker.get_bars(sym, "M1", 3)
            if len(bars) < 2: continue
            last = bars[-2]
            st = self._sym[sym]
            if st.last_bar_ts is not None and last.ts <= st.last_bar_ts: continue
            st.last_bar_ts = last.ts
            self._on_bar(sym, last)

    def _on_bar(self, sym: str, last: Bar) -> None:
        st = self._sym[sym]
        day = last.ts.strftime("%Y-%m-%d")
        if day != st.current_day:
            st.current_day = day
            st.last_outcome_today = None
            st.recovery_trade_taken = False
            st.trades_today = 0
            # Account-level roll exactly once per broker day, by whichever
            # symbol reaches the new day first (a second roll would wipe the
            # day's trade count and starting equity).
            if day != self._risk_day:
                self._risk_day = day
                eq = self.broker.account_equity()
                self.risk.on_new_day(eq)
                logger.info(f"New day: {day}, equity=${eq:,.2f}")

        # Not gated on freshness: after a broker DST change the bars read an hour off
        # and would look stale forever. Detection itself ignores stale data safely.
        self._recheck_offset(sym)
        if sym == self.symbol and self._is_fresh(last):      # never brief off a stale (weekend) bar
            self._maybe_send_briefing(last)

        # ── FTMO guard: update on every tick with live equity/balance ──
        if self.ftmo is not None:
            try:
                _eq = self.broker.account_equity()
                _bal = self.broker.account_balance()
            except Exception as e:
                logger.warning(f"[FTMO] equity/balance read failed: {e}")
                _eq, _bal = None, None
            self.ftmo.on_tick(last.ts.replace(tzinfo=timezone.utc)
                              if last.ts.tzinfo is None else last.ts,
                              _eq, _bal)
            # If a breach/target tripped, force-close any open live positions now
            can_trade_ftmo, _reason = self.ftmo.can_open_trade()
            if not can_trade_ftmo and self._open_positions:
                logger.warning(f"[FTMO] {_reason} — force-closing open positions")
                for _sn in list(self._open_positions.keys()):
                    self._force_close(_sn, last)
                self.notifier.send(f"🛡️ FTMO guard: {_reason}\nClosed open positions.")

        # ── Process this symbol's open live positions (EOD close, fill checks) ──
        for sn in [k for k, m in self._open_positions.items() if m.get("symbol", self.symbol) == sym]:
            strategy = self._find_strategy(sn)
            if strategy is None: continue

            if strategy.should_close_eod(last):
                self._force_close(sn, last)
                continue

            self._check_closed(sn)

            if isinstance(self.broker, PaperBroker):
                closed = self.broker.on_new_bar(sym, last)
                for ticket, pnl in closed:
                    if ticket == self._open_positions.get(sn, {}).get("ticket"):
                        self._record_close(sn, ticket, pnl, last)

        # ── Run paper strategies independently (always; primary symbol only) ──
        if sym == self.symbol:
            for strategy in self.paper_strategies:
                strategy.on_bar(last, sym)  # handles its own sim internally

        # ── Risk gate (account-wide) ──────────────────────────────────────
        if self.risk.state.paused: return
        ok, _ = self.risk.can_trade(last.ts)
        if not ok: return

        # ── Run live strategies independently (with daily loss gate) ───────
        # ── FTMO trade gate (only when guard active) ──
        if self.ftmo is not None:
            ok_ftmo, reason = self.ftmo.can_open_trade()
            if not ok_ftmo:
                logger.info(f"[FTMO] trading blocked: {reason}")
                return

        # ── Per-symbol caps (multi-symbol only; single-symbol relies on the
        #    RiskManager checks above, exactly as before) ─────────────────────
        if self._multi:
            if st.trades_today >= self._cfg[sym].max_trades_per_day: return
            if self._open_count(sym) >= settings.MAX_CONCURRENT_TRADES: return

        for strategy in self._live_by_symbol[sym]:
            sn = strategy.strategy_name

            if sn in self._open_positions: continue

            if not self._can_open_new_trade(sym):
                logger.info(f"[{sn}] Skipped — daily loss gate active")
                continue

            # ── Time-of-day entry cutoff — PREV_DAY_BREAKOUT only ──────────
            # ENTRY filter only; positions already open are handled above and
            # left to hit their SL/TP normally. Other strategies (ORB_5MIN,
            # PREV_WEEK_BREAKOUT, FVG_RETEST) are unaffected and still get
            # evaluated this tick. Matches every symbol's PREV_DAY copy
            # (e.g. PREV_DAY_BREAKOUT_BTCUSD).
            if sn.startswith("PREV_DAY_BREAKOUT"):
                if not has_confirmed_broker_offset():
                    # No offset has ever been confirmed from live tick/bar data
                    # this session — self._broker_offset is only a configured
                    # guess. Fail OPEN (don't enforce the cutoff) rather than
                    # silently gating every entry on a guess for the rest of
                    # the session; this is exactly the failure mode that muted
                    # PREV_DAY_BREAKOUT entirely last time.
                    logger.error(
                        f"[TIME_FILTER] [{sn}] No confirmed broker offset yet "
                        f"(using unconfirmed fallback {self._broker_offset:+.1f}) "
                        f"— NOT enforcing time cutoff this tick, failing open for safety review"
                    )
                else:
                    utc_ts = to_utc(last.ts, self._broker_offset)
                    cutoff = time(settings.PREV_DAY_NO_ENTRY_AFTER_UTC_HOUR, 0)
                    if utc_ts.time() >= cutoff:
                        logger.info(
                            f"[TIME_FILTER] [{sn}] Skipping entry — "
                            f"{utc_ts.strftime('%H:%M')} >= cutoff {cutoff.strftime('%H:%M')}"
                        )
                        continue

            sig = strategy.on_bar(last, sym)

            if sig is not None:
                self._execute(strategy, sig)

    # ── Broker clock: one server clock shared by every symbol ──────────────
    OFFSET_RECHECK = timedelta(hours=1)
    FRESH_BAR = timedelta(minutes=10)

    def _now(self) -> datetime:
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _is_fresh(self, last: Bar) -> bool:
        """True if this closed bar is live market data (not a weekend/holiday leftover)."""
        return abs(self._now() - to_utc(last.ts, self._broker_offset)) <= self.FRESH_BAR

    def _apply_confirmed_offset(self) -> None:
        off = confirmed_broker_offset()
        if off is None:
            return
        changed = [s.strategy_name for s in self.live_strategies + self.paper_strategies
                   if getattr(s, "_broker_offset", None) != off]
        for s in self.live_strategies + self.paper_strategies:
            s._broker_offset = off
        if changed or self._broker_offset != off:
            logger.info(f"[broker_time] applying confirmed offset {off:+.1f} to engine and {len(changed)} "
                        f"strategies ({', '.join(changed) or 'none'})")
        self._broker_offset = off

    def _recheck_offset(self, sym: str) -> None:
        """Hourly, from a symbol that is trading live: re-detect the offset and apply it
        everywhere. Covers a start with nothing confirmed and DST changes mid-session."""
        now = self._now()
        if self._offset_checked_at is not None and now - self._offset_checked_at < self.OFFSET_RECHECK:
            return
        self._offset_checked_at = now
        try:
            detect_broker_offset_hours(self.broker, sym)
            self._apply_confirmed_offset()
        except Exception as e:
            logger.warning(f"[broker_time] hourly re-check failed: {e}")

    def _maybe_send_briefing(self, last: Bar) -> None:
        """Once per weekday at BRIEFING_HOUR_ET (New York): today's USD releases, weekly
        bias and key levels for every traded symbol. Context only; never places trades
        and never lets a failure reach the trading loop."""
        if not settings.BRIEFING_ENABLED:
            return
        try:
            et = briefing.to_et(to_utc(last.ts, self._broker_offset))
            if et.weekday() >= 5 or et.time() < time(settings.BRIEFING_HOUR_ET, 0) or self._briefing_day == et.date():
                return
            self._briefing_day = et.date()
            snaps = [briefing.symbol_snapshot(s, self.broker.get_bars(s, "H1", 200), self.broker.get_bars(s, "D1", 20))
                     for s in self.symbols]
            text = briefing.format_briefing(et, usd_high_impact_events(et.date()), snaps)
            self.notifier.send(text)
            logger.info("[BRIEFING]\n" + text)
        except Exception as e:
            logger.warning(f"[BRIEFING] skipped: {e}")

    def _find_strategy(self, name: str):
        for s in self._all_strategies:
            if s.strategy_name == name:
                return s
        return None

    def _execute(self, strategy, sig) -> None:
        sym  = getattr(strategy, "symbol", self.symbol)
        info = self.broker.symbol_info(sym)
        lots = strategy.compute_lots(
            equity=self.broker.account_equity(),
            risk_dist=sig.risk_dist, point=info["point"],
            tick_value=info["tick_value"],
            min_lot=info.get("min_lot", 0.01), max_lot=info.get("max_lot", 1.0),
            lot_step=info.get("lot_step", 0.01),
        )
        if lots < info.get("min_lot", 0.01):
            logger.warning(f"[{sig.strategy}] lots too small: {lots}")
            return

        # Last-line order guard: with high account leverage the broker accepts
        # almost any size, so refuse anything over the lot cap or the max loss.
        cfg = self._cfg.get(sym)
        why = order_block_reason(
            lots, self.broker.account_equity(), sig.risk_dist, info["point"], info["tick_value"],
            max_lots=cfg.max_lots if cfg else settings.MAX_LOTS, max_risk_pct=settings.MAX_TRADE_RISK_PCT)
        if why:
            logger.error(f"[{sig.strategy}] ORDER BLOCKED by risk guard: {why}")
            self.notifier.send(f"🛑 [{sig.strategy}] {sym} order blocked: {why}")
            return

        # ── FTMO projected-risk check: would this trade's worst-case stop
        #    breach a daily/overall floor? If so, skip it (only when armed). ──
        if self.ftmo is not None:
            try:
                _eq = self.broker.account_equity()
                _trade_risk = (sig.risk_dist / info["point"]) * info["tick_value"] * lots
                ok_risk, why = self.ftmo.trade_risk_ok(_eq, _trade_risk)
                if not ok_risk:
                    logger.warning(f"[{sig.strategy}] FTMO skip: {why}")
                    return
            except Exception as e:
                logger.warning(f"[{sig.strategy}] FTMO risk check failed, skipping trade: {e}")
                return

        pos = self.broker.place_market(
            sym, sig.side, lots, sig.sl, sig.tp,
            comment=f"{sig.strategy[:8]}_{sig.side.value}",
        )
        if pos is None or pos.ticket is None:
            logger.error(f"[{sig.strategy}] Order rejected")
            self.notifier.send(f"⚠️ [{sig.strategy}] Order rejected")
            return

        # Re-anchor SL/TP to the ACTUAL fill price. Market orders can fill
        # past the intended entry on fast breakouts; if we keep the SL/TP
        # computed from the intended entry, the realized RR is distorted
        # (wins book partial-R while losses are full-R). Recompute both
        # stops from pos.entry, preserving the strategy's intended risk_dist.
        try:
            slippage = abs(pos.entry - sig.entry)
            # Sanity bound: real XAUUSD slippage is at most a few dollars. A
            # "slippage" larger than half the stop distance means the fill price
            # is implausible (e.g. a 0.0 that slipped through) — never re-anchor
            # on that, or we compute stops off a garbage entry (10016 Invalid).
            max_sane_slip = max(sig.risk_dist * 0.5, info["point"] * 50)
            if pos.entry <= 0.0 or slippage > max_sane_slip:
                logger.warning(
                    f"[{sig.strategy}] Skipping re-anchor: implausible fill "
                    f"(entry={pos.entry:.2f} intended={sig.entry:.2f} "
                    f"slip=${slippage:.2f} max=${max_sane_slip:.2f}). "
                    f"Keeping broker's original SL/TP."
                )
            elif slippage > info["point"] * 2 and hasattr(self.broker, "modify_sl_tp"):
                if sig.side == Side.BUY:
                    new_sl = pos.entry - sig.risk_dist
                    new_tp = pos.entry + sig.risk_dist * strategy.rr
                else:
                    new_sl = pos.entry + sig.risk_dist
                    new_tp = pos.entry - sig.risk_dist * strategy.rr
                if self.broker.modify_sl_tp(pos.ticket, new_sl, new_tp):
                    logger.info(f"[{sig.strategy}] Re-anchored after ${slippage:.2f} "
                                f"slippage: entry={pos.entry:.2f} SL={new_sl:.2f} TP={new_tp:.2f}")
                    sig.sl = new_sl
                    sig.tp = new_tp
        except Exception as e:
            logger.warning(f"[{sig.strategy}] SL/TP re-anchor failed: {e}")

        risk_ccy = (sig.risk_dist / info["point"]) * info["tick_value"] * lots
        self._open_positions[sig.strategy] = {
            "ticket": pos.ticket, "signal": sig, "lots": lots,
            "entry": pos.entry, "risk_ccy": max(risk_ccy, 1e-9),
            "symbol": sym,
        }
        st = self._sym[sym]
        st.trades_today += 1
        if st.last_outcome_today == "LOSS":
            st.recovery_trade_taken = True
            logger.info(f"[{sig.strategy}] Recovery trade opened after today's loss")
        self.risk.record_trade_open()
        if self.ftmo is not None:
            self.ftmo.record_trade_day()

        self.notifier.send(
            f"📥 *[{sig.strategy}]* {sig.side.value} `{sym}` {lots} lots\n"
            f"Entry: `{pos.entry:.2f}` SL: `{sig.sl:.2f}` TP: `{sig.tp:.2f}`\n"
            f"Risk: `${risk_ccy:.2f}` | {sig.note}"
        )
        logger.info(
            f"[{sig.strategy}] ENTRY {sig.side.value} {lots} lots @ {pos.entry:.2f} "
            f"SL={sig.sl:.2f} TP={sig.tp:.2f} risk=${risk_ccy:.2f} | {sig.note}"
        )

    def _check_closed(self, sn: str) -> None:
        meta = self._open_positions.get(sn)
        if not meta: return
        sym = meta.get("symbol", self.symbol)
        if meta["ticket"] not in {p.ticket for p in self.broker.open_positions(sym)}:
            # Source of truth: realized P/L from broker deal history. Only if
            # that is unavailable do we fall back to recomputing from the entry
            # and current tick — that fallback is an APPROXIMATION and was the
            # source of the bogus -271R figures when entry was 0.0, so it is a
            # last resort, not the primary path.
            pnl = self.broker.realized_pnl(meta["ticket"])
            if pnl is None:
                sig  = meta["signal"]
                info = self.broker.symbol_info(sym)
                try:
                    tick = self.broker.tick(sym)
                    ep   = tick["bid"] if sig.side == Side.BUY else tick["ask"]
                    pts  = (ep - meta["entry"]) / info["point"]
                    if sig.side == Side.SELL: pts = -pts
                    pnl  = pts * info["tick_value"] * meta["lots"]
                    logger.warning(f"[{sn}] realized_pnl unavailable, approximated "
                                   f"close P/L from tick: ${pnl:+.2f}")
                except Exception:
                    pnl = 0.0
            self._record_close(sn, meta["ticket"], pnl, None)

    def _force_close(self, sn: str, bar: Bar) -> None:
        meta = self._open_positions.get(sn)
        if not meta: return
        logger.info(f"[{sn}] Force closing #{meta['ticket']} at EOD")
        pnl = self.broker.close(meta["ticket"])
        # Prefer the realized figure from deal history (includes the closing
        # deal's commission/swap); broker.close() may return only running
        # profit at the close instant. Fall back to that if history is unread.
        realized = self.broker.realized_pnl(meta["ticket"])
        if realized is not None:
            pnl = realized
        self._record_close(sn, meta["ticket"], pnl, bar)

    def _record_close(self, sn: str, ticket: int, pnl: float, bar: Optional[Bar]) -> None:
        meta = self._open_positions.pop(sn, None)
        if not meta: return
        r       = pnl / meta.get("risk_ccy", 1e-9)
        outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BE")
        emoji   = "✅" if pnl > 0 else ("❌" if pnl < 0 else "➖")
        st = self._sym[meta.get("symbol", self.symbol)]
        st.last_outcome_today = "WIN" if pnl >= 0 else "LOSS"
        if st.last_outcome_today == "WIN":
            st.recovery_trade_taken = False  # WIN resets recovery slot
        self.risk.record_trade_close(pnl)
        eq = self.broker.account_equity()
        # Authoritatively mirror the broker's real equity into the RiskManager
        # so a wrong per-trade P/L can never corrupt the daily/weekly loss caps.
        self.risk.sync_equity(eq)
        self.notifier.send(
            f"{emoji} *[{sn}] {outcome}* pnl=`${pnl:+.2f}` (`{r:+.2f}R`)\n"
            f"Equity: `${eq:,.2f}`"
        )
        logger.info(f"[{sn}] CLOSE #{ticket} {outcome} pnl=${pnl:+.2f} "
                    f"({r:+.2f}R) equity=${eq:,.2f}")
        try:
            now = bar.ts if bar else datetime.now(timezone.utc)
            close_trade_by_ticket(ticket, meta.get("entry", 0), now, pnl, r)
        except Exception as e:
            logger.warning(f"DB log failed: {e}")

    def request_stop(self) -> None:
        self._stop.set()


def main() -> None:
    setup_logging()
    init_db()
    logger.info(f"Starting ORB Bot v2 — MODE={settings.MODE}")

    broker = _make_broker()
    if not broker.connect():
        logger.error("Broker connection failed.")
        return

    notifier = Telegram()
    engine   = Engine(broker, notifier)

    threading.Thread(target=_run_api, daemon=True, name="api").start()

    def _handle_sig(signum, frame):
        logger.info(f"Signal {signum} received, stopping...")
        engine.request_stop()
    signal.signal(signal.SIGINT, _handle_sig)
    try: signal.signal(signal.SIGTERM, _handle_sig)
    except (AttributeError, ValueError): pass

    engine.start()


if __name__ == "__main__":
    main()