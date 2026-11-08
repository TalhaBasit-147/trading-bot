"""Daily pre-session briefing (app/briefing.py) and its engine hook."""
from datetime import date, datetime, timedelta

import pytest

import app.strategy.news_filter as nf
from app import briefing as B
from app.core.types import Bar
from tests.test_multi_symbol_engine import _engine, _push


@pytest.mark.parametrize("utc, et", [
    (datetime(2026, 7, 1, 12, 0), datetime(2026, 7, 1, 8, 0)),     # summer: UTC-4
    (datetime(2026, 12, 1, 13, 0), datetime(2026, 12, 1, 8, 0)),   # winter: UTC-5
    (datetime(2026, 3, 8, 6, 59), datetime(2026, 3, 8, 1, 59)),    # just before DST starts
    (datetime(2026, 3, 8, 7, 0), datetime(2026, 3, 8, 3, 0)),      # DST starts 02:00 -> 03:00
    (datetime(2026, 11, 1, 5, 59), datetime(2026, 11, 1, 1, 59)),  # just before DST ends
    (datetime(2026, 11, 1, 6, 0), datetime(2026, 11, 1, 1, 0)),    # DST ends 02:00 -> 01:00
])
def test_new_york_time(utc, et):
    assert B.to_et(utc) == et


def _h1(slope, start=datetime(2026, 6, 1)):
    """Hourly zigzag (period 8, amplitude 5) on a drifting base: clean fractal swings."""
    bars = []
    for i in range(24 * 7):
        tri = abs((i % 8) - 4)                                   # 4,3,2,1,0,1,2,3 ...
        mid = 100 + slope * i + 5 * (2 - tri)
        bars.append(Bar(ts=start + timedelta(hours=i), open=mid, high=mid + 0.5, low=mid - 0.5, close=mid, volume=1.0))
    return bars


def test_weekly_bias_reads_market_structure():
    assert B.weekly_bias(_h1(+0.2))[0] == "BULL"
    assert B.weekly_bias(_h1(-0.2))[0] == "BEAR"
    assert B.weekly_bias(_h1(0.0))[0] == "RANGE"                 # equal swings are not a trend


def test_snapshot_levels():
    d1 = [Bar(ts=datetime(2026, 6, d), open=0, high=100 + d, low=90 - d, close=95, volume=1) for d in (1, 2, 3, 4, 5, 8, 9)]
    h1 = [Bar(ts=datetime(2026, 6, 9, h), open=0, high=120 + h, low=110 - h, close=115, volume=1) for h in range(3)]
    s = B.symbol_snapshot("XAUUSD", h1, d1)
    assert s.prev_day == (108, 82)            # Mon 8 Jun
    assert s.prev_week == (105, 85)           # 1-5 Jun
    assert s.overnight == (122, 108)          # today so far
    assert s.price == 115


def test_briefing_text_lists_events_and_has_no_markdown_breaking_underscores():
    ev = nf.parse_feed([{"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-11-06T08:30:00-05:00",
                         "impact": "High", "forecast": "65K", "previous": "29K"}])
    snap = B.SymbolSnapshot("BTCUSD", 80000.0, "BULL", "higher high + higher low", (81000, 79000), None, (80500, 79500))
    text = B.format_briefing(datetime(2026, 11, 6, 8, 0), ev, [snap])
    assert "08:30 ET Non-Farm Employment Change (fcst 65K, prev 29K)" in text and "no new entries" in text
    assert "BTCUSD" in text and "BULL" in text and "_" not in text


def test_engine_sends_one_briefing_per_new_york_day(monkeypatch, tmp_path):
    eng, feed = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    sent = []
    eng.notifier.send = lambda text: sent.append(text)
    eng._broker_offset = 3.0                                     # broker = UTC+3 in June
    # raw 14:55 -> 07:55 ET (too early); raw 15:05 -> 08:05 ET (send); raw 15:10 (already sent)
    for k, raw in enumerate([datetime(2026, 6, 2, 14, 55), datetime(2026, 6, 2, 15, 5), datetime(2026, 6, 2, 15, 10)]):
        for sym in feed:
            _push(feed, sym, raw, 4000, 4001, 3999, 4000)
            _push(feed, sym, raw + timedelta(minutes=1), 4000, 4001, 3999, 4000)   # engine reads the last CLOSED bar
        eng._tick()
        assert len(sent) == (0 if k == 0 else 1)
    assert "Pre-session briefing" in sent[0] and "XAUUSD" in sent[0] and "BTCUSD" in sent[0]


def test_briefing_failure_never_breaks_the_tick(monkeypatch, tmp_path):
    eng, feed = _engine(monkeypatch, tmp_path, "")
    eng._broker_offset = 3.0
    monkeypatch.setattr(B, "symbol_snapshot", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    for raw in (datetime(2026, 6, 2, 15, 5), datetime(2026, 6, 2, 15, 6)):
        _push(feed, "XAUUSD", raw, 4000, 4001, 3999, 4000)
    eng._tick()                                                  # must not raise
