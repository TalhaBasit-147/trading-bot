"""Run the ORB bot live against MT5 (demo or real account).

Make sure .env has:
  MODE=live
  MT5_LOGIN=...
  MT5_PASSWORD=...
  MT5_SERVER=...
  PRIMARY_SYMBOL=XAUUSD
  RR_TARGET=1.2
  RISK_PER_TRADE=0.02
  STARTING_EQUITY=1000
"""
import os
os.environ.setdefault("MODE", "live")

from app.main import main

if __name__ == "__main__":
    main()
