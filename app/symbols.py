"""Per-symbol trading configuration for the multi-symbol engine.

The engine watches every symbol in TRADE_SYMBOLS on its own M1 chart and runs
its own copy of each live strategy there; whichever symbol's setup triggers is
traded. Adding a symbol is an env change:

    TRADE_SYMBOLS=XAUUSD,BTCUSD
    BTCUSD_PRICE_SCALE=69.23          # required for every non-primary symbol
    BTCUSD_STRATEGIES=PREV_DAY,PREV_WEEK
    BTCUSD_RISK_PER_TRADE=0.02
    BTCUSD_RR_PREV_DAY=1.2
    BTCUSD_RR_PREV_WEEK=1.5
    BTCUSD_MAX_TRADES_PER_DAY=1
    BTCUSD_MAX_LOTS=0.10              # order guard: refuse anything bigger

PRICE_SCALE converts the strategies' gold-dollar constants (minimum/maximum
stop distance, entry buffer, overshoot guard) into the symbol's own price
units. It is the symbol's typical daily range divided by gold's (e.g. 69.23
for BTCUSD from the Nov 2026 research). It has no default for non-primary
symbols on purpose: silently applying gold's $15 stop cap to BTC or EURUSD
would produce nonsense stops.

The primary symbol defaults to the global settings (RISK_PER_TRADE, RR_TARGET
for PREV_DAY, 1.5 for PREV_WEEK, MAX_TRADES_PER_DAY, scale 1.0), so a
gold-only setup behaves exactly as before.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from app.config import settings

KNOWN_STRATEGIES: Tuple[str, ...] = ("PREV_DAY", "PREV_WEEK")
PREV_WEEK_DEFAULT_RR = 1.5


class SymbolConfigError(ValueError):
    """Invalid multi-symbol configuration. Raised at startup so a bad .env can never trade."""


@dataclass(frozen=True)
class SymbolConfig:
    symbol: str
    strategies: Tuple[str, ...]
    price_scale: float
    risk_per_trade: float
    rr_prev_day: float
    rr_prev_week: float
    max_trades_per_day: int
    max_lots: float = 0.10


def _env_source(env: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """Per-symbol keys are dynamic, so pydantic can't declare them. Read them from
    the same .env file settings uses, with real environment variables winning."""
    if env is not None:
        return {k.upper(): v for k, v in env.items() if v is not None}
    merged: Dict[str, str] = {}
    try:
        from dotenv import dotenv_values
        merged.update({k.upper(): v for k, v in dotenv_values(os.environ.get("ENV_FILE", ".env")).items() if v is not None})
    except Exception:
        pass
    merged.update({k.upper(): v for k, v in os.environ.items()})
    return merged


def _num(raw: Optional[str], key: str, cast):
    try:
        return cast(raw)
    except (TypeError, ValueError):
        raise SymbolConfigError(f"{key}={raw!r} is not a valid number") from None


def load_symbol_configs(env: Optional[Mapping[str, str]] = None) -> List[SymbolConfig]:
    """Build one SymbolConfig per traded symbol. `env` is for tests; by default the
    .env file + process environment are read."""
    src = _env_source(env)
    primary = (src.get("PRIMARY_SYMBOL") if env is not None and src.get("PRIMARY_SYMBOL") else settings.PRIMARY_SYMBOL).strip().upper()
    raw = src.get("TRADE_SYMBOLS", "") if env is not None else settings.TRADE_SYMBOLS
    symbols = [s.strip().upper() for s in (raw or "").split(",") if s.strip()] or [primary]

    if len(set(symbols)) != len(symbols):
        raise SymbolConfigError(f"TRADE_SYMBOLS lists a symbol twice: {raw}")
    if primary not in symbols:
        raise SymbolConfigError(f"TRADE_SYMBOLS must include PRIMARY_SYMBOL ({primary}); got {symbols}")

    out: List[SymbolConfig] = []
    for sym in symbols:
        def get(key: str) -> Optional[str]:
            v = src.get(f"{sym}_{key}")
            return v.strip() if isinstance(v, str) and v.strip() else None

        scale_raw = get("PRICE_SCALE")
        if scale_raw is None:
            if sym != primary:
                raise SymbolConfigError(
                    f"{sym}_PRICE_SCALE is required: the strategies' stop/buffer constants are in gold "
                    f"dollars and must be scaled to {sym}'s price units")
            scale = 1.0
        else:
            scale = _num(scale_raw, f"{sym}_PRICE_SCALE", float)
        if scale <= 0:
            raise SymbolConfigError(f"{sym}_PRICE_SCALE must be > 0 (got {scale})")

        strat_raw = get("STRATEGIES")
        strategies = tuple(s.strip().upper() for s in strat_raw.split(",") if s.strip()) if strat_raw else KNOWN_STRATEGIES
        unknown = [s for s in strategies if s not in KNOWN_STRATEGIES]
        if unknown or not strategies:
            raise SymbolConfigError(f"{sym}_STRATEGIES={strat_raw!r}: use a comma list of {', '.join(KNOWN_STRATEGIES)}")

        risk = _num(get("RISK_PER_TRADE"), f"{sym}_RISK_PER_TRADE", float) if get("RISK_PER_TRADE") else settings.RISK_PER_TRADE
        if not (0 < risk <= 0.5):
            raise SymbolConfigError(f"{sym}_RISK_PER_TRADE must be in (0, 0.5] (got {risk})")
        rr_pd = _num(get("RR_PREV_DAY"), f"{sym}_RR_PREV_DAY", float) if get("RR_PREV_DAY") else settings.RR_TARGET
        rr_pw = _num(get("RR_PREV_WEEK"), f"{sym}_RR_PREV_WEEK", float) if get("RR_PREV_WEEK") else PREV_WEEK_DEFAULT_RR
        if rr_pd <= 0 or rr_pw <= 0:
            raise SymbolConfigError(f"{sym} RR values must be > 0")
        cap = _num(get("MAX_TRADES_PER_DAY"), f"{sym}_MAX_TRADES_PER_DAY", int) if get("MAX_TRADES_PER_DAY") else settings.MAX_TRADES_PER_DAY
        if cap < 1:
            raise SymbolConfigError(f"{sym}_MAX_TRADES_PER_DAY must be >= 1 (got {cap})")

        max_lots = _num(get("MAX_LOTS"), f"{sym}_MAX_LOTS", float) if get("MAX_LOTS") else settings.MAX_LOTS
        if not (0 < max_lots <= 100):
            raise SymbolConfigError(f"{sym}_MAX_LOTS must be in (0, 100] (got {max_lots})")

        out.append(SymbolConfig(sym, strategies, scale, risk, rr_pd, rr_pw, cap, max_lots))
    return out
