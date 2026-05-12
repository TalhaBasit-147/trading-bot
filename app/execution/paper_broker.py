"""Paper broker for development and paper-trading mode.

Wraps a data feed and maintains an internal equity curve. Fills are simulated
at the next tick/bar with a configurable slippage. Supports SL/TP via price
comparison on each bar it sees.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from loguru import logger

from app.core.types import Bar, Position, Side
from app.execution.base import Broker


# realistic specs — override via constructor for exotic pairs
DEFAULT_SYMBOL_INFO = {
    "XAUUSD":  {"point": 0.01,    "digits": 2, "contract_size": 100,    "tick_value": 1.0,  "spread_points": 20, "min_lot": 0.01, "max_lot": 100, "lot_step": 0.01},
    "EURUSD":  {"point": 0.00001, "digits": 5, "contract_size": 100_000,"tick_value": 1.0,  "spread_points": 10, "min_lot": 0.01, "max_lot": 100, "lot_step": 0.01},
    "GBPUSD":  {"point": 0.00001, "digits": 5, "contract_size": 100_000,"tick_value": 1.0,  "spread_points": 12, "min_lot": 0.01, "max_lot": 100, "lot_step": 0.01},
    "USDJPY":  {"point": 0.001,   "digits": 3, "contract_size": 100_000,"tick_value": 0.9,  "spread_points": 10, "min_lot": 0.01, "max_lot": 100, "lot_step": 0.01},
    "GBPJPY":  {"point": 0.001,   "digits": 3, "contract_size": 100_000,"tick_value": 0.9,  "spread_points": 18, "min_lot": 0.01, "max_lot": 100, "lot_step": 0.01},
    "AUDUSD":  {"point": 0.00001, "digits": 5, "contract_size": 100_000,"tick_value": 1.0,  "spread_points": 12, "min_lot": 0.01, "max_lot": 100, "lot_step": 0.01},
}


@dataclass
class _OpenPos:
    pos: Position
    tp: float
    sl: float


class PaperBroker(Broker):
    def __init__(
        self,
        starting_equity: float = 10_000.0,
        bar_provider=None,          # callable(symbol, timeframe, n) -> List[Bar]
        tick_provider=None,         # callable(symbol) -> dict {bid, ask, time}
        slippage_points: float = 1.0,
        symbol_specs: Optional[dict] = None,
    ):
        self._equity = starting_equity
        self._balance = starting_equity
        self._specs = symbol_specs or DEFAULT_SYMBOL_INFO
        self._positions: Dict[int, _OpenPos] = {}
        self._next_ticket = 1
        self._slippage = slippage_points
        self._bar_provider = bar_provider
        self._tick_provider = tick_provider

    # ---- required overrides ----
    def connect(self) -> bool: return True
    def disconnect(self) -> None: pass
    def account_equity(self) -> float: return self._equity
    def account_balance(self) -> float: return self._balance

    def symbol_info(self, symbol: str) -> dict:
        return dict(self._specs.get(symbol.upper(), self._specs["EURUSD"]))

    def get_bars(self, symbol: str, timeframe: str, n: int) -> List[Bar]:
        if self._bar_provider is None:
            return []
        return self._bar_provider(symbol, timeframe, n)

    def tick(self, symbol: str) -> dict:
        if self._tick_provider is not None:
            return self._tick_provider(symbol)
        # derive from last M1 bar if bar provider present
        bars = self.get_bars(symbol, "M1", 1)
        if not bars:
            return {"bid": 0.0, "ask": 0.0, "time": datetime.now(timezone.utc)}
        last = bars[-1]
        info = self.symbol_info(symbol)
        half = info["spread_points"] * info["point"] / 2
        return {"bid": last.close - half, "ask": last.close + half, "time": last.ts}

    def place_market(self, symbol: str, side: Side, lots: float, sl: float, tp: float, comment: str = "") -> Optional[Position]:
        t = self.tick(symbol)
        info = self.symbol_info(symbol)
        slip = self._slippage * info["point"]
        if side == Side.BUY:
            fill = t["ask"] + slip
        else:
            fill = t["bid"] - slip
        ticket = self._next_ticket
        self._next_ticket += 1
        pos = Position(
            symbol=symbol, side=side, qty=lots, entry=fill, sl=sl, tp=tp,
            open_ts=t["time"], ticket=ticket, comment=comment,
        )
        self._positions[ticket] = _OpenPos(pos=pos, tp=tp, sl=sl)
        logger.info(f"[PAPER] OPEN #{ticket} {side.value} {lots} {symbol} @ {fill:.5f} SL {sl:.5f} TP {tp:.5f}")
        return pos

    def close(self, ticket: int) -> float:
        if ticket not in self._positions:
            return 0.0
        op = self._positions.pop(ticket)
        t = self.tick(op.pos.symbol)
        exit_px = t["bid"] if op.pos.side == Side.BUY else t["ask"]
        pnl = self._pnl(op.pos, exit_px)
        self._equity += pnl
        self._balance += pnl
        logger.info(f"[PAPER] CLOSE #{ticket} @ {exit_px:.5f} pnl={pnl:.2f}")
        return pnl

    def open_positions(self, symbol: Optional[str] = None) -> List[Position]:
        return [op.pos for op in self._positions.values() if symbol is None or op.pos.symbol == symbol]

    # ---- paper-only helper: called by engine each bar to check SL/TP hits ----

    def on_new_bar(self, symbol: str, bar: Bar) -> list[tuple[int, float]]:
        """Returns list of (ticket, realized_pnl) for positions that SL/TP-hit on this bar."""
        closed: list[tuple[int, float]] = []
        for ticket in list(self._positions.keys()):
            op = self._positions[ticket]
            if op.pos.symbol != symbol:
                continue
            hit_price = None
            if op.pos.side == Side.BUY:
                if bar.low <= op.sl:
                    hit_price = op.sl
                elif bar.high >= op.tp:
                    hit_price = op.tp
            else:
                if bar.high >= op.sl:
                    hit_price = op.sl
                elif bar.low <= op.tp:
                    hit_price = op.tp
            if hit_price is not None:
                pnl = self._pnl(op.pos, hit_price)
                self._equity += pnl
                self._balance += pnl
                del self._positions[ticket]
                logger.info(f"[PAPER] HIT #{ticket} @ {hit_price:.5f} pnl={pnl:.2f}")
                closed.append((ticket, pnl))
        return closed

    def _pnl(self, pos: Position, exit_price: float) -> float:
        info = self.symbol_info(pos.symbol)
        point = info["point"]
        points = (exit_price - pos.entry) / point if pos.side == Side.BUY else (pos.entry - exit_price) / point
        return points * info["tick_value"] * pos.qty
