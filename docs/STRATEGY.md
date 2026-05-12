# SMC Strategy — Full Specification

> This doc corresponds to `app/strategy/smc_strategy.py` and the feature
> modules in `app/features/`. When you change the code, update this doc.

## The 30-second summary

On a 15-minute chart: wait for price to sweep obvious liquidity (equal
highs/lows), then push hard with a displacement candle that leaves a fair
value gap. The last opposite-colored candle before the displacement is our
order block. When price comes back into the OB/FVG overlap, switch to the
1-minute chart, wait for a CHoCH, and enter in the direction of the 15-minute
bias. Stop goes beyond the sweep wick. Target is opposite liquidity.
Minimum RR 2.0. London/NY sessions only.

## Timeframes

- **Bias / structure / setups**: M15
- **Entry trigger**: M1
- This combination is the sweet spot for intraday SMC: slow enough that most
  setups aren't noise, fast enough to get 2–4 opportunities per session.

## The nine rules (all must be true)

1. **Session**: current UTC time is inside London (07:00–10:00) or NY
   (12:30–16:00), or their overlap. Asian session is blocked by default.
2. **Spread**: current spread ≤ `MAX_SPREAD_POINTS_XAUUSD` for gold,
   `MAX_SPREAD_POINTS_FOREX` otherwise.
3. **Bias**: M15 bias is defined (from BOS/CHoCH history).
4. **Sweep**: a liquidity pool on the *correct* side was swept recently
   (within the last 50 M15 bars). "Correct side" means:
   - Bullish bias → a **sell-side** pool (low cluster) was swept.
   - Bearish bias → a **buy-side** pool (high cluster) was swept.
5. **Order block**: a fresh OB (≤ 1 prior touch) exists in the bias
   direction, formed at or after the sweep.
6. **FVG**: an unfilled FVG exists in the bias direction, ideally overlapping
   the OB. If overlap exists, we call this an "A+ setup" (score boost).
7. **Price in zone**: the latest M1 close is inside the OB (or OB∩FVG
   overlap when available).
8. **Micro CHoCH**: the M1 stream has broken its most recent swing in the
   desired direction within the last 20 bars.
9. **RR**: computed RR to TP2 ≥ `MIN_RR` (default 2.0).

If all 9 fire, `generate_signal` returns a `Signal` object with entry, SL,
TP1, TP2, a rule-based score in [0, 1], and a feature dict that will be
stored with the trade for the nightly learner.

## Stop loss construction

`SL = beyond(sweep_wick, OB_extreme) ± buffer`

- Long: `SL = min(sweep_wick_low, recent_bar_low, OB.low) - buffer`
- Short: `SL = max(sweep_wick_high, recent_bar_high, OB.high) + buffer`
- `buffer = max(0.1 × ATR, 3 × spread × point)`

The `3 × spread` buffer matters for XAUUSD where raw spreads can be 20+ points
and a tight SL will get stopped on broker noise.

## Take profit construction

- **TP1** = nearest liquidity pool on the opposite side (internal target).
- **TP2** = far pool or, failing that, 2× TP1 distance from entry.
- If no pool targets exist: TP1 = 2R, TP2 = 4R.

The current code exits at TP2 only (one-shot exit). Moving TP1 to 50% partial
close and running TP2 with stop-to-breakeven is a one-file change in
`PaperBroker.on_new_bar` and an equivalent change in the MT5 live path —
follow the TODO in `app/execution/paper_broker.py`.

## Scoring

The rule-based score is:

```
score = 0.3 × min(displacement/3, 1)
      + 0.25 × min(fvg_atr_ratio, 1)
      + 0.20 × (1 if OB∩FVG overlap else 0.25)
      + 0.15 × min((rr - 2)/4, 1) + 0.15
```

The ML model predicts `P(win)` directly; when enabled it gates trades by
`ml_prob ≥ ML_PROB_THRESHOLD`.

## Features persisted per signal

See `app/strategy/smc_strategy.py::_signal_features` — 17 fields including
FVG size (absolute + ATR-normalized), FVG partial fill %, OB displacement
ratio, OB touches, OB width/ATR, sweep strength, SL distance/ATR, ATR,
current spread, counts of pools/OBs/FVGs in the context, session, hour,
weekday, symbol, and bias.

These become training features. **When you add a feature to
`_signal_features`, you must add it to `app/learning/features.py` too,
otherwise the scorer will silently drop it.**

## Why these specific rules?

The core SMC insight: retail stop clusters sit at obvious highs/lows, and
large participants hunt them before committing in the intended direction.
A sweep alone is noise; a sweep + displacement + gap is a footprint of real
participation. The order block is the origin of that participation — the
last candle before the decision shows where the large order was placed.

We require the entry CHoCH on M1 because it eliminates the majority of
"caught a falling knife" entries where the zone is hit but the retrace keeps
going. You give up some R on your stop, but win-rate goes up measurably.

## Anti-overfit guardrails

- Minimum 100 closed trades before the ML filter engages.
- Walk-forward split in training (80/20 time-ordered, never random).
- Drift monitor: if rolling 30-trade win-rate drops > 15% below backtest
  win-rate, engine auto-switches to paper (hook in `Engine._reconcile_positions`).
- Keep rule logic stable. Don't tune parameters per-symbol until you have
  ≥ 200 trades on that symbol.
