"""News blackout on the Forex Factory feed (app/strategy/news_filter.py)."""
from datetime import date, datetime, timedelta, timezone

import pytest

import app.strategy.news_filter as nf

FEED = [   # shaped like the real ff_calendar_thisweek.json
    {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-11-06T08:30:00-05:00", "impact": "High", "forecast": "65K", "previous": "29K"},
    {"title": "Unemployment Rate", "country": "USD", "date": "2026-11-06T08:30:00-05:00", "impact": "High", "forecast": "4.2%", "previous": "4.2%"},
    {"title": "Congressional Elections", "country": "USD", "date": "2026-11-03T10:16:00-05:00", "impact": "High", "forecast": "", "previous": ""},
    {"title": "CPI m/m", "country": "EUR", "date": "2026-11-05T05:00:00-05:00", "impact": "High", "forecast": "0.2%", "previous": "0.1%"},
    {"title": "FOMC Statement", "country": "USD", "date": "2026-11-04T14:00:00-05:00", "impact": "High", "forecast": "", "previous": ""},
    {"title": "broken row", "country": "USD", "date": "not-a-date", "impact": "High"},
    # low-impact items whose titles contain the keywords (real examples from the live feed)
    {"title": "FOMC Member Williams Speaks", "country": "USD", "date": "2026-11-06T04:30:00-05:00", "impact": "Low"},
    {"title": "ADP Non-Farm Employment Change", "country": "USD", "date": "2026-11-04T08:15:00-05:00", "impact": "Low"},
]


@pytest.fixture
def feed(monkeypatch):
    calls = []
    def fetch():
        calls.append(1); return FEED
    monkeypatch.setattr(nf, "_calendar", nf._Calendar(fetcher=fetch))
    return calls


def test_feed_times_are_converted_to_utc_and_bad_rows_skipped():
    evs = nf.parse_feed(FEED)
    assert len(evs) == 7
    nfp = evs[0]
    assert nfp.time_utc == datetime(2026, 11, 6, 13, 30) and nfp.is_blocked


@pytest.mark.parametrize("now, blocked", [
    (datetime(2026, 11, 6, 13, 0), True),     # 30 min before NFP
    (datetime(2026, 11, 6, 12, 59), False),   # 31 min before
    (datetime(2026, 11, 6, 14, 0), True),     # 30 min after
    (datetime(2026, 11, 6, 14, 1), False),    # 31 min after
    (datetime(2026, 11, 4, 19, 10), True),    # 10 min after the FOMC statement
    (datetime(2026, 11, 3, 15, 16), False),   # elections are "High" but not a data release
    (datetime(2026, 11, 5, 10, 0), False),    # euro-area CPI is not a USD release
    (datetime(2026, 11, 6, 9, 40), False),    # low-impact Fed speaker
    (datetime(2026, 11, 4, 13, 20), False),   # low-impact ADP report
])
def test_blackout_window(feed, now, blocked):
    assert nf.is_news_blackout(now)[0] is blocked


def test_tz_aware_now_is_accepted(feed):
    assert nf.is_news_blackout(datetime(2026, 11, 6, 8, 40, tzinfo=timezone(timedelta(hours=-5))))[0] is True


def test_feed_is_cached_for_an_hour(feed):
    t = datetime(2026, 11, 6, 9, 0)
    for m in range(0, 59, 10):
        nf.is_news_blackout(t + timedelta(minutes=m))
    assert len(feed) == 1
    nf.is_news_blackout(t + timedelta(minutes=61))
    assert len(feed) == 2


def test_feed_failure_never_blocks_and_backs_off(monkeypatch):
    calls = []
    def boom():
        calls.append(1); raise ConnectionError("feed down")
    monkeypatch.setattr(nf, "_calendar", nf._Calendar(fetcher=boom))
    t = datetime(2026, 11, 6, 13, 30)            # exactly NFP time, but the feed is unreachable
    assert nf.is_news_blackout(t) == (False, "")
    nf.is_news_blackout(t + timedelta(minutes=2))
    assert len(calls) == 1                         # waits 5 minutes before retrying
    nf.is_news_blackout(t + timedelta(minutes=6))
    assert len(calls) == 2


def test_todays_usd_high_impact_events_for_the_briefing(feed):
    evs = nf.usd_high_impact_events(date(2026, 11, 6), now=datetime(2026, 11, 6, 13, 0))
    assert [e.title for e in evs] == ["Non-Farm Employment Change", "Unemployment Rate"]
