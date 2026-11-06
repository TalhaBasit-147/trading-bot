"""Multi-symbol Engine: both symbols are watched, each trades on its own setup and
its own daily cap, and a gold-only config stays single-symbol."""
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import app.main as main_mod
import app.strategy.news_filter as news_mod
from app.config import settings
from app.core.types import Bar
from app.execution.paper_broker import PaperBroker
from app.strategy.prev_day_breakout import PrevDayLevels
from app.strategy.prev_week_breakout import WeekLevels

GOLD = {"point": 0.01, "digits": 2, "contract_size": 100, "tick_value": 1.0, "spread_points": 20, "min_lot": 0.01, "max_lot": 100, "lot_step": 0.01}
BTC = {"point": 0.01, "digits": 2, "contract_size": 1, "tick_value": 0.01, "spread_points": 1200, "min_lot": 0.01, "max_lot": 100, "lot_step": 0.01}


def _engine(monkeypatch, tmp_path, trade_symbols):
    monkeypatch.chdir(tmp_path)                                    # no .env, caches/logs land in tmp
    monkeypatch.setattr(settings, "TRADE_SYMBOLS", trade_symbols)
    monkeypatch.setenv("BTCUSD_PRICE_SCALE", "69.23")
    monkeypatch.setattr(news_mod, "is_news_blackout", lambda *a, **k: (False, ""))
    for name in ("ORB5MinStrategy", "PrevDayBreakoutStrategy", "PrevWeekBreakoutStrategy", "TWKMomentumStrategy"):
        monkeypatch.setattr(getattr(main_mod, name), "set_broker", lambda self, broker: None, raising=False)
    monkeypatch.setattr(main_mod, "detect_broker_offset_hours", lambda broker, *a: 3.0)
    feed = {"XAUUSD": [], "BTCUSD": []}
    broker = PaperBroker(starting_equity=1000.0, bar_provider=lambda s, tf, n: feed[s][-n:],
                         symbol_specs={"XAUUSD": GOLD, "BTCUSD": BTC, "EURUSD": GOLD})
    return main_mod.Engine(broker, MagicMock()), feed


def _seed_pd(s, day, h, l):
    s._current_day = day
    s._prev_levels = PrevDayLevels(date="prev", high=h, low=l, range_size=h - l)
    s._trend_regime = None
    s._was_inside_range = True


def _park_pw(s, ts):
    s._current_week = s._week_label(ts - timedelta(hours=3))   # strategies run at the default +3h offset
    s._current_day = (ts - timedelta(hours=3)).strftime("%Y-%m-%d")
    s._levels = WeekLevels("prev", 10**9, 0.0, 10**9)             # far away: never triggers
    s._was_inside = True


def _push(feed, sym, ts, o, h, l, c):
    feed[sym].append(Bar(ts=ts, open=o, high=h, low=l, close=c, volume=1.0))


def test_registry_and_caps_for_two_symbols(monkeypatch, tmp_path):
    eng, _ = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    assert [s.strategy_name for s in eng.live_strategies] == [
        "PREV_DAY_BREAKOUT", "PREV_WEEK_BREAKOUT_LIVE", "PREV_DAY_BREAKOUT_BTCUSD", "PREV_WEEK_BREAKOUT_LIVE_BTCUSD"]
    assert eng.risk.max_trades_per_day == 2 * settings.MAX_TRADES_PER_DAY
    assert eng.risk.max_concurrent_trades == 2 * settings.MAX_CONCURRENT_TRADES
    assert eng._live_by_symbol["BTCUSD"][0].max_risk_dist == pytest.approx(15 * 69.23)


def test_gold_only_config_is_single_symbol(monkeypatch, tmp_path):
    eng, _ = _engine(monkeypatch, tmp_path, "")
    assert eng.symbols == ["XAUUSD"] and eng._multi is False
    assert [s.strategy_name for s in eng.live_strategies] == ["PREV_DAY_BREAKOUT", "PREV_WEEK_BREAKOUT_LIVE"]
    assert eng.risk.max_trades_per_day == settings.MAX_TRADES_PER_DAY


def test_both_symbols_trade_their_own_setup_on_the_same_day(monkeypatch, tmp_path):
    eng, feed = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    t0 = datetime(2026, 6, 2, 8, 0)                                 # broker time -> 05:00 UTC, inside PREV_DAY session
    gpd, gpw = eng._live_by_symbol["XAUUSD"]
    bpd, bpw = eng._live_by_symbol["BTCUSD"]
    _seed_pd(gpd, "2026-06-02", 4010.0, 3990.0); _seed_pd(bpd, "2026-06-02", 70000.0, 68000.0)
    _park_pw(gpw, t0); _park_pw(bpw, t0)

    # minute 1: both inside their ranges; minute 2: gold breaks its high, BTC breaks its high
    for i, (g, b) in enumerate([((4000, 4002, 3998, 4001), (69000, 69050, 68950, 69010)),
                                ((4001, 4011, 4000, 4010.5), (69990, 70120, 69950, 70100))]):
        ts = t0 + timedelta(minutes=i)
        _push(feed, "XAUUSD", ts, *g); _push(feed, "BTCUSD", ts, *b)
    for sym in feed:                                                # the engine reads bars[-2] (last CLOSED bar)
        _push(feed, sym, t0 + timedelta(minutes=2), *([feed[sym][-1].close] * 4))
    eng._tick()

    assert set(eng._open_positions) == {"PREV_DAY_BREAKOUT", "PREV_DAY_BREAKOUT_BTCUSD"}
    assert eng._open_positions["PREV_DAY_BREAKOUT_BTCUSD"]["symbol"] == "BTCUSD"
    assert {p.symbol for p in eng.broker.open_positions()} == {"XAUUSD", "BTCUSD"}
    assert (eng._sym["XAUUSD"].trades_today, eng._sym["BTCUSD"].trades_today) == (1, 1)
    assert eng.risk.state.daily.trades == 2    # the old global cap (1/day) would have blocked BTC here


def test_btc_trades_on_a_weekend_while_gold_has_no_bars(monkeypatch, tmp_path):
    eng, feed = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    t0 = datetime(2026, 6, 6, 8, 0)                                 # Saturday
    bpd, bpw = eng._live_by_symbol["BTCUSD"]
    _seed_pd(bpd, "2026-06-06", 70000.0, 68000.0); _park_pw(bpw, t0)
    _push(feed, "BTCUSD", t0, 69000, 69050, 68950, 69010)
    _push(feed, "BTCUSD", t0 + timedelta(minutes=1), 69990, 70120, 69950, 70100)
    _push(feed, "BTCUSD", t0 + timedelta(minutes=2), 70100, 70100, 70100, 70100)
    eng._tick()                                                     # gold feed empty: skipped, not an error
    assert set(eng._open_positions) == {"PREV_DAY_BREAKOUT_BTCUSD"}
