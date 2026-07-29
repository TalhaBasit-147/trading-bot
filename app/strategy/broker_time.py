"""Broker time utilities — robust against broker DST / offset changes AND
stale market data.

THE PROBLEM THIS SOLVES:
  MT5 returns bar timestamps in BROKER SERVER TIME, which is NOT UTC.
  The offset (e.g. UTC+2, +3, +5) CHANGES with daylight saving on the
  broker's server. Hardcoding "broker time = UTC+5" silently breaks the
  moment the broker shifts for DST — which is exactly what happened
  (broker went UTC+5 -> UTC+3), pushing the NY-open ORB 2 hours off.

  A second, separate failure mode: offset detection was computed from a
  single source (the latest M1 bar). When that source is STALE (weekend,
  market closed, a data gap), it can be 30+ hours old, producing garbage
  offsets like "UTC+-35" or "UTC+-21". That garbage offset then fed to_utc()
  everywhere, silently breaking every time-based gate for the rest of the
  session (confirmed: it muted PREV_DAY_BREAKOUT's entry-time cutoff
  entirely). Separately confirmed live: mt5.symbol_info_tick() can itself
  return None near session end even while the M1 bar feed is still current —
  so neither source alone is reliable on its own.

THE FIX:
  Never hardcode the offset, and never trust a stale timestamp from either
  source:
    1. Try the live tick first. It updates continuously while the market
       trades, so it's normally the most current reference — but it can be
       None/unavailable (confirmed live), so this must never raise; a failure
       here just falls through to the bar.
    2. Try the last M1 bar. This was consistently available in testing even
       when the tick was None, so it's just as trustworthy a reference when
       the tick can't be used.
    3. Whichever source is used, only accept it if BOTH:
         a) its raw offset, rounded to the nearest whole hour, is plausible
            (brokers use clean whole-hour offsets, and a genuine offset is
            always small — see _plausible below), and
         b) it is FRESH: its implied UTC time (via the best offset already
            known) lands within _STALENESS_TOLERANCE_MIN of true "now". This
            is the core fix — a plausibility check alone can't catch every
            stale case (e.g. a source stale by some number of hours that
            isn't close to a 24h multiple could coincidentally still land in
            the plausible range), so freshness is checked independently. The
            tolerance is deliberately wider than a couple of minutes so a
            correct-but-not-yet-confirmed fallback guess (e.g. off by one
            DST hour) never causes a false rejection — while still rejecting
            anything stale by many hours or days (the 35h/weekend case).
    4. If neither source is usable this call, do NOT compute a new offset —
       reuse the last offset this process ever confirmed from live data, or
       finally settings.BROKER_OFFSET_FALLBACK_HOURS. Never raises, never
       returns a guessed/garbage value, and always logs its source.

USAGE in a strategy:
    from app.strategy.broker_time import to_utc
    # bar.ts is broker-server time from MT5
    utc_ts = to_utc(bar.ts, broker_offset_hours)
    t = utc_ts.time()   # now compare against UTC constants

Callers of anything safety-critical (e.g. an entry-time gate) should also
check `has_confirmed_broker_offset()` — if this process has never actually
confirmed an offset from live data, the current value is only the configured
default, i.e. a guess, and safety-critical logic should fail OPEN rather than
silently gate trades on it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger

from app.config import settings


# Real broker/UTC offsets are always small; a computed value outside this
# range means the underlying timestamp was stale, not a genuine offset.
_MIN_PLAUSIBLE_OFFSET_HOURS = -12
_MAX_PLAUSIBLE_OFFSET_HOURS = 14

# How far (real minutes) a source's implied UTC time — computed via the best
# offset already known — may drift from true "now" before it's refused.
# Deliberately wide: it must comfortably absorb a reference guess being off
# by a full DST hour (60 min) with margin, while still decisively rejecting
# anything stale by many hours or days (weekend/market-closed staleness).
_STALENESS_TOLERANCE_MIN = 90.0

# Persisted across calls in this process: the last offset actually confirmed
# from live data (tick or a fresh bar). None until the first confirmation.
_last_good_offset: Optional[float] = None


def has_confirmed_broker_offset() -> bool:
    """True once this process has confirmed a real offset from live broker
    data at least once. False means every detection attempt so far has had
    to fall back to the configured default with zero live confirmation —
    i.e. the current offset is a guess, not a detection."""
    return _last_good_offset is not None


def _naive(ts: datetime) -> datetime:
    return ts.replace(tzinfo=None) if ts.tzinfo is not None else ts


def _plausible(offset: float) -> bool:
    return _MIN_PLAUSIBLE_OFFSET_HOURS <= offset <= _MAX_PLAUSIBLE_OFFSET_HOURS


def _try_source(source_ts: datetime, utc_now: datetime, reference_offset: float,
                 source_name: str) -> Optional[float]:
    """Return a whole-hour offset from source_ts if it is both plausible and
    fresh, else None (logging why it was rejected)."""
    raw_diff = (source_ts - utc_now).total_seconds() / 3600.0
    candidate = float(round(raw_diff))
    if not _plausible(candidate):
        logger.warning(f"[broker_time] {source_name} offset REJECTED "
                        f"({candidate:+.1f} implausible, raw_diff={raw_diff:.2f}h)")
        return None

    implied_utc = source_ts - timedelta(hours=reference_offset)
    staleness_min = abs((utc_now - implied_utc).total_seconds()) / 60.0
    if staleness_min > _STALENESS_TOLERANCE_MIN:
        logger.warning(f"[broker_time] {source_name} STALE (~{staleness_min:.0f} min old "
                        f"using reference offset {reference_offset:+.1f}) — not used for detection")
        return None

    logger.info(f"[broker_time] offset={candidate:+.1f} source={source_name} "
                f"({source_name}_ts={source_ts}, utc={utc_now}, raw_diff={raw_diff:.2f}h)")
    return candidate


def detect_broker_offset_hours(broker) -> float:
    """Compute broker server offset from UTC, in whole hours.

    See module docstring for the full preference order and rejection rules.
    Never raises; always returns a usable, sane number.
    """
    global _last_good_offset
    reference = _last_good_offset if _last_good_offset is not None else settings.BROKER_OFFSET_FALLBACK_HOURS
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)

    # 1) Live tick. mt5.symbol_info_tick() (via broker.tick()) can be
    #    unavailable near session boundaries even while bars are current —
    #    never let that raise past here.
    try:
        tick = broker.tick("XAUUSD")
        candidate = _try_source(_naive(tick["time"]), utc_now, reference, "live_tick")
        if candidate is not None:
            _last_good_offset = candidate
            return candidate
    except Exception as e:
        logger.warning(f"[broker_time] live_tick unavailable: {e} — trying last bar")

    # 2) Last M1 bar — consistently available even when the tick isn't.
    try:
        bars = broker.get_bars("XAUUSD", "M1", 2)
        if bars:
            candidate = _try_source(_naive(bars[-1].ts), utc_now, reference, "last_bar")
            if candidate is not None:
                _last_good_offset = candidate
                return candidate
        else:
            logger.warning("[broker_time] No bars available for offset detection")
    except Exception as e:
        logger.warning(f"[broker_time] bar-based detection failed: {e}")

    # 3) Neither source usable this call — never return a guessed/garbage
    #    value computed from stale data.
    source = "last known-good" if _last_good_offset is not None else "configured default"
    logger.error(f"[broker_time] offset REJECTED this call (no fresh tick or bar) "
                 f"— using {source} fallback {reference:+.1f}")
    return reference


def to_utc(broker_ts: datetime, offset_hours: float) -> datetime:
    """Convert a broker-server timestamp to true UTC (naive).

    broker_ts: timestamp as returned by MT5 (broker server time)
    offset_hours: e.g. 3 (or 3.0) means broker = UTC+3
    """
    return _naive(broker_ts) - timedelta(hours=offset_hours)
