from datetime import datetime, timedelta, timezone

from app.core.types import Bar
from app.data.resampler import OnlineResampler, resample


def _b(ts, o, h, l, c):
    return Bar(ts=ts, open=o, high=h, low=l, close=c, volume=1)


def test_resample_batch():
    base = datetime(2024, 1, 8, 8, 0, tzinfo=timezone.utc)
    # 30 M1 bars → 2 M15 bars
    bars = [_b(base + timedelta(minutes=i), 100, 101, 99, 100.5) for i in range(30)]
    m15 = resample(bars, "M15")
    assert len(m15) == 2
    assert m15[0].ts.minute == 0
    assert m15[1].ts.minute == 15
    # high = max of children
    assert m15[0].high == 101
    assert m15[0].low == 99


def test_online_emits_on_bucket_close():
    base = datetime(2024, 1, 8, 8, 0, tzinfo=timezone.utc)
    r = OnlineResampler("M15")
    emitted = []
    for i in range(17):   # 17 minutes → bucket 08:00 closes at 08:15, we start bucket 08:15
        out = r.add(_b(base + timedelta(minutes=i), 100, 101, 99, 100))
        if out is not None:
            emitted.append(out)
    assert len(emitted) == 1
    assert emitted[0].ts.minute == 0
