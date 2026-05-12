"""Generate a synthetic M1 CSV so you can run a backtest immediately.

This is NOT a substitute for real data. For serious validation, export M1
history from MT5 (Tools → History Center → right-click → Export → CSV) and
place it at data/<SYMBOL>_M1.csv.

Usage:
    python scripts/generate_sample_data.py --symbol XAUUSD --days 60 --out data/XAUUSD_M1.csv
"""
from __future__ import annotations

import argparse
import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path


def generate(symbol: str, days: int, start_price: float, seed: int = 7) -> list[tuple]:
    random.seed(seed)
    # Simulate a gold-like process: trending regimes, pullbacks, occasional
    # spikes that create FVGs, and periodic range conditions.
    out = []
    minutes = days * 24 * 60
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(days=days)
    price = start_price
    # regime parameters evolve slowly
    drift = 0.0
    vol = 0.15 if symbol == "XAUUSD" else 0.00008
    for i in range(minutes):
        ts = start + timedelta(minutes=i)
        # change regime every ~500 bars
        if i % 500 == 0:
            drift = random.uniform(-0.02, 0.02) * (1 if symbol == "XAUUSD" else 0.0002 / 0.02)
            vol_mult = random.choice([0.6, 1.0, 1.0, 1.8])
            cur_vol = vol * vol_mult
        # session modulation: more vol in London/NY hours
        h = ts.hour
        session_mult = 1.4 if 7 <= h < 16 else 0.6
        step = random.gauss(drift, cur_vol * session_mult)
        # occasional spike (creates FVGs)
        if random.random() < 0.002:
            step += random.choice([-1, 1]) * cur_vol * 8
        open_ = price
        close = price + step
        hi = max(open_, close) + abs(random.gauss(0, cur_vol * 0.5))
        lo = min(open_, close) - abs(random.gauss(0, cur_vol * 0.5))
        out.append((ts.isoformat(), round(open_, 5), round(hi, 5), round(lo, 5), round(close, 5), random.randint(50, 500)))
        price = close
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--start-price", type=float, default=2000.0)
    p.add_argument("--out", default="data/XAUUSD_M1.csv")
    args = p.parse_args()
    rows = generate(args.symbol, args.days, args.start_price)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        w.writerows(rows)
    print(f"Wrote {len(rows)} M1 bars → {out}")


if __name__ == "__main__":
    main()
