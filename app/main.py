"""Main engine — runs the Delayed ORB strategy live or in paper mode.

Architecture:
  - Connects to MT5 (live) or PaperBroker (paper mode)
  - Polls for new M1 bars every second
  - Feeds each bar to DelayedORBStrategy
  - Places orders when signals fire
  - Monitors open positions for SL/TP fills
  - Force-closes at 16:30 UTC if still open
  - Sends Telegram alerts on entry/exit/errors
  - Exposes FastAPI health/status/pause endpoints
  - Logs everything to rotating files
"""
from __future__ import annotations

import os
import signal
import threading
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import uvicorn
from loguru import logger

from app.api.server import app as fastapi_app
from app.api.state import engine_state
from app.config import settings
from app.core.types import Bar, Position, Side
from app.db.repo import init_db, save_signal, open_trade, close_trade_by_ticket
from app.execution.base import Broker
from app.execution.paper_broker import PaperBroker
from app.monitoring.logging_setup import setup_logging
from app.notify.telegram import Telegram
from app.strategy.delayed_orb import DelayedORBStrategy, TradeSignal
from app.strategy.risk import RiskManager


KILL_FILE = "./KILL"


def _make_broker() -> Broker:
    if settings.MODE == "live":
        from app.execution.mt5_broker import MT5Broker
        return MT5Broker()
    return PaperBroker(starting_equity=settings.STARTING_EQUITY)


def _run_api() -> None:
    uvicorn.run(
        fastapi_app,
        host=settings.API_HOST,
        port=settings.API_PORT,
        log_level="warning",
        access_log=False,
    )


class Engine:
    def __init__(self, broker: Broker, notifier: Telegram):
        self.broker = broker
        self.notifier = notifier
        self.strategy = DelayedORBStrategy(
            rr=settings.RR_TARGET,
            risk_pct=settings.RISK_PER_TRADE,
        )
        self.risk = RiskManager(starting_equity=broker.account_equity() or settings.STARTING_EQUITY)
        self.symbol = settings.PRIMARY_SYMBOL
        self._last_bar_ts: Optional[datetime] = None
        self._open_ticket: Optional[int] = None
        self._open_meta: Dict = {}
        self._stop = threading.Event()
        self._current_day = ""
        self._errors = 0

        engine_state.broker = broker
        engine_state.risk = self.risk
        engine_state.running = True

    def start(self) -> None:
        self.notifier.send(
            f"🟢 ORB Bot starting in *{settings.MODE}* mode\n"
            f"Symbol: `{self.symbol}`\n"
            f"RR: {settings.RR_TARGET}, Risk: {settings.RISK_PER_TRADE*100}%\n"
            f"Equity: ${self.broker.account_equity():,.2f}"
        )
        logger.info(f"Engine starting. Mode={settings.MODE}, Symbol={self.symbol}")
        self._ensure_symbol()

        while not self._stop.is_set():
            try:
                self._tick()
                self._errors = 0
                time.sleep(1.0)
            except KeyboardInterrupt:
                break
            except Exception as e:
                self._errors += 1
                logger.exception(f"Engine error ({self._errors}): {e}")
                if self._errors >= 10:
                    self.risk.pause(f"errors x{self._errors}: {e}")
                    self.notifier.send(f"🔴 Bot paused: {self._errors} errors\n{e}")
                time.sleep(min(30, 2 ** min(self._errors, 5)))

        self._shutdown()

    def _shutdown(self) -> None:
        self._stop.set()
        engine_state.running = False
        try:
            self.broker.disconnect()
        except Exception:
            pass
        self.notifier.send("🛑 ORB Bot stopped.")
        logger.info("Shutdown complete.")

    def _ensure_symbol(self) -> None:
        """Make sure the symbol is available in the broker."""
        try:
            info = self.broker.symbol_info(self.symbol)
            logger.info(f"Symbol {self.symbol}: point={info['point']}, spread={info['spread_points']}")
        except Exception as e:
            logger.error(f"Symbol {self.symbol} not available: {e}")

    def _tick(self) -> None:
        # Kill switch
        if os.path.exists(KILL_FILE):
            if not self.risk.state.paused:
                self.risk.pause("kill switch file")
                self.notifier.send("🔴 Kill switch activated")
            return

        # Get latest closed M1 bar
        bars = self.broker.get_bars(self.symbol, "M1", 3)
        if len(bars) < 2:
            return
        last_closed = bars[-2]

        # Dedup: only process each bar once
        if self._last_bar_ts is not None and last_closed.ts <= self._last_bar_ts:
            return
        self._last_bar_ts = last_closed.ts

        # Day rollover
        day = last_closed.ts.strftime("%Y-%m-%d")
        if day != self._current_day:
            self._current_day = day
            eq = self.broker.account_equity()
            self.risk.on_new_day(eq)
            self.strategy.reset_day()
            logger.info(f"New day: {day}, equity=${eq:,.2f}")

        # Check if we should force-close at end of session
        if self._open_ticket is not None and self.strategy.should_close_eod(last_closed):
            self._force_close(last_closed)
            return

        # Check if open position was closed by broker (SL/TP hit)
        if self._open_ticket is not None:
            self._check_position_closed()

        # For paper broker: check SL/TP hits
        if isinstance(self.broker, PaperBroker) and self._open_ticket is not None:
            closed = self.broker.on_new_bar(self.symbol, last_closed)
            for ticket, pnl in closed:
                if ticket == self._open_ticket:
                    self._record_close(ticket, pnl, last_closed)

        # Risk check
        if self.risk.state.paused:
            return

        # Feed bar to strategy
        if self._open_ticket is not None:
            return  # already in a trade

        ok, reason = self.risk.can_trade(last_closed.ts)
        if not ok:
            return

        signal = self.strategy.on_bar(last_closed, self.symbol)
        if signal is not None:
            self._execute(signal)

    def _execute(self, sig: TradeSignal) -> None:
        info = self.broker.symbol_info(self.symbol)
        lots = self.strategy.compute_lots(
            equity=self.broker.account_equity(),
            risk_dist=sig.risk_dist,
            point=info["point"],
            tick_value=info["tick_value"],
            min_lot=info.get("min_lot", 0.01),
            max_lot=info.get("max_lot", 1.0),
        )
        if lots < info.get("min_lot", 0.01):
            logger.warning(f"Lots too small: {lots}")
            return

        # Place order
        pos = self.broker.place_market(
            self.symbol, sig.side, lots, sig.sl, sig.tp,
            comment=f"ORB_{sig.side.value}",
        )
        if pos is None or pos.ticket is None:
            logger.error("Order rejected")
            self.notifier.send(f"⚠️ Order rejected: {sig.side.value} {self.symbol}")
            return

        self._open_ticket = pos.ticket
        risk_points = sig.risk_dist / info["point"]
        risk_ccy = risk_points * info["tick_value"] * lots
        self._open_meta = {
            "signal": sig,
            "lots": lots,
            "entry": pos.entry,
            "risk_ccy": max(risk_ccy, 1e-9),
        }
        self.risk.record_trade_open()

        self.notifier.send(
            f"📥 *ENTRY* {sig.side.value} `{self.symbol}` {lots} lots\n"
            f"Entry: `{pos.entry:.2f}`\n"
            f"SL: `{sig.sl:.2f}` | TP: `{sig.tp:.2f}`\n"
            f"Risk: `${risk_ccy:.2f}` | ORB: `{sig.orb_low:.2f}-{sig.orb_high:.2f}`"
        )
        logger.info(f"ENTRY {sig.side.value} {lots} lots @ {pos.entry:.2f}, SL={sig.sl:.2f}, TP={sig.tp:.2f}")

    def _check_position_closed(self) -> None:
        """Check if the broker closed our position (SL/TP hit on MT5)."""
        if self._open_ticket is None:
            return
        positions = self.broker.open_positions(self.symbol)
        tickets = {p.ticket for p in positions}
        if self._open_ticket not in tickets:
            # Position was closed by broker
            meta = self._open_meta
            sig = meta.get("signal")
            eq = self.broker.account_equity()
            # Approximate PnL from equity change
            info = self.broker.symbol_info(self.symbol)
            # We don't know exact exit price from MT5 without deal history query
            # Use tick to estimate
            try:
                tick = self.broker.tick(self.symbol)
                if sig and sig.side == Side.BUY:
                    exit_approx = tick["bid"]
                else:
                    exit_approx = tick["ask"]
                pnl_pts = ((exit_approx - meta["entry"]) / info["point"]
                           if sig.side == Side.BUY
                           else (meta["entry"] - exit_approx) / info["point"])
                pnl = pnl_pts * info["tick_value"] * meta["lots"]
            except Exception:
                pnl = 0.0

            self._record_close(self._open_ticket, pnl, None)

    def _force_close(self, bar: Bar) -> None:
        """Force close position at end of session."""
        if self._open_ticket is None:
            return
        logger.info(f"Force closing #{self._open_ticket} at EOD")
        pnl = self.broker.close(self._open_ticket)
        self._record_close(self._open_ticket, pnl, bar)

    def _record_close(self, ticket: int, pnl: float, bar: Optional[Bar]) -> None:
        meta = self._open_meta
        risk_ccy = meta.get("risk_ccy", 1e-9)
        r = pnl / risk_ccy
        outcome = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BE")
        emoji = "✅" if pnl > 0 else ("❌" if pnl < 0 else "➖")

        self.risk.record_trade_close(pnl)
        eq = self.broker.account_equity()

        self.notifier.send(
            f"{emoji} *{outcome}* `{self.symbol}` pnl=`${pnl:+.2f}` (`{r:+.2f}R`)\n"
            f"Equity: `${eq:,.2f}`"
        )
        logger.info(f"CLOSE #{ticket} {outcome} pnl=${pnl:+.2f} ({r:+.2f}R) equity=${eq:,.2f}")

        # DB logging
        try:
            now = bar.ts if bar else datetime.now(timezone.utc)
            close_trade_by_ticket(ticket, meta.get("entry", 0), now, pnl, r)
        except Exception as e:
            logger.warning(f"DB log failed: {e}")

        self._open_ticket = None
        self._open_meta = {}

    def request_stop(self) -> None:
        self._stop.set()


def main() -> None:
    setup_logging()
    init_db()
    logger.info(f"Starting ORB Bot — MODE={settings.MODE}")

    broker = _make_broker()
    if not broker.connect():
        logger.error("Broker connection failed. Exiting.")
        return

    notifier = Telegram()
    engine = Engine(broker, notifier)

    # API server in background
    t_api = threading.Thread(target=_run_api, daemon=True, name="api")
    t_api.start()

    # Graceful shutdown
    def _handle_sig(signum, frame):
        logger.info(f"Signal {signum} received, stopping...")
        engine.request_stop()
    signal.signal(signal.SIGINT, _handle_sig)
    try:
        signal.signal(signal.SIGTERM, _handle_sig)
    except (AttributeError, ValueError):
        pass

    engine.start()


if __name__ == "__main__":
    main()
