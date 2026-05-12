"""Thin repository layer to keep SQLAlchemy usage out of business logic."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

from sqlalchemy import desc, select

from app.core.types import Position
from app.core.types import Signal as DomainSignal
from app.db.base import SessionLocal, engine
from app.db.models import Base, ModelVersion, Signal, Trade


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


# ---- signals ----

def save_signal(sig: DomainSignal, taken: bool, reject_reason: Optional[str] = None) -> int:
    with SessionLocal() as s:
        row = Signal(
            ts=sig.ts, symbol=sig.symbol, side=sig.side.value,
            entry=sig.entry, sl=sig.sl, tp1=sig.tp1, tp2=sig.tp2, rr=sig.rr,
            score=sig.score, ml_prob=sig.ml_prob, taken=1 if taken else 0,
            reject_reason=reject_reason,
            reasons=json.dumps(sig.reasons),
            features=sig.features or {},
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


# ---- trades ----

def open_trade(signal_id: int, pos: Position, features: dict) -> int:
    with SessionLocal() as s:
        row = Trade(
            signal_id=signal_id,
            ticket=pos.ticket,
            symbol=pos.symbol,
            side=pos.side.value,
            qty=pos.qty,
            entry=pos.entry,
            sl=pos.sl,
            tp=pos.tp,
            open_ts=pos.open_ts,
            features=features or {},
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def close_trade(trade_id: int, exit_price: float, close_ts: datetime, pnl_ccy: float, pnl_r: float) -> None:
    outcome = "WIN" if pnl_ccy > 0 else ("LOSS" if pnl_ccy < 0 else "BE")
    with SessionLocal() as s:
        row = s.get(Trade, trade_id)
        if row is None:
            return
        row.exit = exit_price
        row.close_ts = close_ts
        row.pnl_ccy = pnl_ccy
        row.pnl_r = pnl_r
        row.outcome = outcome
        s.commit()


def close_trade_by_ticket(ticket: int, exit_price: float, close_ts: datetime, pnl_ccy: float, pnl_r: float) -> None:
    with SessionLocal() as s:
        row = s.execute(select(Trade).where(Trade.ticket == ticket, Trade.close_ts.is_(None))).scalar_one_or_none()
        if row is None:
            return
        close_trade(row.id, exit_price, close_ts, pnl_ccy, pnl_r)


def recent_closed_trades(limit: int = 500) -> List[Trade]:
    with SessionLocal() as s:
        rows = s.execute(
            select(Trade).where(Trade.close_ts.isnot(None)).order_by(desc(Trade.close_ts)).limit(limit)
        ).scalars().all()
        return list(rows)


def rolling_winrate(n: int = 30) -> Optional[float]:
    rows = recent_closed_trades(n)
    if len(rows) < n:
        return None
    wins = sum(1 for r in rows if r.outcome == "WIN")
    return wins / len(rows)


# ---- model versions ----

def save_model_version(path: str, n_trades: int, auc: Optional[float], brier: Optional[float], notes: str = "") -> None:
    with SessionLocal() as s:
        s.add(ModelVersion(
            trained_at=datetime.now(timezone.utc),
            n_trades=n_trades, auc=auc, brier=brier, path=path, notes=notes,
        ))
        s.commit()
