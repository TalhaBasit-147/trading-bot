"""Rebuild the engine's per-symbol daily state from broker deal history.

The engine keeps 'trades taken today', 'last outcome today' and 'recovery trade
taken' in memory. A restart (reboot, crash, deploy) used to reset them, which
let a symbol take a second trade on a day it had already traded (16 Nov 2026:
an extra gold PREV_DAY trade after a mid-morning restart). At startup the
engine now replays today's deals through the same rules it applies live:

  - a trade OPENS on an entry deal; a half_be runner leg (comment ending in
    RUNNER_SUFFIX) opened right after its target leg is part of that trade;
  - opening a trade while the last outcome is LOSS uses the recovery slot;
  - a trade CLOSES when every position in it has an exit deal; its result is
    the sum of all its exit deals; >= 0 counts as WIN (as _record_close does),
    and a WIN frees the recovery slot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

RUNNER_SUFFIX = "_R"
RUNNER_LINK_WINDOW = timedelta(seconds=60)


@dataclass
class _Trade:
    symbol: str
    opened: datetime
    positions: set = field(default_factory=set)
    closed_positions: set = field(default_factory=set)
    pnl: float = 0.0
    last_exit: Optional[datetime] = None

    @property
    def closed(self) -> bool:
        return bool(self.positions) and self.positions <= self.closed_positions


@dataclass
class SymbolDay:
    trades: int = 0
    last_outcome: Optional[str] = None          # 'WIN' | 'LOSS' | None
    recovery_taken: bool = False
    closes: List[tuple] = field(default_factory=list)   # (exit time, pnl) per closed trade, in order
    open_trades: int = 0


def rebuild_day(deals: List[dict]) -> Dict[str, SymbolDay]:
    """deals: dicts with time, symbol, position, kind ('in'|'out'), comment, pnl."""
    trades: List[_Trade] = []
    by_position: Dict[int, _Trade] = {}
    for d in sorted(deals, key=lambda x: x["time"]):
        pos = d["position"]
        if d["kind"] == "in":
            is_runner = str(d.get("comment") or "").endswith(RUNNER_SUFFIX)
            parent = None
            if is_runner:
                for t in reversed(trades):
                    if t.symbol == d["symbol"] and timedelta(0) <= d["time"] - t.opened <= RUNNER_LINK_WINDOW:
                        parent = t
                        break
            if parent is None:
                parent = _Trade(symbol=d["symbol"], opened=d["time"])
                trades.append(parent)
            parent.positions.add(pos)
            by_position[pos] = parent
        elif d["kind"] == "out":
            t = by_position.get(pos)
            if t is None:          # position opened before today: not today's trade
                continue
            t.pnl += float(d.get("pnl") or 0.0)
            t.closed_positions.add(pos)
            t.last_exit = d["time"]

    events = []
    for t in trades:
        events.append((t.opened, 0, "open", t))
        if t.closed:
            events.append((t.last_exit, 1, "close", t))
    events.sort(key=lambda e: (e[0], e[1]))

    out: Dict[str, SymbolDay] = {}
    for _, _, kind, t in events:
        s = out.setdefault(t.symbol, SymbolDay())
        if kind == "open":
            s.trades += 1
            if s.last_outcome == "LOSS":
                s.recovery_taken = True
        else:
            s.last_outcome = "WIN" if t.pnl >= 0 else "LOSS"
            if s.last_outcome == "WIN":
                s.recovery_taken = False
            s.closes.append((t.last_exit, t.pnl))
    for t in trades:
        if not t.closed:
            out.setdefault(t.symbol, SymbolDay()).open_trades += 1
    return out
