"""Historical loader for backtesting.

Supported format: a CSV with columns
    time, open, high, low, close, volume

`time` can be either epoch seconds or ISO-8601 UTC. Other vendor exports are
easy to map. For very large files we use pandas for speed then yield Bar objects.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List

import pandas as pd

from app.core.types import Bar


def _to_utc(v) -> datetime:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(int(v), tz=timezone.utc)
    if isinstance(v, str):
        # pandas handles ISO; normalize tz
        ts = pd.to_datetime(v, utc=True)
        return ts.to_pydatetime()
    if isinstance(v, pd.Timestamp):
        if v.tzinfo is None:
            v = v.tz_localize("UTC")
        return v.tz_convert("UTC").to_pydatetime()
    raise ValueError(f"Unknown timestamp type: {type(v)}")


def load_csv(path: str | Path) -> List[Bar]:
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    required = ["time", "open", "high", "low", "close"]
    for r in required:
        if r not in cols:
            raise ValueError(f"CSV missing required column '{r}' (have {list(df.columns)})")
    df = df.rename(columns={cols[k]: k for k in cols})
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df["time"] = df["time"].apply(_to_utc)
    df = df.sort_values("time").reset_index(drop=True)
    bars: List[Bar] = []
    for _, row in df.iterrows():
        bars.append(Bar(
            ts=row["time"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        ))
    return bars


def iter_bars(bars: List[Bar]) -> Iterator[Bar]:
    for b in bars:
        yield b
