from app.core.types import Direction
from app.features.structure import compute_structure, micro_choch


def test_structure_bullish_bias(bullish_trend_bars):
    ms = compute_structure(bullish_trend_bars)
    assert ms.bias == Direction.BULL
    # we should have observed at least one BOS during the uptrend
    assert ms.last_bos_ts is not None


def test_structure_flat_no_crash(flat_bars):
    ms = compute_structure(flat_bars)
    # doesn't crash and returns a valid bias
    assert ms.bias in (Direction.BULL, Direction.BEAR)


def test_micro_choch_detects_close_above_swing_high():
    from datetime import datetime, timedelta, timezone

    from app.core.types import Bar

    base = datetime(2024, 1, 8, 8, 0, tzinfo=timezone.utc)
    # build a clear down-then-up micro: lows get taken, final bar closes above last swing high
    bars = []
    prices = [100, 99, 98, 97, 98, 99, 100, 99, 98, 97, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105]
    for i, p in enumerate(prices):
        bars.append(Bar(ts=base + timedelta(minutes=i), open=p - 0.05, high=p + 0.1, low=p - 0.1, close=p))
    assert micro_choch(bars, Direction.BULL, lookback_bars=20) is True
