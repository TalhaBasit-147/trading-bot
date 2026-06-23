"""Broker time utilities — robust against broker DST / offset changes.

THE PROBLEM THIS SOLVES:
  MT5 returns bar timestamps in BROKER SERVER TIME, which is NOT UTC.
  The offset (e.g. UTC+2, +3, +5) CHANGES with daylight saving on the
  broker's server. Hardcoding "broker time = UTC+5" silently breaks the
  moment the broker shifts for DST — which is exactly what happened
  (broker went UTC+5 -> UTC+3), pushing the NY-open ORB 2 hours off.

THE FIX:
  Never hardcode the offset. Compute it live by comparing the latest
  MT5 bar timestamp to real UTC, round to the nearest hour, and convert
  every bar timestamp to TRUE UTC before any strategy logic runs.
  All strategy time constants are then written in UTC and never break.

USAGE in a strategy:
    from app.strategy.broker_time import to_utc
    # bar.ts is broker-server time from MT5
    utc_ts = to_utc(bar.ts, broker_offset_hours)
    t = utc_ts.time()   # now compare against UTC constants
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger


def detect_broker_offset_hours(broker) -> int:
    """Compute broker server offset from UTC, in whole hours.

    Compares the latest M1 bar timestamp (broker server time) against the
    real current UTC time and rounds to the nearest hour.

    Returns an integer like 2, 3, or 5. Falls back to 3 if detection fails
    (logs a loud warning so it's never silent).
    """
    try:
        bars = broker.get_bars("XAUUSD", "M1", 2)
        if not bars:
            logger.error("[broker_time] No bars to detect offset — defaulting to +3")
            return 3
        broker_bar_ts = bars[-1].ts
        # Strip tzinfo for naive comparison (MT5 ts is naive broker time)
        if broker_bar_ts.tzinfo is not None:
            broker_bar_ts = broker_bar_ts.replace(tzinfo=None)
        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
        # The latest closed bar is ~1-2 min behind 'now'; compare and round
        diff_hours = (broker_bar_ts - utc_now).total_seconds() / 3600.0
        offset = int(round(diff_hours))
        logger.info(f"[broker_time] Detected broker offset: UTC+{offset} "
                    f"(bar={broker_bar_ts}, utc={utc_now}, raw_diff={diff_hours:.2f}h)")
        return offset
    except Exception as e:
        logger.error(f"[broker_time] Offset detection failed: {e} — defaulting to +3")
        return 3


def to_utc(broker_ts: datetime, offset_hours: int) -> datetime:
    """Convert a broker-server timestamp to true UTC (naive).

    broker_ts: timestamp as returned by MT5 (broker server time)
    offset_hours: positive int, e.g. 3 means broker = UTC+3
    """
    ts = broker_ts.replace(tzinfo=None) if broker_ts.tzinfo else broker_ts
    return ts - timedelta(hours=offset_hours)
