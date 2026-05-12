"""Backtest performance metrics."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

import numpy as np


@dataclass
class TradeRecord:
    symbol: str
    side: str
    entry: float
    exit: float
    sl: float
    tp: float
    pnl_ccy: float
    pnl_r: float
    open_ts: str
    close_ts: str
    session: str = ""
    setup_kind: str = ""


@dataclass
class Metrics:
    n_trades: int
    win_rate: float
    avg_r: float
    expectancy_r: float
    profit_factor: float
    max_drawdown: float
    total_pnl_ccy: float
    per_session: dict
    per_symbol: dict


def compute(trades: List[TradeRecord], starting_equity: float = 10_000.0) -> Metrics:
    n = len(trades)
    if n == 0:
        return Metrics(0, 0, 0, 0, 0, 0, 0, {}, {})
    wins = [t for t in trades if t.pnl_ccy > 0]
    losses = [t for t in trades if t.pnl_ccy < 0]
    win_rate = len(wins) / n
    avg_r = float(np.mean([t.pnl_r for t in trades]))
    sum_win = sum(t.pnl_ccy for t in wins)
    sum_loss = abs(sum(t.pnl_ccy for t in losses))
    pf = sum_win / sum_loss if sum_loss > 0 else float("inf")
    # expectancy
    avg_win_r = float(np.mean([t.pnl_r for t in wins])) if wins else 0.0
    avg_loss_r = float(np.mean([t.pnl_r for t in losses])) if losses else 0.0
    expectancy = win_rate * avg_win_r + (1 - win_rate) * avg_loss_r

    # equity curve & max DD
    equity = [starting_equity]
    for t in trades:
        equity.append(equity[-1] + t.pnl_ccy)
    eq = np.array(equity)
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    max_dd = float(dd.min()) if len(dd) else 0.0

    # per-session / per-symbol breakdowns
    def _group(key_fn):
        out = {}
        for t in trades:
            k = key_fn(t)
            out.setdefault(k, []).append(t)
        return {k: {"n": len(v), "win_rate": sum(1 for x in v if x.pnl_ccy > 0) / len(v),
                    "pnl_ccy": sum(x.pnl_ccy for x in v), "avg_r": float(np.mean([x.pnl_r for x in v]))}
                for k, v in out.items()}

    return Metrics(
        n_trades=n,
        win_rate=win_rate,
        avg_r=avg_r,
        expectancy_r=expectancy,
        profit_factor=pf,
        max_drawdown=max_dd,
        total_pnl_ccy=float(eq[-1] - starting_equity),
        per_session=_group(lambda t: t.session or "UNK"),
        per_symbol=_group(lambda t: t.symbol),
    )


def format_report(m: Metrics) -> str:
    lines = [
        "── Backtest metrics ──",
        f"Trades:          {m.n_trades}",
        f"Win rate:        {m.win_rate*100:.2f}%",
        f"Avg R:           {m.avg_r:+.3f}",
        f"Expectancy (R):  {m.expectancy_r:+.3f}",
        f"Profit factor:   {m.profit_factor:.2f}",
        f"Max drawdown:    {m.max_drawdown*100:.2f}%",
        f"Net PnL (ccy):   {m.total_pnl_ccy:+.2f}",
        "",
        "Per session:",
    ]
    for k, v in m.per_session.items():
        lines.append(f"  {k:<8} n={v['n']:<4} wr={v['win_rate']*100:5.1f}% avgR={v['avg_r']:+.2f} pnl={v['pnl_ccy']:+.2f}")
    lines.append("Per symbol:")
    for k, v in m.per_symbol.items():
        lines.append(f"  {k:<8} n={v['n']:<4} wr={v['win_rate']*100:5.1f}% avgR={v['avg_r']:+.2f} pnl={v['pnl_ccy']:+.2f}")
    return "\n".join(lines)
