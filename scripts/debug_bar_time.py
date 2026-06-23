import sys
sys.path.insert(0, ".")
from app.execution.mt5_broker import MT5Broker
import time

broker = MT5Broker()
broker.connect()

# Get the latest bar
bars = broker.get_bars("XAUUSD", "M1", 3)
for b in bars:
    print(f"Bar.ts = {b.ts}")
    print(f"Bar.ts.time() = {b.ts.time()}")
    print(f"Bar.ts.hour = {b.ts.hour}")
    print(f"---")

broker.disconnect()