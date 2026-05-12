"""FastAPI control plane.

Endpoints (all except /health require `X-API-Token: <API_TOKEN>`):
  GET  /health          → basic status
  GET  /status          → equity, daily pnl, open positions, pause state
  POST /pause           → pause trading
  POST /resume          → resume trading
  GET  /trades?limit=50 → recent closed trades
  POST /reload_model    → reload ML model from disk

The engine shares a singleton `EngineState` with the API via `state.py`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from loguru import logger

from app.api.state import engine_state
from app.config import settings
from app.db.repo import recent_closed_trades


def _auth(x_api_token: str | None = Header(default=None)) -> None:
    if not x_api_token or x_api_token != settings.API_TOKEN:
        raise HTTPException(status_code=401, detail="unauthorized")


def build_app() -> FastAPI:
    app = FastAPI(title="SMC Bot Control", version="1.0")

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "time": datetime.utcnow().isoformat() + "Z",
            "mode": settings.MODE,
        }

    @app.get("/status", dependencies=[Depends(_auth)])
    def status():
        r = engine_state.risk
        if r is None:
            return {"running": False}
        return {
            "running": engine_state.running,
            "paused": r.state.paused,
            "pause_reason": r.state.pause_reason,
            "equity": r.state.equity,
            "daily_pnl": r.state.daily.pnl_ccy,
            "daily_trades": r.state.daily.trades,
            "open_positions": r.state.open_positions,
            "mode": settings.MODE,
            "symbols": settings.symbols_list,
        }

    @app.post("/pause", dependencies=[Depends(_auth)])
    def pause(reason: str = "manual"):
        if engine_state.risk:
            engine_state.risk.pause(reason)
        return {"ok": True}

    @app.post("/resume", dependencies=[Depends(_auth)])
    def resume():
        if engine_state.risk:
            engine_state.risk.resume()
        return {"ok": True}

    @app.get("/trades", dependencies=[Depends(_auth)])
    def trades(limit: int = 50):
        rows = recent_closed_trades(limit)
        return [
            {
                "id": t.id, "symbol": t.symbol, "side": t.side,
                "entry": t.entry, "exit": t.exit, "qty": t.qty,
                "pnl_ccy": t.pnl_ccy, "pnl_r": t.pnl_r,
                "outcome": t.outcome,
                "open_ts": t.open_ts.isoformat() if t.open_ts else None,
                "close_ts": t.close_ts.isoformat() if t.close_ts else None,
            }
            for t in rows
        ]

    @app.post("/reload_model", dependencies=[Depends(_auth)])
    def reload_model():
        if engine_state.scorer:
            engine_state.scorer.reload()
            return {"ok": True, "available": engine_state.scorer.available()}
        return {"ok": False}

    return app


app = build_app()
