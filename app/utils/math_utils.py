"""Misc math helpers."""
from __future__ import annotations


def round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(round(value / step) * step, 10)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))
