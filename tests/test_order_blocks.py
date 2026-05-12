from datetime import datetime, timedelta, timezone

from app.core.types import Bar, Direction
from app.features.order_blocks import detect_order_blocks, price_in_ob


def _bar(ts, o, h, l, c):
    return Bar(ts=ts, open=o, high=h, low=l, close=c)


def test_bullish_ob_detected_after_displacement():
    base = datetime(2024, 1, 8, 8, 0, tzinfo=timezone.utc)
    bars = []
    # 25 small bodies ~ avg body 0.1
    for i in range(25):
        bars.append(_bar(base + timedelta(minutes=i), 100.0, 100.05, 99.95, 100.0 + (0.05 if i % 2 else -0.05)))
    # bar 25: clear bearish candle (this is our bull OB candidate)
    bars.append(_bar(base + timedelta(minutes=25), 100.0, 100.1, 99.5, 99.6))
    # bar 26: strong bullish displacement (body >> avg)
    bars.append(_bar(base + timedelta(minutes=26), 99.6, 101.5, 99.5, 101.4))
    # some follow-through that does NOT touch the OB
    for i in range(27, 40):
        bars.append(_bar(base + timedelta(minutes=i), 101.4, 101.5, 101.3, 101.4))

    obs = detect_order_blocks(bars)
    bulls = [o for o in obs if o.direction == Direction.BULL]
    assert bulls, "expected a bullish OB"
    ob = bulls[-1]
    # the OB should be the bearish candle just before displacement
    assert ob.low == 99.5
    assert ob.high == 100.1
    assert price_in_ob(99.8, ob)


def test_mitigated_ob_is_dropped():
    base = datetime(2024, 1, 8, 8, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(25):
        bars.append(_bar(base + timedelta(minutes=i), 100.0, 100.05, 99.95, 100.0))
    bars.append(_bar(base + timedelta(minutes=25), 100.0, 100.1, 99.5, 99.6))
    bars.append(_bar(base + timedelta(minutes=26), 99.6, 101.5, 99.5, 101.4))
    # and now close deeply below OB low → mitigated
    bars.append(_bar(base + timedelta(minutes=27), 101.4, 101.5, 98.0, 98.5))

    obs = detect_order_blocks(bars)
    assert all(not (o.direction == Direction.BULL and o.low == 99.5) for o in obs)
