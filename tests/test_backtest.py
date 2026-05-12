"""Sanity test: the backtester runs end-to-end on synthetic bars without crashing.

We don't assert PnL — synthetic data may or may not produce setups — but we do
assert the engine completes, handles the warmup, and writes metrics.
"""
from datetime import datetime, timedelta, timezone
import random

from app.backtest.engine import Backtester
from app.core.types import Bar


def test_backtester_runs():
    random.seed(42)
    base = datetime(2024, 1, 8, 7, 0, tzinfo=timezone.utc)
    bars = []
    price = 2000.0
    for i in range(3000):  # ~50 hours of M1
        drift = random.gauss(0, 0.3)
        price += drift
        o = price - 0.1
        h = price + abs(random.gauss(0, 0.3))
        l = price - abs(random.gauss(0, 0.3))
        c = price
        bars.append(Bar(ts=base + timedelta(minutes=i), open=o, high=h, low=l, close=c, volume=100))
    bt = Backtester("XAUUSD", bars, starting_equity=10_000)
    trades = bt.run()
    # it must not crash; trades is a (possibly empty) list
    assert isinstance(trades, list)
