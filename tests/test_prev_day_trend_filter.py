"""Tests for PREV_DAY_BREAKOUT's D1 EMA50 trend filter.

PrevDayBreakoutStrategy fetches its D1 regime via a direct `import
MetaTrader5 as mt5` call (matching the existing pattern for prev-day levels
in this same file), which isn't available on this Linux test sandbox. These
tests seed the strategy's internal state directly (_prev_levels,
_trend_regime, _was_inside_range, _current_day) and drive on_bar() with
synthetic bars, bypassing the MT5-backed fetch -- the same approach used
elsewhere in this test suite for MT5-only strategies.
"""
from datetime import datetime
from unittest.mock import patch

from app.config import settings
from app.core.types import Bar, Side
from app.strategy.prev_day_breakout import DailyTrend, PrevDayBreakoutStrategy, PrevDayLevels


def _seeded_strategy(prev_close: float, ema50: float) -> PrevDayBreakoutStrategy:
    strat = PrevDayBreakoutStrategy(rr=1.5, risk_pct=0.02)
    strat._broker_offset = 0  # bar.ts treated as already-UTC for this test
    strat._current_day = "2026-06-02"  # avoids triggering a real reset_day()/MT5 fetch
    strat._prev_levels = PrevDayLevels(date="2026-06-01", high=4010.0, low=3990.0, range_size=20.0)
    strat._trend_regime = DailyTrend(prev_close=prev_close, ema50=ema50)
    return strat


def _bar(hour, minute, open_, high, low, close):
    return Bar(ts=datetime(2026, 6, 2, hour, minute), open=open_, high=high, low=low, close=close, volume=1.0)


def test_upward_breakout_skipped_in_bearish_regime():
    """(1) D1 regime bearish (prev_close < ema50), upward breakout fires ->
    must be SKIPPED (no signal), and must not consume _traded_today (a later
    same-day breakout in the agreeing direction must still be evaluable)."""
    strat = _seeded_strategy(prev_close=4000.0, ema50=4005.0)  # bearish
    strat._was_inside_range = True

    # crosses above prev-day high (4010.0), overshoot=1.0 <= MAX_OVERSHOOT(3.0)
    sig = strat.on_bar(_bar(10, 0, 4008.0, 4011.5, 4007.0, 4011.0))

    assert sig is None
    assert strat._traded_today is False


def test_downward_breakout_passes_in_same_bearish_regime():
    """(2) Same bearish D1 regime, downward breakout -> must PASS (signal
    returned), confirming the filter's direction logic, not just "always skip"."""
    strat = _seeded_strategy(prev_close=4000.0, ema50=4005.0)  # bearish
    strat._was_inside_range = True

    # crosses below prev-day low (3990.0), overshoot=1.0 <= MAX_OVERSHOOT(3.0)
    sig = strat.on_bar(_bar(10, 0, 3992.0, 3993.0, 3988.5, 3989.0))

    assert sig is not None
    assert sig.side == Side.SELL
    assert strat._traded_today is True


def test_downward_breakout_skipped_in_bullish_regime():
    """Symmetric case: bullish regime (prev_close > ema50) must skip a
    downward breakout."""
    strat = _seeded_strategy(prev_close=4010.0, ema50=4005.0)  # bullish
    strat._was_inside_range = True

    sig = strat.on_bar(_bar(10, 0, 3992.0, 3993.0, 3988.5, 3989.0))

    assert sig is None
    assert strat._traded_today is False


def test_upward_breakout_passes_in_bullish_regime():
    strat = _seeded_strategy(prev_close=4010.0, ema50=4005.0)  # bullish
    strat._was_inside_range = True

    sig = strat.on_bar(_bar(10, 0, 4008.0, 4011.5, 4007.0, 4011.0))

    assert sig is not None
    assert sig.side == Side.BUY
    assert strat._traded_today is True


def test_fails_open_when_regime_unavailable():
    """No D1 regime data (fetch failed / insufficient bars) -> the filter
    must not block the trade at all, matching the fail-open contract."""
    strat = _seeded_strategy(prev_close=0.0, ema50=0.0)
    strat._trend_regime = None  # simulate fetch failure / insufficient bars
    strat._was_inside_range = True

    sig = strat.on_bar(_bar(10, 0, 4008.0, 4011.5, 4007.0, 4011.0))

    assert sig is not None
    assert sig.side == Side.BUY


def test_config_flag_disables_the_filter_entirely():
    """PREV_DAY_TREND_FILTER_ENABLED=False must let every fresh cross through
    regardless of regime, with no code change needed."""
    strat = _seeded_strategy(prev_close=4000.0, ema50=4005.0)  # bearish
    strat._was_inside_range = True

    with patch.object(settings, "PREV_DAY_TREND_FILTER_ENABLED", False):
        sig = strat.on_bar(_bar(10, 0, 4008.0, 4011.5, 4007.0, 4011.0))

    assert sig is not None
    assert sig.side == Side.BUY  # would have been skipped with the filter on


def test_ema_matches_hand_computed_value():
    """Direct check of the EMA helper itself against a hand-computed series."""
    closes = [10.0] * 50 + [20.0]  # SMA seed = 10.0, one step toward 20.0
    period = 50
    expected_multiplier = 2.0 / 51
    expected = (20.0 - 10.0) * expected_multiplier + 10.0
    assert abs(PrevDayBreakoutStrategy._ema(closes, period) - expected) < 1e-9


def test_ema_falls_back_to_simple_average_when_fewer_than_period_values():
    closes = [1.0, 2.0, 3.0]
    assert PrevDayBreakoutStrategy._ema(closes, 50) == 2.0
