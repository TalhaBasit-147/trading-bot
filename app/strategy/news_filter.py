"""News blackout filter for gold trading bot.

Blocks trading during ±30 minutes around the three events that most
violently move XAUUSD:
  1. Non-Farm Payrolls (NFP)       — first Friday of month, 13:30 UTC
  2. CPI (Consumer Price Index)    — ~2nd/3rd week of month, 13:30 UTC  
  3. FOMC Rate Decision            — 8x per year, 19:00 UTC

Uses forex-calendar.pro free API (100 req/15min, no card required).
Falls back gracefully if API is unavailable — never blocks trades on error.

Registration: https://forex-calendar.pro (free, just an email)
Set API key in .env as: NEWS_API_KEY=your_key_here
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
import threading

from loguru import logger

try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False


# ── Configuration ──────────────────────────────────────────────────────────

# Minutes before and after a high-impact event to block trading
BLACKOUT_MINUTES_BEFORE = 30
BLACKOUT_MINUTES_AFTER = 30

# Only block these specific events (case-insensitive substring match)
BLOCKED_EVENT_KEYWORDS = [
    "non-farm",
    "nonfarm",
    "nfp",
    "consumer price index",
    "cpi",
    "fomc",
    "federal funds rate",
    "fed rate",
]

# Cache the API response for this many minutes (avoid hammering the API)
CACHE_MINUTES = 15

# API endpoint
CALENDAR_API_URL = "https://api.forex-calendar.pro/events/upcoming"

# Timeout for the API call (seconds) — fail fast, never block the bot
API_TIMEOUT = 4


# ── Internal cache ─────────────────────────────────────────────────────────

class _NewsCache:
    def __init__(self):
        self._events = []
        self._fetched_at: Optional[datetime] = None
        self._lock = threading.Lock()

    def is_stale(self) -> bool:
        if self._fetched_at is None:
            return True
        age = (datetime.now(timezone.utc) - self._fetched_at).total_seconds()
        return age > CACHE_MINUTES * 60

    def update(self, events: list) -> None:
        with self._lock:
            self._events = events
            self._fetched_at = datetime.now(timezone.utc)

    def get(self) -> list:
        with self._lock:
            return list(self._events)


_cache = _NewsCache()


# ── API fetch ──────────────────────────────────────────────────────────────

def _fetch_events(api_key: str) -> list:
    """Fetch upcoming high-impact USD events from forex-calendar.pro."""
    if not _REQUESTS_AVAILABLE:
        return []
    try:
        resp = _requests.get(
            CALENDAR_API_URL,
            headers={"X-API-Key": api_key},
            params={
                "impact": "HIGH",
                "currency": "USD",
                "hours": 4,   # look 4 hours ahead
            },
            timeout=API_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning(f"[NewsFilter] API returned {resp.status_code}")
            return []
        data = resp.json()
        return data.get("events", [])
    except Exception as e:
        logger.warning(f"[NewsFilter] API fetch failed: {e}")
        return []


# ── Main public function ───────────────────────────────────────────────────

def is_news_blackout(now: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    Check whether we are in a news blackout window.

    Returns:
        (blocked: bool, reason: str)
        
    Always returns (False, "") on any error — we never block trading
    due to an API failure. The bot should trade normally if this
    function can't reach the calendar.

    Usage in strategy:
        blocked, reason = is_news_blackout()
        if blocked:
            logger.info(f"Skipping — {reason}")
            return None
    """
    if now is None:
        now = datetime.now(timezone.utc)

    api_key = os.environ.get("NEWS_API_KEY", "")
    if not api_key:
        # No key configured — log once, never block
        return False, ""

    # Refresh cache if stale
    if _cache.is_stale():
        events = _fetch_events(api_key)
        _cache.update(events)

    events = _cache.get()
    if not events:
        return False, ""

    for event in events:
        name = event.get("name", "").lower()
        minutes_until = event.get("minutes_until", 999)

        # Only block on our specific keywords
        if not any(kw in name for kw in BLOCKED_EVENT_KEYWORDS):
            continue

        # Block if we're within the blackout window
        # minutes_until is negative if the event already passed
        if -BLACKOUT_MINUTES_AFTER <= minutes_until <= BLACKOUT_MINUTES_BEFORE:
            display_name = event.get("name", "Unknown event")
            if minutes_until >= 0:
                reason = f"{display_name} in {minutes_until}min (blackout ±{BLACKOUT_MINUTES_BEFORE}min)"
            else:
                reason = f"{display_name} released {abs(minutes_until)}min ago (blackout ±{BLACKOUT_MINUTES_AFTER}min)"
            logger.info(f"[NewsFilter] BLACKOUT — {reason}")
            return True, reason

    return False, ""


# ── Standalone test ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    key = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("NEWS_API_KEY", "")
    if not key:
        print("Usage: python news_filter.py YOUR_API_KEY")
        print("Or set NEWS_API_KEY in environment")
        sys.exit(1)

    os.environ["NEWS_API_KEY"] = key
    blocked, reason = is_news_blackout()
    if blocked:
        print(f"🔴 BLACKOUT: {reason}")
    else:
        print("✅ No blackout — safe to trade")
        events = _fetch_events(key)
        if events:
            print(f"\nUpcoming high-impact USD events (next 4 hours):")
            for e in events:
                print(f"  - {e.get('name')} in {e.get('minutes_until')}min")
        else:
            print("No high-impact USD events in next 4 hours")
