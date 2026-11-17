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

    def can_hold_split_positions(self) -> bool:
        """True if this broker can hold two separate positions on one symbol in
        the same direction and manage them independently (an MT5 *hedging*
        account). Used by the half_be exit mode, which falls back to a single
        fixed-target position when this is False. Default: False (safe)."""
        return False

    def wrong_account_reason(self) -> Optional[str]:
        """None if this broker is connected to the account the bot is configured
        for, else a reason not to trade. Default: no expectation (None)."""
        return None

    def deals_today(self, symbols: List[str]) -> List[dict]:
        """This bot's deals since the start of the current broker day, oldest first:
        dicts with time (broker clock, naive), symbol, position, kind ('in'|'out'),
        comment, pnl. Used to rebuild daily state after a restart. Default: []."""
        return []

    def realized_pnl(self, ticket: int) -> Optional[float]:
        """Realized net P/L for a closed position from broker history.

        Optional capability. Default returns None ("unknown"); brokers that can
        read true closed-deal profit (MT5) override this. Callers must treat
        None as "fall back to another source", never as $0.00.
        """
        return None
