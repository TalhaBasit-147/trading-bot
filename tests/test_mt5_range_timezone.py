"""PREV_DAY / PREV_WEEK must ask MT5 for the exact broker-day/week window.

MetaTrader5.copy_rates_range converts a NAIVE datetime from the PC's local time
zone (VPS = UTC+1, broker = UTC+2/+3), which shifted the previous-day window by
an hour (BTC 2026-11-07: low 75,484 logged vs 75,407 on MT5's own D1 candle).
The bounds must be tz-aware with the broker wall-clock value unchanged."""
import sys
import types
from datetime import datetime, timezone

import pytest

from app.strategy.broker_time import server_clock
from app.strategy.prev_day_breakout import PrevDayBreakoutStrategy
from app.strategy.prev_week_breakout import PrevWeekBreakoutStrategy


@pytest.fixture
def fake_mt5(monkeypatch):
    calls = []
    mod = types.SimpleNamespace(TIMEFRAME_M1=1, copy_rates_range=lambda sym, tf, a, b: calls.append((sym, a, b)) or [])
    monkeypatch.setitem(sys.modules, "MetaTrader5", mod)
    return calls


def _assert_server_clock(dt, expected_wall_clock):
    assert dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0      # never naive (= PC local time)
    assert dt.replace(tzinfo=None) == expected_wall_clock                      # broker wall clock, unshifted


def test_prev_day_requests_the_exact_broker_day(fake_mt5):
    PrevDayBreakoutStrategy(symbol="BTCUSD", price_scale=69.23)._get_day_bars("BTCUSD", datetime(2026, 11, 7))
    sym, a, b = fake_mt5[0]
    assert sym == "BTCUSD"
    _assert_server_clock(a, datetime(2026, 11, 7))
    _assert_server_clock(b, datetime(2026, 11, 8))


def test_prev_week_requests_the_exact_broker_week(fake_mt5):
    PrevWeekBreakoutStrategy(paper=False)._get_range_bars("XAUUSD", datetime(2026, 11, 2), datetime(2026, 11, 9))
    _, a, b = fake_mt5[0]
    _assert_server_clock(a, datetime(2026, 11, 2))
    _assert_server_clock(b, datetime(2026, 11, 9))


def test_server_clock_keeps_wall_clock_for_naive_and_aware_input():
    assert server_clock(datetime(2026, 11, 7, 5, 30)) == datetime(2026, 11, 7, 5, 30, tzinfo=timezone.utc)
    assert server_clock(datetime(2026, 11, 7, 5, 30, tzinfo=timezone.utc)) == datetime(2026, 11, 7, 5, 30, tzinfo=timezone.utc)
