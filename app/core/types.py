"""Domain types used throughout the engine.

Everything is an immutable-ish dataclass so they are easy to log, pickle,
and reason about. Prices are floats; timestamps are timezone-aware UTC
datetimes (we normalize every feed to UTC on ingest).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class Direction(str, Enum):
    BULL = "BULL"
    BEAR = "BEAR"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class Bar:
    ts: datetime        # bar OPEN time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def bullish(self) -> bool:
        return self.close >= self.open


@dataclass(frozen=True)
class Swing:
    idx: int
    ts: datetime
    price: float
    kind: Direction     # BULL = swing high, BEAR = swing low (label by what they *are*)
    # note: we use the convention `Direction.BULL` for swing HIGH (resistance)
    # and `Direction.BEAR` for swing LOW (support). This keeps call sites simple.


@dataclass(frozen=True)
class LiquidityPool:
    ts: datetime
    price: float
    side: Direction            # BULL = buy-side liq (above highs), BEAR = sell-side (below lows)
    strength: int              # how many equal highs/lows clustered
    swept: bool = False
    sweep_ts: Optional[datetime] = None


@dataclass(frozen=True)
class OrderBlock:
    ts: datetime
    direction: Direction       # BULL OB = demand (long setup), BEAR OB = supply (short setup)
    high: float
    low: float
    open: float
    close: float
    displacement_ratio: float  # body of OB vs avg body
    touched: int = 0
    mitigated: bool = False    # true once price fully closes beyond the OB

    @property
    def mid(self) -> float:
        return 0.5 * (self.high + self.low)


@dataclass(frozen=True)
class FairValueGap:
    ts: datetime
    direction: Direction       # BULL FVG = demand zone, BEAR = supply
    high: float                # upper bound of the gap
    low: float                 # lower bound of the gap
    size: float                # high - low
    atr_ratio: float           # size / ATR
    filled: bool = False
    partial_fill_pct: float = 0.0


@dataclass(frozen=True)
class MarketStructure:
    bias: Direction
    last_bos_ts: Optional[datetime]
    last_choch_ts: Optional[datetime]
    last_swing_high: Optional[Swing]
    last_swing_low: Optional[Swing]


@dataclass
class Signal:
    ts: datetime
    symbol: str
    side: Side
    entry: float
    sl: float
    tp1: float
    tp2: float
    rr: float
    reasons: List[str] = field(default_factory=list)
    features: dict = field(default_factory=dict)  # for ML + DB

    # populated later
    ml_prob: Optional[float] = None
    score: float = 0.0


@dataclass
class Position:
    symbol: str
    side: Side
    qty: float                 # lots (MT5 convention)
    entry: float
    sl: float
    tp: float
    open_ts: datetime
    ticket: Optional[int] = None
    comment: str = ""
