"""Shared test fixtures. Generates synthetic bars programmatically so tests
don't depend on network or files.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from app.core.types import Bar


def mk_bar(ts: datetime, o: float, h: float, l: float, c: float, v: float = 100.0) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=v)


def seq(minutes_start: int = 0, n: int = 100, base_ts: datetime | None = None):
    base = base_ts or datetime(2024, 1, 8, 8, 0, tzinfo=timezone.utc)  # Monday 08:00 UTC
    return [base + timedelta(minutes=minutes_start + i) for i in range(n)]


@pytest.fixture
def flat_bars() -> List[Bar]:
    """100 tight-range flat bars around 2000."""
    ts = seq(n=100)
    return [mk_bar(t, 2000.0, 2000.2, 1999.8, 2000.0) for t in ts]


@pytest.fixture
def bullish_trend_bars() -> List[Bar]:
    """Clean uptrend with a pullback and continuation — good for BOS tests."""
    ts = seq(n=80)
    out = []
    price = 2000.0
    for i, t in enumerate(ts):
        if i < 20:
            price += 0.3
            out.append(mk_bar(t, price - 0.1, price + 0.2, price - 0.2, price))
        elif i < 30:
            # pullback
            price -= 0.2
            out.append(mk_bar(t, price + 0.1, price + 0.2, price - 0.2, price))
        else:
            price += 0.4
            out.append(mk_bar(t, price - 0.1, price + 0.3, price - 0.2, price))
    return out
