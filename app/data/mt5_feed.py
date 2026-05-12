"""Thin wrapper around MT5Broker for bar streaming in live mode.

The engine polls `last_closed_bar(symbol, timeframe)` once per second and
dedupes by timestamp so the strategy only fires when a new bar has actually
closed on the broker side.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from app.core.types import Bar
from app.execution.base import Broker


class MT5Feed:
    def __init__(self, broker: Broker):
        self.broker = broker
        self._last_seen: Dict[Tuple[str, str], str] = {}  # (symbol, tf) -> iso ts of last completed

    def last_closed_bar(self, symbol: str, timeframe: str) -> Optional[Bar]:
        """Return the most recent *closed* bar if it's new; else None.

        MT5 copy_rates_from_pos(0) includes the current in-progress bar at
        index -1. We therefore return index -2 (last closed).
        """
        bars = self.broker.get_bars(symbol, timeframe, 3)
        if len(bars) < 2:
            return None
        closed = bars[-2]
        key = (symbol, timeframe)
        iso = closed.ts.isoformat()
        if self._last_seen.get(key) == iso:
            return None
        self._last_seen[key] = iso
        return closed

    def recent_bars(self, symbol: str, timeframe: str, n: int) -> list[Bar]:
        bars = self.broker.get_bars(symbol, timeframe, n + 1)
        # drop the unfinished bar
        return bars[:-1] if len(bars) > 0 else []
