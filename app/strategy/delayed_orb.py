"""NY Delayed ORB Strategy.

Backtested on 3.5 months of XAUUSD M1 data:
  61 trades, 60.7% WR, +$521 on $1000 (+52.1%)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import List, Optional

from app.core.types import Bar, Side


@dataclass
class ORBSetup:
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
    strategy: str
    note: str = ""


# Time boundaries — VPS local time (UTC+2, summer Europe)
# NY equity open 13:30 UTC = 15:30 VPS local
ORB_START = time(18, 30)
ORB_END = time(18, 45)
ENTRY_START = time(19, 15)
ENTRY_END = time(21, 0)
CLOSE_DEADLINE = time(21, 30)

MIN_ORB_RANGE = 2.0
MAX_ORB_RANGE = 20.0
MAX_RISK_DIST = 20.0
MIN_RISK_DIST = 2.0
SL_BUFFER = 0.21


class DelayedORBStrategy:
    def __init__(self, rr: float = 1.2, risk_pct: float = 0.02):
        self.rr = rr
        self.risk_pct = risk_pct
        self.strategy_name = "DELAYED_ORB"
        self._current_orb: Optional[ORBSetup] = None
        self._current_day: str = ""
        self._traded_today: bool = False
        self._orb_bars: List[Bar] = []

    def reset_day(self) -> None:
        self._current_orb = None
        self._traded_today = False
        self._orb_bars = []

    def on_bar(self, bar: Bar, symbol: str = "XAUUSD") -> Optional[TradeSignal]:
        day = bar.ts.strftime("%Y-%m-%d")
        t = bar.ts.time()

        if day != self._current_day:
            self._current_day = day
            self.reset_day()

        if self._traded_today:
            return None

        if ORB_START <= t < ORB_END:
            self._orb_bars.append(bar)
            return None

        if t >= ORB_END and self._current_orb is None:
            if len(self._orb_bars) < 10:
                self._traded_today = True
                return None
            oh = max(b.high for b in self._orb_bars)
            ol = min(b.low for b in self._orb_bars)
            rng = oh - ol
            if rng < MIN_ORB_RANGE or rng > MAX_ORB_RANGE:
                self._traded_today = True
                return None
            self._current_orb = ORBSetup(day, oh, ol, rng, True)

        if self._current_orb is None or not self._current_orb.formed:
            return None
        if t < ENTRY_START or t > ENTRY_END:
            return None

        orb = self._current_orb
        side = None
        if bar.high > orb.orb_high:
            side = Side.BUY
            entry = orb.orb_high + SL_BUFFER
            sl = orb.orb_low - SL_BUFFER
        elif bar.low < orb.orb_low:
            side = Side.SELL
            entry = orb.orb_low - SL_BUFFER
            sl = orb.orb_high + SL_BUFFER

        if side is None:
            return None

        risk_dist = abs(entry - sl)
        if risk_dist < MIN_RISK_DIST or risk_dist > MAX_RISK_DIST:
            return None

        tp = entry + risk_dist * self.rr if side == Side.BUY else entry - risk_dist * self.rr
        self._traded_today = True

        return TradeSignal(
            ts=bar.ts, symbol=symbol, side=side,
            entry=entry, sl=sl, tp=tp,
            risk_dist=risk_dist,
            strategy=self.strategy_name,
            note=f"ORB H={orb.orb_high:.2f} L={orb.orb_low:.2f} range=${orb.orb_range:.1f}",
        )

    def should_close_eod(self, bar: Bar) -> bool:
        return bar.ts.time() >= CLOSE_DEADLINE

    def compute_lots(
        self, equity: float, risk_dist: float,
        point: float = 0.01, tick_value: float = 1.0,
        min_lot: float = 0.01, max_lot: float = 1.0,
    ) -> float:
        risk_cash = equity * self.risk_pct
        risk_points = risk_dist / point
        loss_per_lot = risk_points * tick_value
        if loss_per_lot <= 0:
            return 0.0
        lots = max(min_lot, round(risk_cash / loss_per_lot, 2))
        return min(lots, max_lot)
