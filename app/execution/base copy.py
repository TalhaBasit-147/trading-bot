"""Broker abstract interface. Implementations: PaperBroker (always available),
MT5Broker (Windows-only, requires MetaTrader5 package + running terminal).

Contract:
  - `connect()` must be idempotent and return True on success.
  - `symbol_info(symbol)` returns a dict with keys:
        point, digits, contract_size, tick_value, spread_points,
        min_lot, max_lot, lot_step
  - `get_bars(symbol, timeframe, n)` returns list[Bar] newest-last.
  - `tick(symbol)` returns dict with bid, ask, time.
  - `place_market(...)` / `place_pending(...)` return a Position with a ticket.
  - `close(ticket)` returns realized pnl in account currency.
  - `open_positions()` returns the current positions for this bot's magic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from app.core.types import Bar, Position, Side


class Broker(ABC):
    @abstractmethod
    def connect(self) -> bool: ...
    @abstractmethod
    def disconnect(self) -> None: ...
    @abstractmethod
    def account_equity(self) -> float: ...
    @abstractmethod
    def account_balance(self) -> float: ...
    @abstractmethod
    def symbol_info(self, symbol: str) -> dict: ...
    @abstractmethod
    def get_bars(self, symbol: str, timeframe: str, n: int) -> List[Bar]: ...
    @abstractmethod
    def tick(self, symbol: str) -> dict: ...
    @abstractmethod
    def place_market(self, symbol: str, side: Side, lots: float, sl: float, tp: float, comment: str = "") -> Optional[Position]: ...
    @abstractmethod
    def close(self, ticket: int) -> float: ...
    @abstractmethod
    def open_positions(self, symbol: Optional[str] = None) -> List[Position]: ...
