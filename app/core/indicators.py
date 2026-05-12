"""Pure-numpy indicator functions used across features and strategy."""
from __future__ import annotations

from typing import List, Sequence, Tuple

import numpy as np

from app.core.types import Bar, Direction, Swing


def true_range(bars: Sequence[Bar]) -> np.ndarray:
    """Wilder TR: max(high-low, |high - prev_close|, |low - prev_close|)."""
    n = len(bars)
    tr = np.zeros(n, dtype=float)
    if n == 0:
        return tr
    tr[0] = bars[0].high - bars[0].low
    for i in range(1, n):
        pc = bars[i - 1].close
        tr[i] = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - pc),
            abs(bars[i].low - pc),
        )
    return tr


def atr(bars: Sequence[Bar], period: int = 14) -> np.ndarray:
    """Exponential-style Wilder ATR."""
    tr = true_range(bars)
    n = len(tr)
    out = np.zeros(n, dtype=float)
    if n == 0:
        return out
    if n < period:
        out[:] = tr.mean() if n > 0 else 0.0
        return out
    # seed with simple mean of first `period`
    out[period - 1] = tr[:period].mean()
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    # back-fill early values with the seed so callers can always index
    out[:period - 1] = out[period - 1]
    return out


def body_average(bars: Sequence[Bar], period: int = 20) -> np.ndarray:
    bodies = np.array([b.body for b in bars], dtype=float)
    n = len(bodies)
    out = np.zeros(n, dtype=float)
    if n == 0:
        return out
    c = np.cumsum(bodies)
    for i in range(n):
        lo = max(0, i - period + 1)
        count = i - lo + 1
        s = c[i] - (c[lo - 1] if lo > 0 else 0.0)
        out[i] = s / count
    return out


def find_fractals(bars: Sequence[Bar], lookback: int = 2) -> List[Swing]:
    """Williams-style fractal swings.

    A swing HIGH at index i requires bars[i].high to be strictly greater than
    the `lookback` bars on each side. Swing LOW is the mirror. We emit both.
    """
    swings: List[Swing] = []
    n = len(bars)
    for i in range(lookback, n - lookback):
        left = bars[i - lookback:i]
        right = bars[i + 1:i + 1 + lookback]
        hi = bars[i].high
        lo = bars[i].low
        if all(hi > b.high for b in left) and all(hi > b.high for b in right):
            swings.append(Swing(idx=i, ts=bars[i].ts, price=hi, kind=Direction.BULL))
        if all(lo < b.low for b in left) and all(lo < b.low for b in right):
            swings.append(Swing(idx=i, ts=bars[i].ts, price=lo, kind=Direction.BEAR))
    return swings


def last_n_swings(swings: List[Swing], kind: Direction, n: int) -> List[Swing]:
    return [s for s in swings if s.kind == kind][-n:]
