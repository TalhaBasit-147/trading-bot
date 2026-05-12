"""Run the ORB bot in paper mode (no real money, no MT5 required)."""
import os
os.environ.setdefault("MODE", "paper")

from app.main import main

if __name__ == "__main__":
    main()
