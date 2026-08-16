"""Synthetic replay test for FVGRetestStrategy's live execution path.

The market is closed while this is being verified (weekend), so this is the
closest thing to an end-to-end check available: a known M1 bar sequence,
built to form a real bullish FVG and then retest its CE level, is replayed
through the real on_bar()/_update_active_fvgs()/_fire_signal() call chain and
must return a correctly-shaped TradeSignal at the exact bar where the retest
completes -- and None on every other bar. Real end-to-end verification (a
live order actually landing on the demo account) can only happen once the
market reopens and the bot is restarted against it.
"""
from datetime import datetime, timedelta, timezone

from app.core.types import Bar, Side
from app.strategy.fvg_retest import FVGRetestStrategy, RR_TARGET, SESSION_START


def _make_m1_bars():
    """One M1 bar per intended M5 candle (single-bar buckets), 5 min apart,
    starting inside the London session window. _update_m5 closes a bucket's
    M5 candle only when the NEXT bucket's first bar arrives, so N candles
    need N+1 bars fed.

    Candle plan (all within 07:00-12:00 UTC):
      0-13 : quiet consolidation (~$1 range each) -> establishes ATR(14)
      14   : c0 -- narrow candle, its low becomes the bullish FVG's SL anchor
      15   : c1 -- impulsive bullish candle (body >> ATR)
      16   : c2 -- gaps away from c0 -> bullish FVG forms here
      17   : retest -- price dips back into the FVG and tags CE
    """
    base = datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    candles = []

    # 0-13: consolidation
    for i in range(14):
        candles.append((4000.0, 4000.5, 3999.5, 4000.0))

    # 14: c0
    candles.append((4000.0, 4000.5, 3999.8, 4000.2))
    # 15: c1 -- impulsive bullish (body=9.8, far above the ~$1 ATR baseline)
    candles.append((4000.2, 4010.2, 4000.1, 4010.0))
    # 16: c2 -- gaps above c0.high(4000.5): low=4008 > 4000.5
    candles.append((4010.0, 4014.0, 4008.0, 4012.0))
    # 17: retest -- dips to tag CE (computed below as 4004.25), stays above
    #     fvg_low(4000.5) so it doesn't invalidate first
    candles.append((4012.0, 4006.0, 4003.0, 4005.0))

    bars = [
        Bar(ts=base + timedelta(minutes=5 * i), open=o, high=h, low=l, close=c, volume=1.0)
        for i, (o, h, l, c) in enumerate(candles)
    ]
    # One extra bar (next bucket) to trigger the close of candle 17.
    bars.append(Bar(ts=base + timedelta(minutes=5 * len(candles)),
                     open=4005.0, high=4005.5, low=4004.5, close=4005.0, volume=1.0))
    return bars


def test_fvg_retest_returns_live_signal_on_the_retest_bar():
    strat = FVGRetestStrategy(rr=RR_TARGET, risk_pct=0.02, paper=False)
    strat._broker_offset = 0  # bars are already in UTC for this test

    bars = _make_m1_bars()
    signals = [strat.on_bar(b, "XAUUSD") for b in bars]

    assert strat.strategy_name == "FVG_RETEST_LIVE"

    non_none = [(i, s) for i, s in enumerate(signals) if s is not None]
    assert len(non_none) == 1, f"expected exactly one signal, got {non_none}"
    idx, sig = non_none[0]
    assert idx == len(bars) - 1, "signal should fire on the bar that closes the retest candle"

    # Shape must match ORB_5MIN/PrevDayBreakoutStrategy's TradeSignal exactly.
    for field in ("ts", "symbol", "side", "entry", "sl", "tp", "risk_dist", "strategy", "note"):
        assert hasattr(sig, field)

    assert sig.symbol == "XAUUSD"
    assert sig.side == Side.BUY
    assert sig.strategy == "FVG_RETEST_LIVE"

    # entry = CE = (fvg_low + fvg_high) / 2 = (4000.5 + 4008.0) / 2
    assert sig.entry == 4004.25
    # sl = c0.low - SL_BUFFER = 3999.8 - 0.5
    assert abs(sig.sl - 3999.3) < 1e-9
    assert abs(sig.risk_dist - (sig.entry - sig.sl)) < 1e-9
    # tp = entry + risk * RR_TARGET(2.5)
    assert abs(sig.tp - (sig.entry + sig.risk_dist * RR_TARGET)) < 1e-9
    assert abs((sig.tp - sig.entry) / sig.risk_dist - 2.5) < 1e-9


def test_paper_mode_never_returns_a_signal_for_the_same_sequence():
    """Sanity check that paper=True still only simulates -- never hands the
    engine a live signal -- for the identical bar sequence."""
    strat = FVGRetestStrategy(rr=RR_TARGET, risk_pct=0.02, paper=True)
    strat._broker_offset = 0

    signals = [strat.on_bar(b, "XAUUSD") for b in _make_m1_bars()]
    assert all(s is None for s in signals)


def test_compute_lots_matches_orb_style_sizing_formula():
    strat = FVGRetestStrategy(rr=RR_TARGET, risk_pct=0.02, paper=False)
    # equity=10_000, risk_pct=0.02 -> risk_cash=200. risk_dist=2.0, point=0.01,
    # tick_value=1.0 -> loss_per_lot = (2.0/0.01)*1.0 = 200. lots = 200/200 = 1.0
    lots = strat.compute_lots(equity=10_000, risk_dist=2.0, point=0.01,
                               tick_value=1.0, min_lot=0.01, max_lot=1.0)
    assert lots == 1.0

    # max_lot must actually clamp (this was missing before the fix).
    lots_capped = strat.compute_lots(equity=10_000, risk_dist=0.5, point=0.01,
                                      tick_value=1.0, min_lot=0.01, max_lot=1.0)
    assert lots_capped == 1.0  # would be 4.0 uncapped; must clamp to max_lot


def test_paper_strategy_compute_lots_is_always_zero():
    strat = FVGRetestStrategy(rr=RR_TARGET, risk_pct=0.02, paper=True)
    assert strat.compute_lots(equity=10_000, risk_dist=2.0) == 0.0
