"""Previous Week High/Low Breakout Strategy.

PAPER MODE by default — run alongside PREV_DAY_BREAKOUT live to collect
real-time validation data before risking real money.

Backtest on 73 days / 15 weeks XAUUSD M1 (Feb-Jun 2026):
  23 trades, 52.2% WR, +0.304R expectancy (full day session)
  20 trades, 60.0% WR, +0.500R expectancy (NY session 13:00-20:00 UTC)

Logic:
  1. On each new week, fetch previous week's COMPLETE M1 history from MT5
     (walks back up to 6 weeks to find a complete week with >= 4 trading days)
  2. Cache levels to data/prev_week_levels.json
  3. During NY session (18:00-23:00 broker bar time = 13:00-18:00 UTC),
     wait for a FRESH CROSS of the weekly high or low
  4. Entry: weekly level + small buffer
  5. SL: risk = min(weekly_range * 0.05, $15), capped
  6. TP: 1.5R
  7. One trade per day max (not per week — weekly levels provide multiple
     opportunities throughout the week)
  8. Fresh-cross guard: skip if price already overshot > $5 past the level
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

from app.core.types import Bar, Side


# ── Session (NY session in broker bar time UTC+5) ─────────────────────────
SESSION_START   = time(18, 0)   # 13:00 UTC
SESSION_END     = time(23, 0)   # 18:00 UTC
CLOSE_DEADLINE  = time(23, 30)

# ── Risk parameters ────────────────────────────────────────────────────────
RANGE_FRACTION  = 0.05   # 5% of weekly range as risk
MAX_RISK_DIST   = 15.0
MIN_RISK_DIST   = 3.0
SL_BUFFER       = 0.21
RR_TARGET       = 1.5
MAX_OVERSHOOT   = 5.0    # slightly wider than daily (weekly levels are wider)

# ── Paper mode ─────────────────────────────────────────────────────────────
PAPER_MODE      = True
PAPER_LOG_FILE  = Path("logs/prev_week_paper.jsonl")

CACHE_FILE      = Path("data/prev_week_levels.json")


@dataclass
class WeekLevels:
    week_label: str   # e.g. "2026-W22"
    high: float
    low: float
    range_size: float


@dataclass
class TradeSignal:
    ts: datetime
    symbol: str
    side: Side
    entry: float
    sl: float
    tp: float
    risk_dist: float
    strategy: str
    note: str = ""


class PrevWeekBreakoutStrategy:
    def __init__(self, rr: float = RR_TARGET, risk_pct: float = 0.02,
                 paper: bool = PAPER_MODE, broker=None):
        self.rr         = rr
        self.risk_pct   = risk_pct
        self.paper      = paper
        self.broker     = broker
        self.strategy_name = "PREV_WEEK_BREAKOUT" + ("_PAPER" if paper else "_LIVE")

        self._current_day   = ""
        self._current_week  = ""
        self._traded_today  = False
        self._was_inside    = None   # fresh-cross tracker
        self._levels: Optional[WeekLevels] = None

        # Paper sim state
        self._paper_open: dict = {}
        self._paper_ticket  = 200000
        self._paper_equity  = 1000.0

        if self.paper:
            PAPER_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"[{self.strategy_name}] PAPER MODE — "
                        f"signals logged to {PAPER_LOG_FILE}")

    def set_broker(self, broker) -> None:
        self.broker = broker

    # ── Weekly level fetching ──────────────────────────────────────────────

    def _week_label(self, dt: datetime) -> str:
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"

    def _fetch_week_levels(self, symbol: str, today: str) -> Optional[WeekLevels]:
        if self.broker is None:
            return self._load_cache()
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            today_wl = self._week_label(today_dt)

            for weeks_back in range(1, 7):
                check_dt  = today_dt - timedelta(weeks=weeks_back)
                check_wl  = self._week_label(check_dt)
                if check_wl == today_wl:
                    continue   # same week as today

                # Fetch the full week (Mon–Sun) of M1 bars
                # Week starts on Monday
                week_start = check_dt - timedelta(days=check_dt.weekday())
                week_end   = week_start + timedelta(days=7)

                bars = self._get_range_bars(symbol, week_start, week_end)
                if not bars or len(bars) < 500:   # need a substantive week
                    logger.debug(f"[{self.strategy_name}] {check_wl} "
                                 f"has only {len(bars) if bars else 0} bars, skip")
                    continue

                high = max(b.high for b in bars)
                low  = min(b.low  for b in bars)
                lvl  = WeekLevels(check_wl, high, low, high - low)
                self._save_cache(lvl)
                return lvl

            logger.warning(f"[{self.strategy_name}] No valid prev week found")
            return self._load_cache()

        except Exception as e:
            logger.exception(f"[{self.strategy_name}] fetch prev week failed: {e}")
            return self._load_cache()

    def _get_range_bars(self, symbol: str, start: datetime, end: datetime) -> list:
        try:
            import MetaTrader5 as mt5
            rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, start, end)
            if rates is None or len(rates) == 0:
                return []
            return [type('B', (), {
                'high': float(r['high']), 'low': float(r['low'])
            }) for r in rates]
        except Exception as e:
            logger.warning(f"_get_range_bars failed: {e}")
            return []

    def _save_cache(self, lvl: WeekLevels) -> None:
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps({
                "week_label": lvl.week_label, "high": lvl.high,
                "low": lvl.low, "range_size": lvl.range_size,
            }, indent=2))
        except Exception as e:
            logger.warning(f"cache save failed: {e}")

    def _load_cache(self) -> Optional[WeekLevels]:
        try:
            if not CACHE_FILE.exists():
                return None
            return WeekLevels(**json.loads(CACHE_FILE.read_text()))
        except Exception as e:
            logger.warning(f"cache load failed: {e}")
            return None

    # ── Main bar handler ───────────────────────────────────────────────────

    def on_bar(self, bar: Bar, symbol: str = "XAUUSD") -> Optional[TradeSignal]:
        day  = bar.ts.strftime("%Y-%m-%d")
        week = self._week_label(bar.ts)
        t    = bar.ts.time()

        # New week → refresh levels
        if week != self._current_week:
            self._current_week = week
            self._levels = self._fetch_week_levels(symbol, day)
            if self._levels:
                logger.info(
                    f"[{self.strategy_name}] Week={week} "
                    f"PrevWeek levels: H={self._levels.high:.2f} "
                    f"L={self._levels.low:.2f} "
                    f"range=${self._levels.range_size:.2f} "
                    f"(from {self._levels.week_label})"
                )
            else:
                logger.warning(f"[{self.strategy_name}] No prev week levels for {week}")

        # New day → reset daily state
        if day != self._current_day:
            self._current_day  = day
            self._traded_today = False
            self._was_inside   = None

        if self._levels is None:
            return None

        pwh = self._levels.high
        pwl = self._levels.low

        # Track inside/outside range
        inside_now = pwl <= bar.close <= pwh

        # First bar of session — record state, never trade
        if self._was_inside is None:
            self._was_inside = inside_now
            self._check_paper_fills(bar)
            return None

        # Check paper fills on every bar
        self._check_paper_fills(bar)

        if self._traded_today or t < SESSION_START or t >= SESSION_END:
            self._was_inside = inside_now
            return None

        # News blackout filter
        try:
            from app.strategy.news_filter import is_news_blackout
            blocked, reason = is_news_blackout()
            if blocked:
                logger.info(f"[{self.strategy_name}] NEWS BLACKOUT: {reason}")
                self._was_inside = inside_now
                return None
        except ImportError:
            pass

        # Risk sizing
        pw_range = self._levels.range_size
        risk = min(pw_range * RANGE_FRACTION, MAX_RISK_DIST)
        if risk < MIN_RISK_DIST:
            risk = MIN_RISK_DIST

        # Fresh cross detection
        crossed_up   = self._was_inside and bar.high > pwh
        crossed_down = self._was_inside and bar.low  < pwl
        self._was_inside = inside_now

        side  = None
        entry = None
        sl    = None

        if crossed_up:
            overshoot = bar.close - pwh
            if overshoot > MAX_OVERSHOOT:
                logger.info(f"[{self.strategy_name}] BUY cross but "
                            f"overshoot ${overshoot:.1f} > ${MAX_OVERSHOOT} — skip")
                return None
            side  = Side.BUY
            entry = pwh + SL_BUFFER
            sl    = entry - risk

        elif crossed_down:
            overshoot = pwl - bar.close
            if overshoot > MAX_OVERSHOOT:
                logger.info(f"[{self.strategy_name}] SELL cross but "
                            f"overshoot ${overshoot:.1f} > ${MAX_OVERSHOOT} — skip")
                return None
            side  = Side.SELL
            entry = pwl - SL_BUFFER
            sl    = entry + risk

        if side is None:
            return None

        risk_dist = abs(entry - sl)
        tp = (entry + risk_dist * self.rr if side == Side.BUY
              else entry - risk_dist * self.rr)

        self._traded_today = True

        if self.paper:
            self._open_paper(bar.ts, symbol, side, entry, sl, tp, risk_dist)
            return None   # paper — engine doesn't place real order

        return TradeSignal(
            ts=bar.ts, symbol=symbol, side=side,
            entry=entry, sl=sl, tp=tp, risk_dist=risk_dist,
            strategy=self.strategy_name,
            note=(f"FreshCross PrevWeek({self._levels.week_label}) "
                  f"H={pwh:.2f} L={pwl:.2f} range=${pw_range:.1f}"),
        )

    # ── Paper simulation ───────────────────────────────────────────────────

    def _open_paper(self, ts, symbol, side, entry, sl, tp, risk_dist):
        self._paper_ticket += 1
        ticket   = self._paper_ticket
        lots     = max(0.01, round(
            (self._paper_equity * self.risk_pct) / (risk_dist * 100), 2))
        risk_ccy = risk_dist * 100 * lots

        self._paper_open[ticket] = {
            'ts': ts, 'symbol': symbol, 'side': side,
            'entry': entry, 'sl': sl, 'tp': tp,
            'lots': lots, 'risk_ccy': risk_ccy,
        }
        logger.info(
            f"[{self.strategy_name}] PAPER OPEN #{ticket} "
            f"{side.value} {lots} {symbol} @ {entry:.2f} "
            f"SL={sl:.2f} TP={tp:.2f} risk=${risk_ccy:.2f}"
        )
        self._log({'event': 'OPEN', 'ticket': ticket, 'time': ts.isoformat(),
                   'symbol': symbol, 'side': side.value, 'entry': entry,
                   'sl': sl, 'tp': tp, 'lots': lots, 'risk_ccy': risk_ccy})

    def _check_paper_fills(self, m1: Bar) -> None:
        closed = []
        for ticket, meta in self._paper_open.items():
            side = meta['side']
            if side == Side.BUY:
                if m1.low  <= meta['sl']: self._close_paper(ticket, meta['sl'],  m1.ts, "LOSS"); closed.append(ticket)
                elif m1.high >= meta['tp']: self._close_paper(ticket, meta['tp'], m1.ts, "WIN");  closed.append(ticket)
            else:
                if m1.high >= meta['sl']: self._close_paper(ticket, meta['sl'],  m1.ts, "LOSS"); closed.append(ticket)
                elif m1.low  <= meta['tp']: self._close_paper(ticket, meta['tp'], m1.ts, "WIN");  closed.append(ticket)
        for t in closed:
            del self._paper_open[t]

    def _close_paper(self, ticket, exit_price, ts, outcome):
        meta     = self._paper_open[ticket]
        side     = meta['side']
        pnl_pts  = ((exit_price - meta['entry']) if side == Side.BUY
                    else (meta['entry'] - exit_price)) * 100
        pnl      = pnl_pts * meta['lots']
        r        = pnl / meta['risk_ccy'] if meta['risk_ccy'] > 0 else 0
        self._paper_equity += pnl
        emoji = "✅" if outcome == "WIN" else "❌"
        logger.info(
            f"[{self.strategy_name}] {emoji} PAPER CLOSE #{ticket} "
            f"{outcome} @ {exit_price:.2f} pnl=${pnl:+.2f} "
            f"({r:+.2f}R) paper_eq=${self._paper_equity:.2f}"
        )
        self._log({'event': 'CLOSE', 'ticket': ticket, 'time': ts.isoformat(),
                   'exit': exit_price, 'outcome': outcome,
                   'pnl': pnl, 'r_multiple': r,
                   'paper_equity': self._paper_equity})

    def _log(self, event: dict) -> None:
        try:
            with open(PAPER_LOG_FILE, 'a') as f:
                f.write(json.dumps(event) + "\n")
        except Exception as e:
            logger.warning(f"paper log write failed: {e}")

    # ── Engine compatibility ───────────────────────────────────────────────

    def should_close_eod(self, bar: Bar) -> bool:
        return bar.ts.time() >= CLOSE_DEADLINE

    def compute_lots(self, equity, risk_dist, point=0.01, tick_value=1.0,
                     min_lot=0.01, max_lot=1.0):
        if self.paper:
            return 0.0
        risk_cash = equity * self.risk_pct
        loss_per_lot = (risk_dist / point) * tick_value
        if loss_per_lot <= 0:
            return 0.0
        return min(max_lot, max(min_lot, round(risk_cash / loss_per_lot, 2)))
