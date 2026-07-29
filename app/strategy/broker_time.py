"""Broker time utilities — robust against broker DST / offset changes AND
stale market data.

THE PROBLEM THIS SOLVES:
  MT5 returns bar timestamps in BROKER SERVER TIME, which is NOT UTC.
  The offset (e.g. UTC+2, +3, +5) CHANGES with daylight saving on the
  broker's server. Hardcoding "broker time = UTC+5" silently breaks the
  moment the broker shifts for DST — which is exactly what happened
  (broker went UTC+5 -> UTC+3), pushing the NY-open ORB 2 hours off.

  A second, separate failure mode: offset detection was computed from the
  latest M1 bar's timestamp. When that bar is STALE (weekend, market closed,
  a data gap), it can be 30+ hours old, producing garbage offsets like
  "UTC+-35" or "UTC+-21". That garbage offset then fed to_utc() everywhere,
  silently breaking every time-based gate for the rest of the session
  (confirmed: it muted PREV_DAY_BREAKOUT's entry-time cutoff entirely).

THE FIX:
  Never hardcode the offset, and never trust a stale timestamp:
    1. Prefer the live TICK, which updates continuously while the market
       trades (unlike an M1 bar, which can lag up to ~a minute behind "now"
       even when fresh, and is fully stale for the entire time the market is
       closed).
    2. If the tick is unavailable, fall back to the last M1 bar — but ONLY if
       it is fresh (its implied UTC time, using the best offset we already
       know, must land within a few minutes of true "now").
    3. Reject any candidate offset outside a sane [-12, +14] range outright —
       this is what catches wildly-stale timestamps before they ever become
       "the offset".
    4. If nothing usable is available this call, fall back to the last
       offset this process ever confirmed from live data, or finally to
       settings.BROKER_OFFSET_FALLBACK_HOURS. Never raises, never returns a
       guessed/garbage value, and always logs its source.

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
_MIN_PLAUSIBLE_OFFSET_HOURS = -12.0
_MAX_PLAUSIBLE_OFFSET_HOURS = 14.0

# How stale (real minutes) the last M1 bar may be, once interpreted through
# the best offset we currently know, before we refuse to use it.
_BAR_STALENESS_TOLERANCE_MIN = 10.0

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


def _round_to_half_hour(hours: float) -> float:
    return round(hours * 2) / 2.0


def _plausible(offset: float) -> bool:
    return _MIN_PLAUSIBLE_OFFSET_HOURS <= offset <= _MAX_PLAUSIBLE_OFFSET_HOURS


def detect_broker_offset_hours(broker) -> float:
    """Compute broker server offset from UTC, in hours (whole or half-hour).

    See module docstring for the full preference order and rejection rules.
    Never raises; always returns a usable, sane number.
    """
    global _last_good_offset
    fallback = _last_good_offset if _last_good_offset is not None else settings.BROKER_OFFSET_FALLBACK_HOURS
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)

    # 1) Live tick — current even when the market has just gone quiet, and
    #    itself stale (caught below) only when the market is genuinely closed.
    try:
        tick = broker.tick("XAUUSD")
        tick_ts = _naive(tick["time"])
        raw_diff = (tick_ts - utc_now).total_seconds() / 3600.0
        candidate = _round_to_half_hour(raw_diff)
        if _plausible(candidate):
            logger.info(f"[broker_time] offset={candidate:+.1f} source=live_tick "
                        f"(tick={tick_ts}, utc={utc_now}, raw_diff={raw_diff:.2f}h)")
            _last_good_offset = candidate
            return candidate
        logger.warning(f"[broker_time] live_tick offset REJECTED "
                        f"({candidate:+.1f} implausible, raw_diff={raw_diff:.2f}h) — trying last bar")
    except Exception as e:
        logger.warning(f"[broker_time] live_tick detection failed: {e} — trying last bar")

    # 2) Last M1 bar — only trusted if fresh relative to the best offset we
    #    currently know (last confirmed, or the configured default).
    try:
        bars = broker.get_bars("XAUUSD", "M1", 2)
        if bars:
            bar_ts = _naive(bars[-1].ts)
            implied_utc = bar_ts - timedelta(hours=fallback)
            staleness_min = abs((utc_now - implied_utc).total_seconds()) / 60.0
            if staleness_min <= _BAR_STALENESS_TOLERANCE_MIN:
                raw_diff = (bar_ts - utc_now).total_seconds() / 3600.0
                candidate = _round_to_half_hour(raw_diff)
                if _plausible(candidate):
                    logger.info(f"[broker_time] offset={candidate:+.1f} source=last_bar "
                                f"(bar={bar_ts}, utc={utc_now}, raw_diff={raw_diff:.2f}h)")
                    _last_good_offset = candidate
                    return candidate
                logger.warning(f"[broker_time] last_bar offset REJECTED "
                                f"({candidate:+.1f} implausible, raw_diff={raw_diff:.2f}h)")
            else:
                logger.warning(f"[broker_time] last_bar STALE (~{staleness_min:.0f} min old "
                                f"using reference offset {fallback:+.1f}) — not used for detection")
        else:
            logger.warning("[broker_time] No bars available for offset detection")
    except Exception as e:
        logger.warning(f"[broker_time] bar-based detection failed: {e}")

    # 3) Nothing usable this call — never return a guessed/garbage value.
    source = "last known-good" if _last_good_offset is not None else "configured default"
    logger.error(f"[broker_time] offset REJECTED this call (no fresh tick or bar) "
                 f"— using {source} fallback {fallback:+.1f}")
    return fallback


def to_utc(broker_ts: datetime, offset_hours: float) -> datetime:
    """Convert a broker-server timestamp to true UTC (naive).

    broker_ts: timestamp as returned by MT5 (broker server time)
    offset_hours: e.g. 3 (or 3.0) means broker = UTC+3
    """
    return _naive(broker_ts) - timedelta(hours=offset_hours)
