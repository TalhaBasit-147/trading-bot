"""Backtest the Delayed ORB strategy on historical M1 CSV data.

Usage:
    python -m scripts.run_backtest --csv data/XAUUSD_M1_ALL.csv
    python -m scripts.run_backtest --csv data/XAUUSD_M1_ALL.csv --rr 1.0 --risk 0.03
"""
import sys
sys.path.insert(0, ".")

import argparse
import json
from pathlib import Path

from loguru import logger

from app.data.historical import load_csv
from app.execution.paper_broker import DEFAULT_SYMBOL_INFO, PaperBroker
from app.strategy.delayed_orb import DelayedORBStrategy


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True)
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--equity", type=float, default=1000.0)
    p.add_argument("--rr", type=float, default=1.2)
    p.add_argument("--risk", type=float, default=0.02, help="Risk per trade as decimal (0.02 = 2%)")
    args = p.parse_args()

    bars = load_csv(args.csv)
    print(f"Loaded {len(bars)} M1 bars")
    print(f"From: {bars[0].ts} To: {bars[-1].ts}")
    print(f"Config: RR={args.rr}, Risk={args.risk*100}%, Equity=${args.equity}")

    info = DEFAULT_SYMBOL_INFO[args.symbol]
    strategy = DelayedORBStrategy(rr=args.rr, risk_pct=args.risk)

    equity = args.equity
    max_equity = equity
    max_dd = 0
    trades = []
    open_ticket = None
    open_meta = {}
    monthly = {}

    # Simple paper broker for SL/TP simulation
    def bar_provider(sym, tf, n): return []
    def tick_provider(sym):
        return {"bid": 0, "ask": 0, "time": None}
    broker = PaperBroker(equity, bar_provider, tick_provider)

    for bar in bars:
        # Check SL/TP on open position
        if open_ticket is not None:
            closed = broker.on_new_bar(args.symbol, bar)
            for ticket, pnl in closed:
                if ticket == open_ticket:
                    meta = open_meta
                    r = pnl / meta["risk_ccy"] if meta["risk_ccy"] > 0 else 0
                    outcome = "WIN" if pnl > 0 else "LOSS"
                    equity = broker.account_equity()
                    max_equity = max(max_equity, equity)
                    max_dd = min(max_dd, (equity - max_equity) / max_equity)
                    month = meta["day"][:7]
                    monthly.setdefault(month, {"w": 0, "l": 0, "pnl": 0})
                    monthly[month]["pnl"] += pnl
                    monthly[month]["w" if outcome == "WIN" else "l"] += 1
                    emoji = "✅" if outcome == "WIN" else "❌"
                    trades.append(f"{emoji} {meta['day']} {meta['side']:4s} "
                                  f"entry={meta['entry']:7.1f} sl={meta['sl']:7.1f} tp={meta['tp']:7.1f} "
                                  f"risk=${meta['risk_dist']:5.1f} lots={meta['lots']:.2f} "
                                  f"pnl=${pnl:+7.1f} eq=${equity:7.1f}")
                    open_ticket = None
                    open_meta = {}

            # Force close at EOD
            if open_ticket is not None and strategy.should_close_eod(bar):
                pnl = broker.close(open_ticket)
                equity = broker.account_equity()
                max_equity = max(max_equity, equity)
                max_dd = min(max_dd, (equity - max_equity) / max_equity)
                meta = open_meta
                month = meta["day"][:7]
                monthly.setdefault(month, {"w": 0, "l": 0, "pnl": 0})
                monthly[month]["pnl"] += pnl
                monthly[month]["w" if pnl > 0 else "l"] += 1
                trades.append(f"⏰ {meta['day']} {meta['side']:4s} FORCE CLOSE pnl=${pnl:+7.1f} eq=${equity:7.1f}")
                open_ticket = None
                open_meta = {}

        # Skip if already in position
        if open_ticket is not None:
            continue

        # Feed bar to strategy
        sig = strategy.on_bar(bar, args.symbol)
        if sig is None:
            continue

        # Size and execute
        lots = strategy.compute_lots(
            equity=broker.account_equity(),
            risk_dist=sig.risk_dist,
            point=info["point"],
            tick_value=info["tick_value"],
        )
        if lots < 0.01:
            continue

        # Update broker tick for fill price
        half_spread = info["spread_points"] * info["point"] / 2
        broker._tick_provider = lambda sym, e=sig.entry, hs=half_spread: {
            "bid": e - hs, "ask": e + hs, "time": bar.ts
        }

        pos = broker.place_market(args.symbol, sig.side, lots, sig.sl, sig.tp, "ORB")
        if pos is None or pos.ticket is None:
            continue

        risk_points = sig.risk_dist / info["point"]
        risk_ccy = risk_points * info["tick_value"] * lots
        open_ticket = pos.ticket
        open_meta = {
            "day": bar.ts.strftime("%Y-%m-%d"),
            "side": sig.side.value,
            "entry": pos.entry,
            "sl": sig.sl,
            "tp": sig.tp,
            "risk_dist": sig.risk_dist,
            "lots": lots,
            "risk_ccy": max(risk_ccy, 1e-9),
        }

    # Results
    wins = sum(1 for t in trades if "✅" in t)
    losses = sum(1 for t in trades if "❌" in t)
    n = wins + losses
    equity = broker.account_equity()

    print(f"\n{'='*70}")
    print(f"BACKTEST RESULTS — Delayed ORB, RR={args.rr}, Risk={args.risk*100}%")
    print(f"{'='*70}")
    print(f"Starting equity:  ${args.equity:,.2f}")
    print(f"Final equity:     ${equity:,.2f}")
    print(f"Return:           {(equity - args.equity) / args.equity * 100:+.2f}%")
    print(f"Max drawdown:     {max_dd * 100:.2f}%")
    print(f"Total trades:     {n}")
    print(f"Wins: {wins}  Losses: {losses}  Win Rate: {wins/n*100:.1f}%" if n > 0 else "No trades")

    print(f"\n--- MONTHLY ---")
    for month in sorted(monthly.keys()):
        m = monthly[month]
        mn = m['w'] + m['l']
        wr = m['w'] / mn * 100 if mn > 0 else 0
        print(f"  {month}: trades={mn:2d}  W={m['w']:2d} L={m['l']:2d}  WR={wr:5.1f}%  PnL=${m['pnl']:+8.1f}")

    print(f"\n--- TRADE LOG ---")
    for t in trades:
        print(f"  {t}")

    # Save report
    out = Path("reports/backtest.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "symbol": args.symbol, "rr": args.rr, "risk_pct": args.risk,
        "trades": n, "wins": wins, "losses": losses,
        "win_rate": wins / n if n > 0 else 0,
        "return_pct": (equity - args.equity) / args.equity * 100,
        "max_drawdown": max_dd * 100,
        "final_equity": equity,
        "monthly": monthly,
    }, indent=2))
    print(f"\nReport: {out}")


if __name__ == "__main__":
    main()
