"""Database models. Four tables:

  - signals: every signal we considered (even rejected ones, for analysis)
  - trades:  actual entries/exits with outcomes (training labels)
  - bars:    optional cache for backtest replay
  - model_versions: track ML model artifacts
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(4))     # BUY / SELL
    entry: Mapped[float] = mapped_column(Float)
    sl: Mapped[float] = mapped_column(Float)
    tp1: Mapped[float] = mapped_column(Float)
    tp2: Mapped[float] = mapped_column(Float)
    rr: Mapped[float] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    ml_prob: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    taken: Mapped[int] = mapped_column(Integer, default=0)     # 0/1
    reject_reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    reasons: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    features: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    ticket: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[float] = mapped_column(Float)
    entry: Mapped[float] = mapped_column(Float)
    sl: Mapped[float] = mapped_column(Float)
    tp: Mapped[float] = mapped_column(Float)
    open_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    close_ts: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    exit: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_ccy: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)    # R-multiple
    outcome: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # WIN/LOSS/BE
    features: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class BarCache(Base):
    __tablename__ = "bars"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    timeframe: Mapped[str] = mapped_column(String(4), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=0.0)


class ModelVersion(Base):
    __tablename__ = "model_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trained_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    n_trades: Mapped[int] = mapped_column(Integer)
    auc: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    brier: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    path: Mapped[str] = mapped_column(String(300))
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
