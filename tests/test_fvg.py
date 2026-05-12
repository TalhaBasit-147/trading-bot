from datetime import datetime, timedelta, timezone

from app.core.types import Bar, Direction
from app.features.fvg import detect_fvgs, price_in_fvg


def test_fvg_bullish_detected():
    base = datetime(2024, 1, 8, 8, 0, tzinfo=timezone.utc)
    bars = []
    # 20 filler bars around 100
    for i in range(20):
        bars.append(Bar(ts=base + timedelta(minutes=i), open=100, high=100.5, low=99.5, close=100))
    # 3-bar FVG: C0 high=101, C1 strong body, C2 low=102 → gap [101, 102]
    bars.append(Bar(ts=base + timedelta(minutes=20), open=100.5, high=101.0, low=100.3, close=100.8))
    bars.append(Bar(ts=base + timedelta(minutes=21), open=100.8, high=103.5, low=100.7, close=103.2))
    bars.append(Bar(ts=base + timedelta(minutes=22), open=103.2, high=104.0, low=102.0, close=103.8))

    fvgs = detect_fvgs(bars, min_atr_ratio=0.1)
    bulls = [f for f in fvgs if f.direction == Direction.BULL]
    assert bulls, "should have detected at least one bullish FVG"
    g = bulls[-1]
    assert g.low == 101.0
    assert g.high == 102.0
    assert price_in_fvg(101.5, g)


def test_fvg_bearish_detected():
    base = datetime(2024, 1, 8, 8, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(20):
        bars.append(Bar(ts=base + timedelta(minutes=i), open=100, high=100.5, low=99.5, close=100))
    bars.append(Bar(ts=base + timedelta(minutes=20), open=100, high=100.3, low=99.0, close=99.5))
    bars.append(Bar(ts=base + timedelta(minutes=21), open=99.5, high=99.6, low=96.5, close=96.8))
    bars.append(Bar(ts=base + timedelta(minutes=22), open=96.8, high=98.0, low=96.0, close=97.0))

    fvgs = detect_fvgs(bars, min_atr_ratio=0.1)
    bears = [f for f in fvgs if f.direction == Direction.BEAR]
    assert bears
    g = bears[-1]
    # bearish FVG bounds: [c2.high, c0.low] = [98.0, 99.0]
    assert g.low == 98.0
    assert g.high == 99.0
