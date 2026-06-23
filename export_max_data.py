"""Export maximum available M1 history from MT5 for XAUUSD.

Pulls in monthly chunks going backwards until no more data is available,
so we get the broker's full history (often 1-2 years for IC Markets).

Usage:
    .venv\\Scripts\\activate
    python export_max_data.py
"""
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta, timezone

SYMBOL = "XAUUSD"
OUTPUT = "data/XAUUSD_M1_FULL.csv"

def main():
    if not mt5.initialize():
        print(f"initialize failed: {mt5.last_error()}")
        return

    mt5.symbol_select(SYMBOL, True)

    # Pull in 30-day chunks going backwards from now
    all_rates = []
    end = datetime.now(timezone.utc)
    chunk_days = 30
    max_chunks = 36  # up to ~3 years

    for i in range(max_chunks):
        start = end - timedelta(days=chunk_days)
        rates = mt5.copy_rates_range(SYMBOL, mt5.TIMEFRAME_M1, start, end)
        if rates is None or len(rates) == 0:
            print(f"Chunk {i+1}: no data for {start.date()} to {end.date()} — stopping")
            break
        all_rates.append(pd.DataFrame(rates))
        print(f"Chunk {i+1}: {len(rates):>6} bars  {start.date()} to {end.date()}")
        end = start

    mt5.shutdown()

    if not all_rates:
        print("No data retrieved.")
        return

    df = pd.concat(all_rates, ignore_index=True)
    df = df.drop_duplicates(subset='time').sort_values('time').reset_index(drop=True)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = df[['time', 'open', 'high', 'low', 'close', 'tick_volume']]
    df.columns = ['time', 'open', 'high', 'low', 'close', 'volume']

    import os
    os.makedirs("data", exist_ok=True)
    df.to_csv(OUTPUT, index=False)

    print(f"\nSaved {len(df):,} bars to {OUTPUT}")
    print(f"Range: {df.time.iloc[0]} to {df.time.iloc[-1]}")
    days = (df.time.iloc[-1] - df.time.iloc[0]).days
    print(f"Span: {days} days (~{days//30} months)")

if __name__ == "__main__":
    main()
