from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import app.strategy.broker_time as broker_time
from app.strategy.broker_time import (
    detect_broker_offset_hours,
    has_confirmed_broker_offset,
    to_utc,
)


class FakeBroker:
    """Minimal broker double exposing only what detect_broker_offset_hours needs."""

    def __init__(self, tick=None, bars=None):
        self._tick = tick
        self._bars = bars or []

    def tick(self, symbol):
        if self._tick is None:
            raise RuntimeError("no tick available")
        return self._tick

    def get_bars(self, symbol, timeframe, n):
        return self._bars[-n:]


class FakeBar:
    def __init__(self, ts):
        self.ts = ts


def setup_function(_fn):
    # Each test starts with a clean slate: nothing confirmed yet.
    broker_time._last_good_offset = None


def _frozen_at(utc_now: datetime):
    """Patch app.strategy.broker_time.datetime.now() to return utc_now."""
    return patch.object(broker_time, "datetime", wraps=datetime)


def test_stale_bar_is_rejected_not_used_as_offset():
    """A 35h-old bar (weekend/market-closed) must NOT produce a garbage
    offset like -35 — the historical bug this module exists to prevent."""
    utc_now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    stale_bar_ts = (utc_now - timedelta(hours=35)).replace(tzinfo=None)
    broker = FakeBroker(tick=None, bars=[FakeBar(ts=stale_bar_ts)])

    with _frozen_at(utc_now) as mock_dt:
        mock_dt.now.return_value = utc_now
        offset = detect_broker_offset_hours(broker)

    assert offset != -35
    assert -12.0 <= offset <= 14.0
    # No live data was usable at all -> falls back to the configured default,
    # and must NOT claim a confirmed detection.
    assert offset == 3.0
    assert has_confirmed_broker_offset() is False


def test_live_tick_preferred_and_rounds_to_clean_half_hour():
    utc_now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    # Raw diff is ~2.98h -> should resolve to a clean +3.0, not 2.98.
    tick_ts = (utc_now + timedelta(hours=2, minutes=58, seconds=48)).replace(tzinfo=None)
    broker = FakeBroker(tick={"bid": 0, "ask": 0, "time": tick_ts}, bars=[])

    with _frozen_at(utc_now) as mock_dt:
        mock_dt.now.return_value = utc_now
        offset = detect_broker_offset_hours(broker)

    assert offset == 3.0
    assert has_confirmed_broker_offset() is True


def test_implausible_offset_rejected_even_if_tick_available():
    utc_now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    # A tick reporting a 21h diff is implausible for a real broker -> reject
    # (this is exactly the "UTC+-21" garbage seen in production logs).
    bad_tick_ts = (utc_now - timedelta(hours=21)).replace(tzinfo=None)
    broker = FakeBroker(tick={"bid": 0, "ask": 0, "time": bad_tick_ts}, bars=[])

    with _frozen_at(utc_now) as mock_dt:
        mock_dt.now.return_value = utc_now
        offset = detect_broker_offset_hours(broker)

    assert offset == 3.0  # configured fallback, not -21
    assert has_confirmed_broker_offset() is False


def test_fresh_bar_used_when_tick_unavailable():
    utc_now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    fresh_bar_ts = (utc_now + timedelta(hours=3, minutes=1)).replace(tzinfo=None)
    broker = FakeBroker(tick=None, bars=[FakeBar(ts=fresh_bar_ts)])

    with _frozen_at(utc_now) as mock_dt:
        mock_dt.now.return_value = utc_now
        offset = detect_broker_offset_hours(broker)

    assert offset == 3.0
    assert has_confirmed_broker_offset() is True


def test_falls_back_to_last_known_good_when_later_call_has_no_fresh_data():
    utc_now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)

    # First call: confirm +3 from a good tick.
    good_tick_ts = (utc_now + timedelta(hours=3)).replace(tzinfo=None)
    broker_good = FakeBroker(tick={"bid": 0, "ask": 0, "time": good_tick_ts}, bars=[])
    with _frozen_at(utc_now) as mock_dt:
        mock_dt.now.return_value = utc_now
        offset1 = detect_broker_offset_hours(broker_good)
    assert offset1 == 3.0
    assert has_confirmed_broker_offset() is True

    # Second call, later: market closed, no tick, only a very stale bar.
    # Must reuse the earlier confirmed +3 rather than compute garbage from
    # the stale bar.
    later = utc_now + timedelta(hours=2)
    stale_bar_ts = (utc_now - timedelta(hours=40)).replace(tzinfo=None)
    broker_bad = FakeBroker(tick=None, bars=[FakeBar(ts=stale_bar_ts)])
    with _frozen_at(later) as mock_dt:
        mock_dt.now.return_value = later
        offset2 = detect_broker_offset_hours(broker_bad)
    assert offset2 == 3.0
    assert has_confirmed_broker_offset() is True


def test_to_utc_still_converts_correctly():
    broker_ts = datetime(2026, 6, 1, 14, 0)
    assert to_utc(broker_ts, 3) == datetime(2026, 6, 1, 11, 0)
    assert to_utc(broker_ts, 3.0) == datetime(2026, 6, 1, 11, 0)
