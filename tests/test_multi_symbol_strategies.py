"""Per-symbol strategy instances: names, caches, scaled constants, and broker-time symbol."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.strategy.broker_time as bt
import app.strategy.prev_day_breakout as pdb
import app.strategy.prev_week_breakout as pwb
from app.core.types import Bar, Side
from app.strategy.prev_day_breakout import PrevDayBreakoutStrategy, PrevDayLevels
from app.strategy.prev_week_breakout import PrevWeekBreakoutStrategy


def test_primary_symbol_keeps_original_names_and_caches():
    pd_, pw = PrevDayBreakoutStrategy(), PrevWeekBreakoutStrategy(paper=False)
    assert (pd_.symbol, pd_.strategy_name, pd_.cache_file) == ("XAUUSD", "PREV_DAY_BREAKOUT", pdb.CACHE_FILE)
    assert (pw.strategy_name, pw.cache_file) == ("PREV_WEEK_BREAKOUT_LIVE", pwb.CACHE_FILE)
    assert (pd_.max_risk_dist, pd_.min_risk_dist, pd_.sl_buffer, pd_.max_overshoot) == (15.0, 3.0, 0.21, 3.0)
    assert (pw.max_risk_dist, pw.min_risk_dist, pw.sl_buffer, pw.max_overshoot) == (15.0, 3.0, 0.21, 5.0)


def test_other_symbols_get_their_own_name_cache_and_scaled_constants():
    pd_ = PrevDayBreakoutStrategy(symbol="btcusd", price_scale=69.23)
    pw = PrevWeekBreakoutStrategy(paper=False, symbol="BTCUSD", price_scale=69.23)
    assert pd_.strategy_name == "PREV_DAY_BREAKOUT_BTCUSD" and pw.strategy_name == "PREV_WEEK_BREAKOUT_LIVE_BTCUSD"
    assert pd_.cache_file == Path("data/prev_day_levels_BTCUSD.json") and pw.cache_file == Path("data/prev_week_levels_BTCUSD.json")
    assert pd_.max_risk_dist == pytest.approx(15.0 * 69.23) and pd_.sl_buffer == pytest.approx(0.21 * 69.23)
    assert pw.max_overshoot == pytest.approx(5.0 * 69.23)


def _seeded_pd(scale, symbol="BTCUSD"):
    s = PrevDayBreakoutStrategy(rr=1.2, risk_pct=0.02, symbol=symbol, price_scale=scale)
    s._broker_offset = 0
    s._current_day = "2026-06-02"
    s._prev_levels = PrevDayLevels(date="2026-06-01", high=70000.0, low=68000.0, range_size=2000.0)
    s._trend_regime = None
    s._was_inside_range = True
    return s


def _bar(close, high):
    return Bar(ts=datetime(2026, 6, 2, 5, 0), open=69990.0, high=high, low=69950.0, close=close, volume=1.0)


def test_scaled_btc_breakout_fires_with_btc_sized_stop():
    sig = _seeded_pd(69.23).on_bar(_bar(close=70100.0, high=70120.0), "BTCUSD")
    assert sig is not None and sig.side == Side.BUY and sig.symbol == "BTCUSD"
    assert sig.entry == pytest.approx(70000.0 + 0.21 * 69.23)
    # risk = clip(range*0.3=600, min 3*k=207.7, max 15*k=1038.5) = 600
    assert sig.risk_dist == pytest.approx(600.0)
    assert sig.tp == pytest.approx(sig.entry + 1.2 * 600.0)


def test_same_bar_with_gold_constants_is_rejected():
    # A $100 overshoot is tiny for BTC but 33x gold's $3 guard: proves the scale is what lets it trade.
    assert _seeded_pd(1.0).on_bar(_bar(close=70100.0, high=70120.0), "BTCUSD") is None


class _Broker:
    def __init__(self, fresh_symbol):
        self.fresh, self.asked = fresh_symbol, []
    def tick(self, symbol):
        self.asked.append(symbol)
        if symbol != self.fresh:
            raise RuntimeError("market closed")
        return {"time": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=2)}
    def get_bars(self, *a):
        return []


def test_broker_offset_is_read_from_the_symbols_own_tick(monkeypatch):
    monkeypatch.setattr(bt, "_last_good_offset", None)
    b = _Broker(fresh_symbol="BTCUSD")         # weekend: gold has no tick, BTC does
    assert bt.detect_broker_offset_hours(b, "BTCUSD") == 2.0
    assert b.asked == ["BTCUSD"]
