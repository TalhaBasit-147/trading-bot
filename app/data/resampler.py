"""Bar resampling. Converts a stream of M1 bars to M5/M15/H1 etc.

We support two modes:
  - batch: resample a list all at once (used in backtest/feature bootstrap)
  - online: call `add_m1(bar)` and receive a completed higher-tf bar when its
    window closes. This is how the live engine feeds 15m features.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from app.core.types import Bar


_TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def _bucket_start(ts: datetime, minutes: int) -> datetime:
    # floor to minute bucket (UTC)
    total = ts.hour * 60 + ts.minute
    floored = (total // minutes) * minutes
    hh, mm = divmod(floored, 60)
    return ts.replace(hour=hh, minute=mm, second=0, microsecond=0)


def resample(bars_m1: Sequence[Bar], timeframe: str) -> List[Bar]:
    if timeframe == "M1":
        return list(bars_m1)
    m = _TF_MINUTES[timeframe]
    buckets: Dict[datetime, Bar] = {}
    for b in bars_m1:
        k = _bucket_start(b.ts, m)
        cur = buckets.get(k)
        if cur is None:
            buckets[k] = Bar(ts=k, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume)
        else:
            buckets[k] = Bar(
                ts=k,
                open=cur.open,
                high=max(cur.high, b.high),
                low=min(cur.low, b.low),
                close=b.close,
                volume=cur.volume + b.volume,
            )
    return [buckets[k] for k in sorted(buckets.keys())]


@dataclass
class OnlineResampler:
    timeframe: str
    _current_bucket: Optional[datetime] = None
    _acc: Optional[Bar] = None
    completed: List[Bar] = field(default_factory=list)

    def add(self, m1: Bar) -> Optional[Bar]:
        """Feed an M1 bar, return a completed higher-tf Bar when one closes."""
        minutes = _TF_MINUTES[self.timeframe]
        k = _bucket_start(m1.ts, minutes)
        if self._current_bucket is None:
            self._current_bucket = k
            self._acc = Bar(ts=k, open=m1.open, high=m1.high, low=m1.low, close=m1.close, volume=m1.volume)
            return None
        if k == self._current_bucket:
            a = self._acc
            assert a is not None
            self._acc = Bar(
                ts=a.ts, open=a.open,
                high=max(a.high, m1.high),
                low=min(a.low, m1.low),
                close=m1.close,
                volume=a.volume + m1.volume,
            )
            return None
        # new bucket → previous bar closed
        closed = self._acc
        assert closed is not None
        self.completed.append(closed)
        self._current_bucket = k
        self._acc = Bar(ts=k, open=m1.open, high=m1.high, low=m1.low, close=m1.close, volume=m1.volume)
        return closed
