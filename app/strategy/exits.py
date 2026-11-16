"""Exit modes for live trades.

fixed
    One position with a broker-side stop and target. The original behaviour.

half_be  ("half off at target, rest to breakeven, ride to end of day")
    The trade is opened as TWO positions on the same signal, each with the
    strategy's stop:
      - the TARGET leg carries the strategy's take-profit at the broker, so it
        banks even if the bot is down;
      - the RUNNER leg has no take-profit.
    When the target leg closes in profit, the runner's stop is moved to the
    runner's own entry price (breakeven). If price is already past that level
    the runner is closed at market instead (a breakeven stop there would be
    invalid and would have been hit anyway). The runner is closed at the
    strategy's end-of-day time (should_close_eod), like any other live trade.
    If the target leg closes at a loss or by hand, an open runner is closed.

    Total size and total risk are unchanged: the two legs add up to the lots the
    normal sizing produced, so every risk guard sees the same order size.

    Nov 2026 backtest (749 PD/PW trades, gold + BTC, Oct 2023 - Nov 2026, real
    spreads, slippage, commission): +0.066R/trade vs the fixed target, 95% CI
    [+0.013, +0.120], better in both halves of the period and on both symbols;
    most of the gain came from a few large trend days.

    Falls back to `fixed` (logged) when:
      - the lots cannot be split into two legs of at least the broker minimum
        (e.g. 0.01 lots);
      - the account is not a hedging account (two positions on a netting
        account would merge into one);
      - the broker cannot manage a split (paper broker).
"""
from __future__ import annotations

from typing import Optional, Tuple

from app.strategy.sizing import floor_to_step

FIXED = "fixed"
HALF_BE = "half_be"
EXIT_MODES = (FIXED, HALF_BE)

_EPS = 1e-9


def normalize_exit_mode(raw: Optional[str]) -> Optional[str]:
    """Lower-cased, stripped mode name, or None if `raw` is not a known mode."""
    if raw is None:
        return None
    m = str(raw).strip().lower()
    return m if m in EXIT_MODES else None


def split_lots(lots: float, min_lot: float = 0.01, lot_step: float = 0.01) -> Optional[Tuple[float, float]]:
    """Split `lots` into (target_leg, runner_leg) that add up exactly to `lots`.

    The runner gets the rounded-down half and the target leg the rest, so with an
    odd number of steps the bigger share is banked at the target. Returns None if
    either leg would be below the broker minimum or the numbers are invalid.
    """
    try:
        lots = float(lots); min_lot = float(min_lot); lot_step = float(lot_step)
    except (TypeError, ValueError):
        return None
    if not (lots > 0 and min_lot > 0 and lot_step > 0):
        return None
    runner = floor_to_step(lots / 2.0, lot_step)
    target = floor_to_step(lots - runner + _EPS, lot_step)
    if runner + _EPS < min_lot or target + _EPS < min_lot:
        return None
    if abs((target + runner) - lots) > lot_step / 2:
        return None
    return target, runner
