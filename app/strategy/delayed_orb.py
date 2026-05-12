"""NY Delayed ORB Strategy.

Backtested on 3.5 months of real XAUUSD M1 data:
  53 trades, 66% WR, PF 2.26, +56.8% return on $1000 at 2% risk.

Logic:
  1. Mark the opening range: high/low of 13:30-13:44 UTC (first 15 min of NY equity open)
  2. Skip the first 30 min of breakout noise (13:45-14:14) — this is where fake-outs happen
  3. After 14:15 UTC, enter on first clean break of the ORB:
     - Price breaks ABOVE ORB high → BUY, SL at ORB low
     - Price breaks BELOW ORB low → SELL, SL at ORB high
  4. TP = entry ± (risk × RR)
  5. Only 1 trade per day. Close any open position by 16:30 UTC.

Parameters:
  - ORB window: 13:30 - 13:44 UTC
  - Entry window: 14:15 - 16:00 UTC
  - ORB range filter: $2 - $15 (skip if too tight or too wide)
  - SL buffer: $0.21 (spread protection)
  - RR: 1.2 (default, configurable)
  - Risk per trade: 2% of equity (default, configurable)
  - Max risk distance: $15
  - Close deadline: 16:30 UTC
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import List, Optional, Sequence

from app.core.types import Bar, Side


@dataclass
class ORBSetup:
    """Represents the opening range for a single day."""
    date: str
    orb_high: float
    orb_low: float
    orb_range: float
    formed: bool = False


@dataclass
class TradeSignal:
    ts: datetime
    symbol: str
    side: Side
    entry: float
    sl: float
    tp: float
    risk_dist: float
    orb_high: float
    orb_low: float
    orb_range: float


# Time boundaries (UTC)
ORB_START = time(13, 30)
ORB_END = time(13, 45)
ENTRY_START = time(14, 15)
ENTRY_END = time(16, 0)
CLOSE_DEADLINE = time(16, 30)

# Filters
MIN_ORB_RANGE = 2.0
MAX_ORB_RANGE = 15.0
MAX_RISK_DIST = 15.0
MIN_RISK_DIST = 2.0
SL_BUFFER = 0.21


class DelayedORBStrategy:
    def __init__(self, rr: float = 1.2, risk_pct: float = 0.02):
        self.rr = rr
        self.risk_pct = risk_pct
        self._current_orb: Optional[ORBSetup] = None
        self._current_day: str = ""
        self._traded_today: bool = False
        self._orb_bars: List[Bar] = []

    def reset_day(self) -> None:
        self._current_orb = None
        self._traded_today = False
        self._orb_bars = []

    def on_bar(self, bar: Bar, symbol: str = "XAUUSD") -> Optional[TradeSignal]:
        """Feed a new M1 bar. Returns a TradeSignal if conditions are met."""
        day = bar.ts.strftime("%Y-%m-%d")
        t = bar.ts.time()

        # new day
        if day != self._current_day:
            self._current_day = day
            self.reset_day()

        # already traded today
        if self._traded_today:
            return None

        # Phase 1: collect ORB bars (13:30 - 13:44)
        if ORB_START <= t < ORB_END:
            self._orb_bars.append(bar)
            return None

        # Phase 2: finalize ORB when we exit the window
        if t >= ORB_END and self._current_orb is None:
            if len(self._orb_bars) < 10:
                # not enough bars — skip today (holiday / late start)
                self._traded_today = True
                return None
            orb_high = max(b.high for b in self._orb_bars)
            orb_low = min(b.low for b in self._orb_bars)
            orb_range = orb_high - orb_low
            if orb_range < MIN_ORB_RANGE or orb_range > MAX_ORB_RANGE:
                self._traded_today = True  # skip today, bad ORB
                return None
            self._current_orb = ORBSetup(
                date=day, orb_high=orb_high, orb_low=orb_low,
                orb_range=orb_range, formed=True,
            )

        # Phase 3: wait for entry window (14:15 - 16:00)
        if self._current_orb is None or not self._current_orb.formed:
            return None
        if t < ENTRY_START or t > ENTRY_END:
            return None

        orb = self._current_orb

        # Check for breakout
        side = None
        if bar.high > orb.orb_high:
            side = Side.BUY
            entry = orb.orb_high + SL_BUFFER
            sl = orb.orb_low - SL_BUFFER
            risk_dist = entry - sl
        elif bar.low < orb.orb_low:
            side = Side.SELL
            entry = orb.orb_low - SL_BUFFER
            sl = orb.orb_high + SL_BUFFER
            risk_dist = sl - entry

        if side is None:
            return None
        if risk_dist < MIN_RISK_DIST or risk_dist > MAX_RISK_DIST:
            return None

        # compute TP
        if side == Side.BUY:
            tp = entry + risk_dist * self.rr
        else:
            tp = entry - risk_dist * self.rr

        self._traded_today = True

        return TradeSignal(
            ts=bar.ts, symbol=symbol, side=side,
            entry=entry, sl=sl, tp=tp,
            risk_dist=risk_dist,
            orb_high=orb.orb_high, orb_low=orb.orb_low,
            orb_range=orb.orb_range,
        )

    def should_close_eod(self, bar: Bar) -> bool:
        """Check if we should force-close at end of session."""
        return bar.ts.time() >= CLOSE_DEADLINE

    def compute_lots(
        self,
        equity: float,
        risk_dist: float,
        point: float = 0.01,
        tick_value: float = 1.0,
        min_lot: float = 0.01,
        max_lot: float = 1.0,
    ) -> float:
        risk_cash = equity * self.risk_pct
        risk_points = risk_dist / point
        loss_per_lot = risk_points * tick_value
        if loss_per_lot <= 0:
            return 0.0
        lots = risk_cash / loss_per_lot
        lots = max(min_lot, round(lots, 2))
        lots = min(lots, max_lot)
        return lots
