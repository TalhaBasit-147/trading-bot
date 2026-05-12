"""Manually trigger a training run. Normally the engine does this nightly."""
from __future__ import annotations

from app.learning.trainer import train
from app.monitoring.logging_setup import setup_logging


if __name__ == "__main__":
    setup_logging()
    path = train()
    if path:
        print(f"Model saved to {path}")
    else:
        print("Not enough data to train yet.")
