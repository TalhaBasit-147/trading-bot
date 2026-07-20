"""Risk manager. Single source of truth for: can we trade now, what size?

State is held in-process and in DB. All decisions log a reason.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from loguru import logger

from app.config import settings
from app.core.types import Side, Signal


KILL_FILE = "./KILL"


@dataclass
class DailyStats:
    day: str
    pnl_ccy: float = 0.0
    trades: int = 0
    losses_in_a_row: int = 0
    last_loss_ts: Optional[datetime] = None


@dataclass
class RiskState:
    equity_start_of_day: float = 10_000.0
    equity: float = 10_000.0
    week_start_equity: float = 10_000.0
    daily: DailyStats = field(default_factory=lambda: DailyStats(day=_today()))
    paused: bool = False
    pause_reason: Optional[str] = None
    open_positions: int = 0


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _iso_week(d: datetime) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


class RiskManager:
    def __init__(self, starting_equity: float = 10_000.0):
        self.state = RiskState(
            equity_start_of_day=starting_equity,
            equity=starting_equity,
            week_start_equity=starting_equity,
        )
        self._current_week = _iso_week(datetime.now(timezone.utc))

    # ---------- equity roll-over ----------

    def on_new_day(self, equity: float) -> None:
        self.state.equity = equity
        self.state.equity_start_of_day = equity
        self.state.daily = DailyStats(day=_today())
        week_now = _iso_week(datetime.now(timezone.utc))
        if week_now != self._current_week:
            self._current_week = week_now
            self.state.week_start_equity = equity

    def update_equity(self, equity: float) -> None:
        self.state.equity = equity

    # ---------- decisions ----------

    def can_trade(self, now: Optional[datetime] = None) -> tuple[bool, str]:
        now = now or datetime.now(timezone.utc)

        if os.path.exists(KILL_FILE):
            return False, "kill-switch file present"

        if self.state.paused:
            return False, f"paused: {self.state.pause_reason}"

        # day roll
        today = _today()
        if self.state.daily.day != today:
            self.state.daily = DailyStats(day=today)
            self.state.equity_start_of_day = self.state.equity

        # daily loss cap
        daily_dd = (self.state.equity - self.state.equity_start_of_day) / max(self.state.equity_start_of_day, 1e-9)
        if daily_dd <= -settings.MAX_DAILY_LOSS:
            return False, f"daily loss cap hit ({daily_dd*100:.2f}%)"

        # weekly loss cap
        weekly_dd = (self.state.equity - self.state.week_start_equity) / max(self.state.week_start_equity, 1e-9)
        if weekly_dd <= -settings.MAX_WEEKLY_LOSS:
            return False, f"weekly loss cap hit ({weekly_dd*100:.2f}%)"

        if self.state.daily.trades >= settings.MAX_TRADES_PER_DAY:
            return False, "max trades per day reached"

        if self.state.open_positions >= settings.MAX_CONCURRENT_TRADES:
            return False, "max concurrent trades reached"

        # cool-down after N consecutive losses
        if (
            self.state.daily.losses_in_a_row >= 2
            and self.state.daily.last_loss_ts is not None
        ):
            cd_until = self.state.daily.last_loss_ts + timedelta(hours=settings.CONSEC_LOSS_COOLDOWN_HOURS)
            if now < cd_until:
                return False, f"cool-down until {cd_until.isoformat()}"

        return True, "ok"

    def pause(self, reason: str) -> None:
        logger.warning(f"RiskManager PAUSED: {reason}")
        self.state.paused = True
        self.state.pause_reason = reason

    def resume(self) -> None:
        logger.info("RiskManager RESUMED")
        self.state.paused = False
        self.state.pause_reason = None

    # ---------- outcomes ----------

    def record_trade_open(self) -> None:
        self.state.open_positions += 1
        self.state.daily.trades += 1

    def record_trade_close(self, pnl_ccy: float, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        self.state.open_positions = max(0, self.state.open_positions - 1)
        self.state.daily.pnl_ccy += pnl_ccy
        self.state.equity += pnl_ccy
        if pnl_ccy < 0:
            self.state.daily.losses_in_a_row += 1
            self.state.daily.last_loss_ts = now
        else:
            self.state.daily.losses_in_a_row = 0

    # ---------- sizing ----------

    def position_size_lots(
        self,
        symbol: str,
        entry: float,
        sl: float,
        point: float,
        contract_size: float,
        tick_value: float,
    ) -> float:
        """Compute lot size so that SL distance * tick_value == equity * RISK_PER_TRADE.

        Works for both forex (tick_value per 1 lot ~ $10) and gold (tick_value
        per 1 lot 100 oz × tick).
        """
        risk_cash = self.state.equity * settings.RISK_PER_TRADE
        dist_points = abs(entry - sl) / point
        if dist_points <= 0 or tick_value <= 0:
            return 0.0
        # money lost per 1 lot if SL hits
        loss_per_lot = dist_points * tick_value
        if loss_per_lot <= 0:
            return 0.0
        raw_lots = risk_cash / loss_per_lot
        # round to 0.01 lot
        lots = max(0.01, round(raw_lots, 2))
        # cap at 2% of equity as a sanity net even if inputs are off
        hard_cap_risk = self.state.equity * 0.02
        if raw_lots * loss_per_lot > hard_cap_risk:
            lots = max(0.01, round(hard_cap_risk / loss_per_lot, 2))
        return lots
