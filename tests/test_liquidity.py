from datetime import datetime, timedelta, timezone

from app.core.types import Bar, Direction
from app.features.liquidity import detect_pools, detect_sweeps


def _bar(ts, o, h, l, c):
    return Bar(ts=ts, open=o, high=h, low=l, close=c)


def test_equal_highs_and_sweep():
    base = datetime(2024, 1, 8, 8, 0, tzinfo=timezone.utc)
    bars = []
    # Build a sequence with two equal highs at ~100.5
    # pattern: oscillate, but leave two peaks at 100.5
    pattern = [
        100.0, 100.2, 100.3, 100.5,  # peak #1 (swing high at i=3)
        100.2, 100.0, 99.8, 100.0,
        100.2, 100.4, 100.5,         # peak #2
        100.2, 99.9, 99.8, 99.9,
        100.1, 100.3, 100.4,
    ]
    for i, p in enumerate(pattern):
        bars.append(_bar(base + timedelta(minutes=i), p - 0.05, p + 0.02, p - 0.05, p))
    # extend to have enough bars for ATR
    for i in range(len(pattern), 40):
        bars.append(_bar(base + timedelta(minutes=i), 100.2, 100.3, 100.1, 100.2))
    pools = detect_pools(bars, tol_ratio=0.5)
    assert any(p.side == Direction.BULL for p in pools), "should have detected a buy-side pool"

    # Now construct a sweep bar: wick above pool price 100.5 but close below
    pool_price = max(p.price for p in pools if p.side == Direction.BULL)
    sweep_bar = _bar(
        base + timedelta(minutes=len(bars)),
        o=100.2, h=pool_price + 0.3, l=100.1, c=100.1,
    )
    bars.append(sweep_bar)
    swept = detect_sweeps(bars, pools)
    assert swept, "should have detected a sweep on the last bar"
    assert any(s.side == Direction.BULL and s.swept for s in swept)
