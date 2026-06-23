import MetaTrader5 as mt5
import datetime
from app.strategy.delayed_orb import ORB_START, ORB_END, ENTRY_START, ENTRY_END
from app.strategy.prev_day_breakout import SESSION_START, SESSION_END

mt5.initialize()
mt5.symbol_select('XAUUSD', True)
r = mt5.copy_rates_from_pos('XAUUSD', mt5.TIMEFRAME_M1, 0, 1)
mt5.shutdown()

bar_time = datetime.datetime.fromtimestamp(r[-1]['time'])
utc_now = datetime.datetime.utcnow()

print(f"Current UTC time:        {utc_now}")
print(f"Bar timestamp from MT5:  {bar_time}")
print(f"Offset from UTC:         +{bar_time.hour - utc_now.hour}h")
print()
print(f"DELAYED_ORB windows:")
print(f"  ORB collection: {ORB_START} - {ORB_END}")
print(f"  Entry window:   {ENTRY_START} - {ENTRY_END}")
print()
print(f"PREV_DAY_BREAKOUT windows:")
print(f"  Session: {SESSION_START} - {SESSION_END}")
print()
print("NY equity opens at 13:30 UTC.")
print(f"With current broker offset, NY open in bar time = {(13 + (bar_time.hour - utc_now.hour)) % 24}:30")
print(f"ORB_START is set to {ORB_START}")
expected_hour = (13 + (bar_time.hour - utc_now.hour)) % 24
if ORB_START.hour == expected_hour and ORB_START.minute == 30:
    print("RESULT: TIMING IS CORRECT")
else:
    print(f"RESULT: TIMING IS WRONG - should be {expected_hour}:30")