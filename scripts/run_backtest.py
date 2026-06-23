"""Backtest both strategies on historical M1 CSV data.

Usage:
    python -m scripts.run_backtest --csv data/XAUUSD_M1_ALL.csv
"""
import sys
sys.path.insert(0, ".")

import argparse
from collections import defaultdict

from app.data.historical import load_csv
from app.execution.paper_broker import DEFAULT_SYMBOL_INFO, PaperBroker
from app.strategy.delayed_orb import DelayedORBStrategy
from app.strategy.prev_day_breakout import PrevDayBreakoutStrategy


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True)
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--equity", type=float, default=1000.0)
    p.add_argument("--risk", type=float, default=0.02)
    args = p.parse_args()

    bars = load_csv(args.csv)
    print(f"Loaded {len(bars)} M1 bars")
    print(f"From: {bars[0].ts}  To: {bars[-1].ts}")
    print(f"Equity: ${args.equity}, Risk: {args.risk*100}%")

    info = DEFAULT_SYMBOL_INFO[args.symbol]

    strategies = {
        "DELAYED_ORB": DelayedORBStrategy(rr=1.2, risk_pct=args.risk),
        "PREV_DAY_BREAKOUT": PrevDayBreakoutStrategy(rr=1.5, risk_pct=args.risk),
    }

    # Separate paper brokers per strategy
    brokers = {name: PaperBroker(args.equity, lambda s,t,n: [], lambda s: {"bid":0,"ask":0,"time":None})
               for name in strategies}
    open_pos = {}  # strategy → meta dict
    trades = {name: [] for name in strategies}
    monthly = {name: defaultdict(lambda: {"w":0,"l":0,"pnl":0.0}) for name in strategies}

    for bar in bars:
        # Check SL/TP fills & EOD closes for each strategy
        for name, strategy in strategies.items():
            broker = brokers[name]
            if name in open_pos:
                meta = open_pos[name]

                # SL/TP simulation
                closed = broker.on_new_bar(args.symbol, bar)
                for ticket, pnl in closed:
                    if ticket != meta["ticket"]:
                        continue
                    r = pnl / meta["risk_ccy"] if meta["risk_ccy"] > 0 else 0
                    outcome = "WIN" if pnl > 0 else "LOSS"
                    eq = broker.account_equity()
                    m = meta["day"][:7]
                    monthly[name][m]["w" if outcome == "WIN" else "l"] += 1
                    monthly[name][m]["pnl"] += pnl
                    emoji = "✅" if outcome == "WIN" else "❌"
                    trades[name].append(
                        f"{emoji} {meta['day']} {meta['side']:4s} "
                        f"entry={meta['entry']:7.2f} risk=${meta['risk_dist']:5.1f} "
                        f"lots={meta['lots']:.2f} pnl=${pnl:+7.2f} eq=${eq:7.2f}"
                    )
                    del open_pos[name]
                    break

                # EOD force close
                if name in open_pos and strategy.should_close_eod(bar):
                    pnl = broker.close(open_pos[name]["ticket"])
                    eq = broker.account_equity()
                    meta = open_pos[name]
                    m = meta["day"][:7]
                    monthly[name][m]["w" if pnl > 0 else "l"] += 1
                    monthly[name][m]["pnl"] += pnl
                    trades[name].append(
                        f"⏰ {meta['day']} {meta['side']:4s} FORCE CLOSE pnl=${pnl:+7.2f} eq=${eq:7.2f}"
                    )
                    del open_pos[name]

        # New signals from each strategy
        for name, strategy in strategies.items():
            broker = brokers[name]
            if name in open_pos:
                continue
            sig = strategy.on_bar(bar, args.symbol)
            if sig is None:
                continue

            lots = strategy.compute_lots(
                equity=broker.account_equity(),
                risk_dist=sig.risk_dist,
                point=info["point"],
                tick_value=info["tick_value"],
            )
            if lots < 0.01:
                continue

            half = info["spread_points"] * info["point"] / 2
            broker._tick_provider = lambda s, e=sig.entry, h=half: {"bid": e-h, "ask": e+h, "time": bar.ts}
            pos = broker.place_market(args.symbol, sig.side, lots, sig.sl, sig.tp, name)
            if pos is None or pos.ticket is None:
                continue

            risk_points = sig.risk_dist / info["point"]
            risk_ccy = risk_points * info["tick_value"] * lots
            open_pos[name] = {
                "ticket": pos.ticket,
                "day": bar.ts.strftime("%Y-%m-%d"),
                "side": sig.side.value,
                "entry": pos.entry,
                "sl": sig.sl, "tp": sig.tp,
                "risk_dist": sig.risk_dist,
                "lots": lots,
                "risk_ccy": max(risk_ccy, 1e-9),
            }

    # Print results
    print("\n" + "="*70)
    print(f"BACKTEST RESULTS — {len(bars)} bars")
    print("="*70)

    for name in strategies:
        equity = brokers[name].account_equity()
        wins = sum(1 for t in trades[name] if "✅" in t)
        losses = sum(1 for t in trades[name] if "❌" in t)
        n = wins + losses
        print(f"\n📊 {name}")
        print(f"  Trades: {n}  Wins: {wins}  Losses: {losses}  WR: {wins/n*100 if n else 0:.1f}%")
        print(f"  $1000 → ${equity:,.2f} ({(equity-args.equity)/args.equity*100:+.1f}%)")
        print(f"  Monthly:")
        for m in sorted(monthly[name].keys()):
            d = monthly[name][m]
            mn = d['w'] + d['l']
            wr = d['w']/mn*100 if mn else 0
            print(f"    {m}: {mn:2d} trades  W={d['w']:2d} L={d['l']:2d}  WR={wr:5.1f}%  PnL=${d['pnl']:+8.2f}")

    # Combined (if both run on same account)
    print(f"\n📊 COMBINED (both strategies)")
    combined_pnl = sum(brokers[n].account_equity() - args.equity for n in strategies)
    print(f"  Total PnL: ${combined_pnl:+,.2f}")
    print(f"  Combined return on $1000: {combined_pnl/args.equity*100:+.1f}%")

    print(f"\n--- DELAYED_ORB TRADE LOG ---")
    for t in trades["DELAYED_ORB"]:
        print(f"  {t}")
    print(f"\n--- PREV_DAY_BREAKOUT TRADE LOG ---")
    for t in trades["PREV_DAY_BREAKOUT"]:
        print(f"  {t}")


if __name__ == "__main__":
    main()
