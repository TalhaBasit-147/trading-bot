"""Feature vector spec used for both training and live inference.

Having a single definition here guarantees that the model trained last night
sees the same column names/order at inference time.
"""
from __future__ import annotations

from typing import List

# These match keys produced by `smc_strategy._signal_features`.
# If you add features there, add them here too (and retrain).
NUMERIC_FEATURES: List[str] = [
    "fvg_size",
    "fvg_atr_ratio",
    "fvg_partial_fill",
    "ob_displacement",
    "ob_touched",
    "ob_width_atr",
    "sweep_strength",
    "distance_entry_sl_atr",
    "atr",
    "spread_points",
    "pool_count",
    "ob_count",
    "fvg_count",
    "hour_utc",
    "weekday",
]

CATEGORICAL_FEATURES: List[str] = [
    "symbol",
    "session",
    "bias",
]

ALL_FEATURES: List[str] = NUMERIC_FEATURES + CATEGORICAL_FEATURES


def feature_dict_to_row(d: dict) -> dict:
    """Keep only known keys, fill missing with sensible defaults."""
    out = {k: d.get(k, 0) for k in NUMERIC_FEATURES}
    for k in CATEGORICAL_FEATURES:
        out[k] = d.get(k, "UNK")
    return out
