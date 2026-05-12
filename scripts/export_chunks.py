import MetaTrader5 as mt5
import pandas as pd

if not mt5.initialize():
    print(f"ERROR: {mt5.last_error()}")
    quit()

mt5.symbol_select("XAUUSD", True)
print("MT5 connected, XAUUSD selected")

# Grab in chunks of 50,000
all_rates = []
for offset in range(0, 300000, 50000):
    rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M1, offset, 50000)
    if rates is None or len(rates) == 0:
        print(f"  Chunk at offset {offset}: no more data")
        break
    print(f"  Chunk at offset {offset}: got {len(rates)} bars")
    all_rates.append(pd.DataFrame(rates))

mt5.shutdown()

if not all_rates:
    print("ERROR: No data at all")
else:
    df = pd.concat(all_rates).drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df[["time", "open", "high", "low", "close", "tick_volume"]]
    df.columns = ["time", "open", "high", "low", "close", "volume"]
    df.to_csv("data/XAUUSD_M1_ALL.csv", index=False)
    print(f"\nExported {len(df)} bars total")
    print(f"From: {df.time.iloc[0]}")
    print(f"To:   {df.time.iloc[-1]}")
    print(f"Days: {(df.time.iloc[-1] - df.time.iloc[0]).days}")