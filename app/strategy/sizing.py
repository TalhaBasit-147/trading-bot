"""Lot sizing helpers and the last-line order guard.

Sizing rounds DOWN to the broker's lot step, so a trade never risks more than
the configured RISK_PER_TRADE because of rounding (it used to round to the
nearest step: 0.0158 lots became 0.02 and a 2% trade risked 2.53%).

The guard runs on every live order after sizing, whatever the strategy. It
blocks (never shrinks) an order that is larger than the symbol's lot cap or
whose stop would lose more than MAX_TRADE_RISK_PCT of equity. Blocking is
deliberate: an order that size means an input is wrong (a bad fill price,
tick value or stop distance), and a smaller copy of a wrong order is still
wrong. With 1:1000+ account leverage the broker accepts almost any size, so
this is the only net.
"""
from __future__ import annotations

import math
from typing import Optional

_EPS = 1e-9


def floor_to_step(lots: float, step: float = 0.01) -> float:
    """Largest multiple of `step` that is <= lots (float-safe)."""
    if step <= 0:
        step = 0.01
    n = math.floor(lots / step + _EPS)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, decimals + 2)


def risk_lots(equity: float, risk_pct: float, risk_dist: float, point: float, tick_value: float,
              min_lot: float = 0.01, max_lot: float = 1.0, lot_step: float = 0.01) -> float:
    """Lots so the stop loses at most equity * risk_pct, rounded down to the lot step.
    Never below min_lot (the broker's minimum; the order guard decides whether a
    min-lot trade is still acceptable) and never above max_lot."""
    loss_per_lot = (risk_dist / point) * tick_value
    if loss_per_lot <= 0 or equity <= 0:
        return 0.0
    lots = floor_to_step(equity * risk_pct / loss_per_lot, lot_step)
    return min(max_lot, max(min_lot, lots))


def stop_loss_cash(lots: float, risk_dist: float, point: float, tick_value: float) -> float:
    """Account-currency loss if the stop is hit."""
    return (risk_dist / point) * tick_value * lots


def order_block_reason(lots: float, equity: float, risk_dist: float, point: float, tick_value: float,
                       max_lots: float, max_risk_pct: float) -> Optional[str]:
    """None if the order may be sent, otherwise why it must not be."""
    if not (lots > 0 and math.isfinite(lots)):
        return f"invalid lot size {lots}"
    if lots > max_lots + _EPS:
        return f"{lots:.2f} lots exceeds the {max_lots:.2f}-lot cap"
    if not (equity > 0 and math.isfinite(equity)):
        return f"equity unreadable ({equity})"
    loss = stop_loss_cash(lots, risk_dist, point, tick_value)
    if not math.isfinite(loss) or loss <= 0:
        return f"stop-loss cash {loss} is not a valid number"
    if loss > equity * max_risk_pct + _EPS:
        return (f"stop would lose ${loss:,.2f} = {loss / equity * 100:.1f}% of ${equity:,.2f} equity "
                f"(cap {max_risk_pct * 100:.1f}%)")
    return None
