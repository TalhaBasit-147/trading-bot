"""Session classification. All times are UTC."""
from __future__ import annotations

from datetime import datetime, time
from enum import Enum

from app.config import settings


class Session(str, Enum):
    ASIA = "ASIA"
    LONDON = "LONDON"
    NY = "NY"
    OVERLAP = "OVERLAP"     # London/NY overlap (13:00-16:00 UTC usually)
    DEAD = "DEAD"


def _between(t: time, start: time, end: time) -> bool:
    return start <= t < end


def classify(ts: datetime) -> Session:
    """Return the session `ts` falls into (UTC)."""
    t = ts.time()
    lo, lc = settings.london_open_t, settings.london_close_t
    no, nc = settings.ny_open_t, settings.ny_close_t
    in_london = _between(t, lo, lc)
    in_ny = _between(t, no, nc)
    if in_london and in_ny:
        return Session.OVERLAP
    if in_london:
        return Session.LONDON
    if in_ny:
        return Session.NY
    # Asia roughly 00:00-06:00 UTC
    if _between(t, time(0, 0), time(6, 0)):
        return Session.ASIA
    return Session.DEAD


def is_tradeable(ts: datetime) -> bool:
    s = classify(ts)
    if s in (Session.LONDON, Session.NY, Session.OVERLAP):
        return True
    if s == Session.ASIA and not settings.BLOCK_ASIAN_SESSION:
        return True
    return False
