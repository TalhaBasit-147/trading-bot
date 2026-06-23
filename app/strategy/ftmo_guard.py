"""FTMO prop-challenge safety guard.

Protects against the rule breaches that fail ~60-92% of FTMO challenges.
Confirmed against a real FTMO dashboard (2026):
  - Max Daily Loss : 5% of day-start balance, EQUITY-based (incl. floating P/L),
                     resets at midnight CET/CEST (Prague time).
  - Max Loss       : 10% of INITIAL balance, STATIC floor (does not trail).
  - Profit Target  : 5% (trial) / 10% (real phase 1).
  - Min Trading Days.

CONSERVATIVE buffers: the bot stops BEFORE the FTMO hard limit, because a
breach ends the challenge instantly while stopping early just resumes next day.
  - Daily stop at 4% (FTMO limit 5%)
  - Overall stop at 8% (FTMO limit 10%)

FAIL-SAFE: if equity cannot be read, the guard blocks trading (errs toward
safety, never toward trading blind).

This module is pure logic + broker reads. It does NOT place or close orders
itself — it returns decisions the engine acts on. Keeps it testable and safe.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Set

from loguru import logger


# ── CET/CEST helper ────────────────────────────────────────────────────────
def cet_offset_hours(dt_utc: datetime) -> int:
    """Return Prague UTC offset (1 in winter, 2 in summer/DST).

    EU DST: last Sunday of March 01:00 UTC → last Sunday of October 01:00 UTC.
    Computed (not hardcoded) so it is correct across years.
    """
    year = dt_utc.year

    def last_sunday(y, month):
        # find last Sunday of given month
        if month == 12:
            d = datetime(y, 12, 31, tzinfo=timezone.utc)
        else:
            d = datetime(y, month + 1, 1, tzinfo=timezone.utc) - timedelta(days=1)
        # weekday(): Mon=0..Sun=6
        return d - timedelta(days=(d.weekday() + 1) % 7)

    dst_start = last_sunday(year, 3).replace(hour=1)   # 01:00 UTC
    dst_end   = last_sunday(year, 10).replace(hour=1)  # 01:00 UTC
    return 2 if (dst_start <= dt_utc < dst_end) else 1


def cet_day_key(dt_utc: datetime) -> str:
    """The Prague-local calendar date as a string — used to detect the
    FTMO daily reset boundary (midnight CET/CEST)."""
    off = cet_offset_hours(dt_utc)
    return (dt_utc + timedelta(hours=off)).strftime("%Y-%m-%d")


@dataclass
class FTMOConfig:
    initial_balance: float
    daily_loss_pct: float = 0.05      # FTMO hard daily limit
    max_loss_pct: float = 0.10        # FTMO hard overall limit (static)
    profit_target_pct: float = 0.05   # trial 5% (set 0.10 for real phase 1)
    min_trading_days: int = 2
    # Conservative internal buffers (stop BEFORE the hard limit)
    daily_buffer_pct: float = 0.04    # bot stops at 4%
    max_buffer_pct: float = 0.08      # bot stops at 8%


@dataclass
class FTMOState:
    cet_day: str = ""
    day_start_balance: float = 0.0
    trading_days: Set[str] = field(default_factory=set)
    daily_locked: bool = False        # hit daily buffer → no trades till next CET day
    challenge_over: bool = False      # hit overall buffer or target → stop entirely
    over_reason: str = ""


class FTMOGuard:
    def __init__(self, config: FTMOConfig):
        self.cfg = config
        self.st = FTMOState()
        logger.info(
            f"[FTMO] Guard armed. initial=${config.initial_balance:,.0f} "
            f"daily_stop={config.daily_buffer_pct*100:.0f}% (limit {config.daily_loss_pct*100:.0f}%) "
            f"overall_stop={config.max_buffer_pct*100:.0f}% (limit {config.max_loss_pct*100:.0f}%) "
            f"target={config.profit_target_pct*100:.0f}% min_days={config.min_trading_days}"
        )

    # ── derived levels ──────────────────────────────────────────────────────
    @property
    def overall_floor(self) -> float:
        """Static floor: initial balance minus the conservative buffer."""
        return self.cfg.initial_balance * (1 - self.cfg.max_buffer_pct)

    @property
    def overall_hard_floor(self) -> float:
        return self.cfg.initial_balance * (1 - self.cfg.max_loss_pct)

    @property
    def profit_target_equity(self) -> float:
        return self.cfg.initial_balance * (1 + self.cfg.profit_target_pct)

    def daily_floor(self) -> float:
        """Equity floor for the current CET day (day-start balance minus buffer)."""
        return self.st.day_start_balance - self.cfg.initial_balance * self.cfg.daily_buffer_pct

    def daily_hard_floor(self) -> float:
        return self.st.day_start_balance - self.cfg.initial_balance * self.cfg.daily_loss_pct

    # ── per-tick update ─────────────────────────────────────────────────────
    def on_tick(self, now_utc: datetime, equity: Optional[float],
                balance: Optional[float]) -> None:
        """Call every tick BEFORE deciding to trade. Updates day boundary and
        evaluates breaches. Fail-safe: missing data locks trading."""
        # CET day rollover
        key = cet_day_key(now_utc)
        if key != self.st.cet_day:
            self.st.cet_day = key
            # day_start_balance is the balance at CET midnight
            if balance is not None:
                self.st.day_start_balance = balance
            elif equity is not None:
                self.st.day_start_balance = equity
            self.st.daily_locked = False
            logger.info(f"[FTMO] New CET day {key}. day_start_balance="
                        f"${self.st.day_start_balance:,.2f}")

        if self.st.challenge_over:
            return

        # Fail-safe: if we cannot read equity, lock the day (do not trade blind)
        if equity is None:
            if not self.st.daily_locked:
                logger.warning("[FTMO] equity unreadable — locking trading (fail-safe)")
            self.st.daily_locked = True
            return

        # Overall static floor (conservative)
        if equity <= self.overall_floor:
            self.st.challenge_over = True
            self.st.over_reason = (
                f"overall buffer hit: equity ${equity:,.2f} <= floor "
                f"${self.overall_floor:,.2f} (hard ${self.overall_hard_floor:,.2f})"
            )
            logger.error(f"[FTMO] {self.st.over_reason} — STOP ALL TRADING")
            return

        # Daily floor (conservative)
        if self.st.day_start_balance > 0 and equity <= self.daily_floor():
            if not self.st.daily_locked:
                logger.error(
                    f"[FTMO] daily buffer hit: equity ${equity:,.2f} <= daily floor "
                    f"${self.daily_floor():,.2f} (hard ${self.daily_hard_floor():,.2f}) "
                    f"— lock trades until next CET day"
                )
            self.st.daily_locked = True

        # Profit target reached → stop (lock in the pass)
        if equity >= self.profit_target_equity:
            self.st.challenge_over = True
            self.st.over_reason = (
                f"profit target reached: equity ${equity:,.2f} >= "
                f"${self.profit_target_equity:,.2f}"
            )
            logger.success(f"[FTMO] {self.st.over_reason} — target hit, stop trading")

    # ── trade gate ──────────────────────────────────────────────────────────
    def can_open_trade(self) -> tuple[bool, str]:
        if self.st.challenge_over:
            return False, f"challenge over ({self.st.over_reason})"
        if self.st.daily_locked:
            return False, "daily loss buffer reached — locked until next CET day"
        return True, ""

    # ── projected-risk check: would this trade's worst case breach a floor? ──
    def trade_risk_ok(self, equity: float, trade_risk_cash: float) -> tuple[bool, str]:
        """Reject a trade if its full stop-loss could, in the worst case, push
        equity below the daily or overall conservative floor."""
        worst = equity - trade_risk_cash
        if worst <= self.overall_floor:
            return False, (f"trade risk ${trade_risk_cash:,.2f} could breach overall "
                           f"floor (worst ${worst:,.2f} <= ${self.overall_floor:,.2f})")
        if self.st.day_start_balance > 0 and worst <= self.daily_floor():
            return False, (f"trade risk ${trade_risk_cash:,.2f} could breach daily "
                           f"floor (worst ${worst:,.2f} <= ${self.daily_floor():,.2f})")
        return True, ""

    # ── trading-day tracking ────────────────────────────────────────────────
    def record_trade_day(self) -> None:
        if self.st.cet_day:
            self.st.trading_days.add(self.st.cet_day)

    @property
    def trading_day_count(self) -> int:
        return len(self.st.trading_days)

    def min_days_met(self) -> bool:
        return self.trading_day_count >= self.cfg.min_trading_days

    # ── status for logging/telegram ─────────────────────────────────────────
    def status(self, equity: Optional[float]) -> str:
        eq = f"${equity:,.2f}" if equity is not None else "n/a"
        return (f"[FTMO] eq={eq} day_start=${self.st.day_start_balance:,.0f} "
                f"daily_floor=${self.daily_floor():,.0f} "
                f"overall_floor=${self.overall_floor:,.0f} "
                f"days={self.trading_day_count}/{self.cfg.min_trading_days} "
                f"locked={self.st.daily_locked} over={self.st.challenge_over}")
