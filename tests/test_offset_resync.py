"""One broker clock for every symbol: a confirmed offset reaches every strategy, it is
re-checked hourly on live bars, and stale (weekend) bars never trigger a briefing.

Regression for the Sunday 2026-11-08 restart: gold had no fresh tick, so the gold
strategies fell back to +3h while BTC confirmed the real +2h."""
from datetime import datetime, timedelta

import app.main as main_mod
from tests.test_multi_symbol_engine import _engine, _push


def _offsets(eng):
    return {s.strategy_name: s._broker_offset for s in eng.live_strategies + eng.paper_strategies}


def test_confirmed_offset_reaches_every_strategy_at_startup(monkeypatch, tmp_path):
    eng, _ = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    for s in eng.live_strategies + eng.paper_strategies:
        s._broker_offset = 3.0                                  # what gold's stale weekend tick fell back to
    monkeypatch.setattr(main_mod, "confirmed_broker_offset", lambda: 2.0)   # BTC's live tick confirmed +2
    eng._apply_confirmed_offset()
    assert set(_offsets(eng).values()) == {2.0} and eng._broker_offset == 2.0


def test_hourly_recheck_on_live_bars_applies_a_new_offset(monkeypatch, tmp_path):
    eng, feed = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    eng._broker_offset = 3.0
    detected = []
    state = {"confirmed": 3.0}
    monkeypatch.setattr(main_mod, "detect_broker_offset_hours", lambda broker, sym: detected.append(sym) or state["confirmed"])
    monkeypatch.setattr(main_mod, "confirmed_broker_offset", lambda: state["confirmed"])
    t0 = datetime(2026, 11, 1, 20, 0)                           # raw broker time
    for i, mins in enumerate((0, 10, 80)):
        raw = t0 + timedelta(minutes=mins)
        if mins == 80:
            state["confirmed"] = 2.0                            # the broker moved to winter time
            raw = raw - timedelta(hours=1)                      # ...so its bars now read an hour earlier
        eng._now = lambda m=mins: datetime(2026, 11, 1, 17, 2) + timedelta(minutes=m)   # real UTC, bar just closed
        for sym in feed:
            _push(feed, sym, raw, 100, 101, 99, 100); _push(feed, sym, raw + timedelta(minutes=1), 100, 101, 99, 100)
        eng._tick()
    assert len(detected) == 2                                   # once at the start, once an hour later (not every bar)
    assert set(_offsets(eng).values()) == {2.0} and eng._broker_offset == 2.0


def test_stale_weekend_bar_sends_no_briefing(monkeypatch, tmp_path):
    eng, feed = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    sent = []
    eng.notifier.send = lambda text: sent.append(text)
    eng._broker_offset = 2.0
    fri = datetime(2026, 11, 6, 23, 55)                         # gold's last bar before the weekend (16:55 ET)
    _push(feed, "XAUUSD", fri, 4000, 4001, 3999, 4000); _push(feed, "XAUUSD", fri + timedelta(minutes=1), 4000, 4001, 3999, 4000)
    eng._now = lambda: datetime(2026, 11, 8, 10, 7)             # Sunday: that bar is 35 hours old
    eng._tick()
    assert sent == []
