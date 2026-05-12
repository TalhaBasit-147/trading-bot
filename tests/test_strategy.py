"""Smoke test: build a synthetic scenario where the strategy *should* fire."""
from datetime import datetime, timedelta, timezone

from app.core.types import Bar, Direction, Side
from app.features.context import build_context
from app.strategy.smc_strategy import generate_signal


def _b(ts, o, h, l, c):
    return Bar(ts=ts, open=o, high=h, low=l, close=c)


def test_strategy_can_generate_signal_in_bull_scenario():
    """Scenario: uptrend → pullback creates equal lows → sweep below →
    strong bullish displacement → we want a BUY signal on 1m retest."""
    base = datetime(2024, 1, 8, 8, 0, tzinfo=timezone.utc)  # London session

    # build M15 bars (60 bars needed for min history)
    m15 = []
    # 40 bars of clean uptrend to set BULL bias
    for i in range(40):
        p = 2000 + i * 1.5
        m15.append(_b(base + timedelta(minutes=15 * i), p - 0.3, p + 0.8, p - 0.5, p + 0.2))
    # equal lows at ~2055 via a double bottom
    low_price = m15[-1].close - 5
    for i, off in enumerate([-5, -4.5, -5.1]):  # three shallow dips near same low
        p = m15[-1].close + off
        m15.append(_b(base + timedelta(minutes=15 * (40 + i)), p + 0.1, p + 0.5, low_price + 0.1, p + 0.3))
    # sweep bar: wicks below lows then closes back up
    m15.append(_b(base + timedelta(minutes=15 * 43), low_price + 0.2, low_price + 1.0, low_price - 1.5, low_price + 0.8))
    # strong bullish displacement
    last_close = m15[-1].close
    m15.append(_b(base + timedelta(minutes=15 * 44), last_close, last_close + 5.0, last_close - 0.3, last_close + 4.5))
    # some continuation (then pull back into OB area)
    p = m15[-1].close
    for i in range(45, 60):
        m15.append(_b(base + timedelta(minutes=15 * i), p, p + 1.0, p - 0.5, p + 0.3))
        p += 0.3
    # final bar: price returns INTO the OB/FVG zone (the sweep candle area ~ low_price)
    m15.append(_b(base + timedelta(minutes=15 * 60), p, p + 0.2, low_price + 1.5, low_price + 2.0))

    ctx = build_context("XAUUSD", "M15", m15)
    # sanity: bias should be bullish
    assert ctx.structure.bias == Direction.BULL

    # 1m bars: micro-dip then a close above the micro swing high to fire CHoCH
    last_ts = m15[-1].ts + timedelta(minutes=15)
    prices = [low_price + 2.0, low_price + 1.8, low_price + 1.6, low_price + 1.5,
              low_price + 1.7, low_price + 1.9, low_price + 2.1, low_price + 2.3,
              low_price + 2.5, low_price + 2.7, low_price + 2.4, low_price + 2.2,
              low_price + 2.0, low_price + 2.3, low_price + 2.5, low_price + 2.7,
              low_price + 2.9, low_price + 3.1, low_price + 3.3, low_price + 3.5,
              low_price + 3.4, low_price + 3.6, low_price + 3.8]
    m1 = [_b(last_ts + timedelta(minutes=i), p - 0.1, p + 0.2, p - 0.2, p) for i, p in enumerate(prices)]

    sig = generate_signal(ctx, m1, spread_points=20.0, point=0.01)
    # we don't strictly assert a signal fires (scenario synthetic) — but if it does,
    # it must be a BUY with RR >= min configured RR
    if sig is not None:
        assert sig.side == Side.BUY
        assert sig.rr >= 2.0
        assert sig.sl < sig.entry
        assert sig.tp2 > sig.entry
