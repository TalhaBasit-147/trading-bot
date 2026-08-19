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


def _make_m1_bars(utc_base=None, broker_offset=0.0):
    """One M1 bar per intended M5 candle (single-bar buckets), 5 min apart,
    starting at utc_base (TRUE UTC). Bar.ts (what the "broker" hands the
    strategy) is shifted forward by broker_offset hours, matching how MT5
    bar timestamps are actually broker-server time, not UTC. _update_m5
    closes a bucket's M5 candle only when the NEXT bucket's first bar
    arrives, so N candles need N+1 bars fed.

    Candle plan (all within [utc_base, utc_base+90min) TRUE UTC):
      0-13 : quiet consolidation (~$1 range each) -> establishes ATR(14)
      14   : c0 -- narrow candle, its low becomes the bullish FVG's SL anchor
      15   : c1 -- impulsive bullish candle (body >> ATR)
      16   : c2 -- gaps away from c0 -> bullish FVG forms here
      17   : retest -- price dips back into the FVG and tags CE
    """
    if utc_base is None:
        utc_base = datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    base = utc_base + timedelta(hours=broker_offset)  # raw bar.ts is BROKER time
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


# ── Timezone fix verification ────────────────────────────────────────────
# Regression tests for the bug where M5Bar.ts / FVG.formed_at carried raw
# broker-server time but were compared against SESSION_START/SESSION_END,
# UTC constants -- silently shifting the traded window by the broker offset.

def test_m5bar_ts_utc_and_ts_broker_differ_by_exactly_the_offset():
    """Direct field-level check: ts_utc must be the true UTC conversion of
    ts_broker, not the same value under two names."""
    offset = 3.0
    strat = FVGRetestStrategy(rr=RR_TARGET, risk_pct=0.02, paper=False)
    strat._broker_offset = offset

    utc_base = datetime(2026, 6, 1, 7, 0)
    for b in _make_m1_bars(utc_base=utc_base, broker_offset=offset):
        strat.on_bar(b, "XAUUSD")

    # First M5 candle (candle 0) is anchored at utc_base in true UTC, and
    # utc_base+offset in broker time -- exactly the shift the bug lost.
    m5 = strat._m5_history[0]
    assert m5.ts_utc == utc_base
    assert m5.ts_broker == utc_base + timedelta(hours=offset)
    assert m5.ts_broker - m5.ts_utc == timedelta(hours=offset)

    # c2 (candle 16, at utc_base + 80min = 08:20 UTC) is what the FVG's
    # formed_at_utc must carry -- true UTC, not the +3h broker value.
    assert len(strat._active_fvgs) == 1
    fvg = strat._active_fvgs[0]
    assert fvg.formed_at_utc == utc_base + timedelta(minutes=80)


def test_session_gate_rejects_when_true_utc_is_out_of_session_even_if_broker_time_is_in_window():
    """The bug this catches: utc_base=05:00 (OUTSIDE the 07:00-12:00 UTC
    session) with a +3 broker offset means bar.ts (raw broker time) lands at
    08:00-09:xx, which falls INSIDE [07:00, 12:00) if that window is
    (wrongly) treated as broker time. Before the fix this would have been
    admitted; after the fix it must be rejected because true UTC (05:00ish)
    is outside the session."""
    strat = FVGRetestStrategy(rr=RR_TARGET, risk_pct=0.02, paper=False)
    strat._broker_offset = 3.0

    utc_base = datetime(2026, 6, 1, 5, 0)  # true UTC, outside session
    signals = [strat.on_bar(b, "XAUUSD")
               for b in _make_m1_bars(utc_base=utc_base, broker_offset=3.0)]

    assert all(s is None for s in signals)
    assert strat._active_fvgs == [], "FVG must not be admitted when its true UTC formation time is out of session"


def test_session_gate_admits_when_true_utc_is_in_session_even_if_broker_time_is_out_of_window():
    """The complementary bug direction: utc_base=10:00 (INSIDE the
    07:00-12:00 UTC session) with a +3 broker offset means bar.ts lands at
    13:00-14:xx broker time, OUTSIDE [07:00, 12:00) broker-clock hours. Before
    the fix this would have been wrongly REJECTED; after the fix it must fire
    because true UTC (10:00-11:xx) is inside the session."""
    strat = FVGRetestStrategy(rr=RR_TARGET, risk_pct=0.02, paper=False)
    strat._broker_offset = 3.0

    utc_base = datetime(2026, 6, 1, 10, 0)  # true UTC, inside session; ends ~11:30 UTC
    signals = [strat.on_bar(b, "XAUUSD")
               for b in _make_m1_bars(utc_base=utc_base, broker_offset=3.0)]

    non_none = [s for s in signals if s is not None]
    assert len(non_none) == 1, "FVG must be admitted and fire when its true UTC formation time is in session"


def test_session_gate_behaves_sanely_with_unconfirmed_fallback_offset():
    """When _broker_offset was never set at all (the getattr(..., 3) default
    path -- what happens if set_broker() hasn't run / offset detection never
    confirmed anything), the fix must not crash and must still gate
    consistently on whatever offset it ends up using."""
    strat = FVGRetestStrategy(rr=RR_TARGET, risk_pct=0.02, paper=False)
    assert not hasattr(strat, "_broker_offset")  # simulates the unconfirmed-fallback path

    utc_base = datetime(2026, 6, 1, 7, 0)
    # bars generated assuming the same fallback (3) the strategy will use via getattr
    signals = [strat.on_bar(b, "XAUUSD")
               for b in _make_m1_bars(utc_base=utc_base, broker_offset=3.0)]

    non_none = [s for s in signals if s is not None]
    assert len(non_none) == 1  # doesn't crash, and gates consistently with the fallback
