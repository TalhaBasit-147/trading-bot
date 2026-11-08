"""News blackout + economic calendar for the trading bot.

Blocks new entries during ±30 minutes around the USD releases that most
violently move XAUUSD: Non-Farm Payrolls, CPI and the FOMC decision.

Source: Forex Factory's free weekly calendar feed (no key, no account):
    https://nfs.faireconomy.media/ff_calendar_thisweek.json
Fields per event: title, country, date (ET with UTC offset), impact,
forecast, previous. There is no "actual" field, which is fine: the
blackout only needs the scheduled time.

The previous version called forex-calendar.pro with NEWS_API_KEY read from
os.environ. Nothing ever loads .env into os.environ and `requests` was not
installed, so it silently never blocked anything. This version needs no key
and uses httpx (already a dependency).

Contract (unchanged): never block trading because of a feed problem. Any
fetch/parse failure means "no blackout". The feed is cached for an hour so
the bot fetches it about 25 times a day.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

from loguru import logger

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
API_TIMEOUT = 5            # seconds; fail fast, never stall the engine
CACHE_MINUTES = 60         # successful fetch is reused this long
RETRY_MINUTES = 5          # after a failed fetch, wait this long before retrying

# Minutes before and after a blocked event during which new entries are skipped
BLACKOUT_MINUTES_BEFORE = 30
BLACKOUT_MINUTES_AFTER = 30

# Blocked releases (USD, case-insensitive substring of the Forex Factory title).
# Titles look like "Non-Farm Employment Change", "CPI m/m", "Core CPI m/m",
# "Federal Funds Rate", "FOMC Statement", "FOMC Press Conference".
BLOCKED_EVENT_KEYWORDS = ["non-farm", "nonfarm", "cpi", "federal funds rate", "fomc"]


@dataclass(frozen=True)
class CalendarEvent:
    title: str
    country: str
    impact: str
    time_utc: datetime        # naive UTC
    forecast: str = ""
    previous: str = ""

    @property
    def is_blocked(self) -> bool:
        # High impact only: the keywords alone also match low-impact items such as
        # "FOMC Member X Speaks", "ADP Non-Farm Employment Change" and
        # "Prelim Nonfarm Productivity" (8 of 9 matches in the week of 2026-11-02).
        t = self.title.lower()
        return (self.country.upper() == "USD" and self.impact.lower() == "high"
                and any(k in t for k in BLOCKED_EVENT_KEYWORDS))


def parse_feed(items: list) -> List[CalendarEvent]:
    """Parse the Forex Factory weekly JSON into CalendarEvents (bad rows are skipped)."""
    out: List[CalendarEvent] = []
    for e in items or []:
        try:
            ts = datetime.fromisoformat(str(e["date"]))
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts
            out.append(CalendarEvent(title=str(e.get("title", "")), country=str(e.get("country", "")),
                                     impact=str(e.get("impact", "")), time_utc=ts,
                                     forecast=str(e.get("forecast", "") or ""), previous=str(e.get("previous", "") or "")))
        except Exception:
            continue
    return out


def _http_fetch() -> list:
    import httpx
    r = httpx.get(FEED_URL, timeout=API_TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (trading-bot calendar)"})
    r.raise_for_status()
    return r.json()


class _Calendar:
    def __init__(self, fetcher: Callable[[], list] = _http_fetch):
        self._fetcher = fetcher
        self._events: List[CalendarEvent] = []
        self._fetched_at: Optional[datetime] = None
        self._failed_at: Optional[datetime] = None
        self._lock = threading.Lock()

    def events(self, now: Optional[datetime] = None) -> List[CalendarEvent]:
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        with self._lock:
            fresh = self._fetched_at is not None and now - self._fetched_at < timedelta(minutes=CACHE_MINUTES)
            backing_off = self._failed_at is not None and now - self._failed_at < timedelta(minutes=RETRY_MINUTES)
            if not fresh and not backing_off:
                try:
                    self._events = parse_feed(self._fetcher())
                    self._fetched_at, self._failed_at = now, None
                    logger.info(f"[NewsFilter] calendar refreshed: {len(self._events)} events this week, "
                                f"{sum(e.is_blocked for e in self._events)} blocked USD releases")
                except Exception as e:
                    self._failed_at = now
                    logger.warning(f"[NewsFilter] calendar fetch failed ({e}); not blocking any trades until it recovers")
            return list(self._events)


_calendar = _Calendar()


def _utcnow(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    return now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now


def is_news_blackout(now: Optional[datetime] = None) -> Tuple[bool, str]:
    """(blocked, reason). Blocks within -30/+30 min of NFP, CPI or FOMC. Never blocks on a feed error."""
    now = _utcnow(now)
    try:
        for ev in _calendar.events(now):
            if not ev.is_blocked:
                continue
            minutes_until = (ev.time_utc - now).total_seconds() / 60.0
            if -BLACKOUT_MINUTES_AFTER <= minutes_until <= BLACKOUT_MINUTES_BEFORE:
                if minutes_until >= 0:
                    reason = f"{ev.title} in {minutes_until:.0f}min (blackout ±{BLACKOUT_MINUTES_BEFORE}min)"
                else:
                    reason = f"{ev.title} released {abs(minutes_until):.0f}min ago (blackout ±{BLACKOUT_MINUTES_AFTER}min)"
                logger.info(f"[NewsFilter] BLACKOUT — {reason}")
                return True, reason
    except Exception as e:
        logger.warning(f"[NewsFilter] blackout check failed ({e}); not blocking")
    return False, ""


def usd_high_impact_events(day_et: date, now: Optional[datetime] = None) -> List[CalendarEvent]:
    """High-impact USD events whose New York date is `day_et` (for the daily briefing)."""
    from app.briefing import to_et
    try:
        evs = _calendar.events(_utcnow(now))
    except Exception:
        return []
    return sorted([e for e in evs if e.country.upper() == "USD" and e.impact.lower() == "high" and to_et(e.time_utc).date() == day_et],
                  key=lambda e: e.time_utc)


if __name__ == "__main__":
    blocked, reason = is_news_blackout()
    print(f"BLACKOUT: {reason}" if blocked else "No blackout right now")
    for e in _calendar.events():
        if e.country == "USD" and e.impact == "High":
            print(f"  {e.time_utc:%a %Y-%m-%d %H:%M} UTC  {e.title}  (forecast {e.forecast or '-'}, previous {e.previous or '-'})"
                  f"{'  [BLOCKED]' if e.is_blocked else ''}")
