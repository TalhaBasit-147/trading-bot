"""Daily pre-session briefing (08:00 New York time, weekdays).

Automates the routine of discretionary gold day traders: check today's
high-impact USD releases, read the weekly bias from market structure, and
mark the key levels. Sent to Telegram and the log. Context only: the Nov
2026 backtest found the weekly bias is not a trading signal on its own, so
nothing here places trades.

Weekly bias = market structure on H1 over the last 5 trading days: swing
highs/lows are fractals (beat 3 bars on each side). Higher high + higher low
= BULL, lower high + lower low = BEAR, anything else = RANGE.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Sequence

FRACTAL_N = 3
BIAS_DAYS = 5


def _naive(ts: datetime) -> datetime:
    return ts.replace(tzinfo=None) if ts.tzinfo is not None else ts


def _nth_sunday(year: int, month: int, n: int) -> datetime:
    d = datetime(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def to_et(utc: datetime) -> datetime:
    """Naive UTC -> naive New York time (US DST: 2nd Sunday of March to 1st Sunday of November, 02:00 local)."""
    utc = _naive(utc)
    start = _nth_sunday(utc.year, 3, 2) + timedelta(hours=7)    # 02:00 EST = 07:00 UTC
    end = _nth_sunday(utc.year, 11, 1) + timedelta(hours=6)     # 02:00 EDT = 06:00 UTC
    return utc - timedelta(hours=4 if start <= utc < end else 5)


def weekly_bias(h1: Sequence) -> tuple[str, str]:
    """(BULL|BEAR|RANGE, short explanation) from H1 bars (objects with .ts .high .low), oldest first."""
    if not h1:
        return "RANGE", "no data"
    dates = sorted({_naive(b.ts).date() for b in h1})
    keep = set(dates[-(BIAS_DAYS + 1):])                          # last 5 full days + today so far
    bars = [b for b in h1 if _naive(b.ts).date() in keep]
    hs, ls, n = [], [], FRACTAL_N
    for i in range(n, len(bars) - n):                             # a swing needs n bars after it (no lookahead)
        if bars[i].high > max(b.high for b in bars[i - n:i]) and bars[i].high > max(b.high for b in bars[i + 1:i + n + 1]):
            hs.append(bars[i].high)
        if bars[i].low < min(b.low for b in bars[i - n:i]) and bars[i].low < min(b.low for b in bars[i + 1:i + n + 1]):
            ls.append(bars[i].low)
    if len(hs) < 2 or len(ls) < 2:
        return "RANGE", "not enough swings"
    if hs[-1] > hs[-2] and ls[-1] > ls[-2]:
        return "BULL", "higher high + higher low"
    if hs[-1] < hs[-2] and ls[-1] < ls[-2]:
        return "BEAR", "lower high + lower low"
    return "RANGE", "mixed structure"


@dataclass
class SymbolSnapshot:
    symbol: str
    price: Optional[float]
    bias: str
    bias_why: str
    prev_day: Optional[tuple]      # (high, low)
    prev_week: Optional[tuple]
    overnight: Optional[tuple]


def symbol_snapshot(symbol: str, h1: Sequence, d1: Sequence) -> SymbolSnapshot:
    bias, why = weekly_bias(h1)
    price = h1[-1].close if h1 else None
    prev_day = prev_week = overnight = None
    if d1:
        today = _naive(d1[-1].ts).date()
        past = [b for b in d1 if _naive(b.ts).date() < today]
        if past:
            prev_day = (past[-1].high, past[-1].low)
        wk = today.isocalendar()[:2]
        y, w = (wk[0], wk[1] - 1) if wk[1] > 1 else (wk[0] - 1, date(wk[0] - 1, 12, 28).isocalendar()[1])
        pw = [b for b in d1 if _naive(b.ts).date().isocalendar()[:2] == (y, w)]
        if pw:
            prev_week = (max(b.high for b in pw), min(b.low for b in pw))
        tb = [b for b in h1 if _naive(b.ts).date() == today]
        if tb:
            overnight = (max(b.high for b in tb), min(b.low for b in tb))
    return SymbolSnapshot(symbol, price, bias, why, prev_day, prev_week, overnight)


def _hl(p: Optional[tuple]) -> str:
    return f"H {p[0]:,.2f} / L {p[1]:,.2f}" if p else "n/a"


def format_briefing(et_now: datetime, events: List, snaps: List[SymbolSnapshot]) -> str:
    """Telegram text (legacy Markdown: no underscores, only *bold*)."""
    from app.strategy.news_filter import BLACKOUT_MINUTES_AFTER, BLACKOUT_MINUTES_BEFORE
    lines = [f"📋 *Pre-session briefing* — {et_now:%a %d %b %Y}, {et_now:%H:%M} ET", "", "📅 *USD high-impact today*"]
    if events:
        for e in events:
            extra = ", ".join(x for x in (f"fcst {e.forecast}" if e.forecast else "", f"prev {e.previous}" if e.previous else "") if x)
            flag = f"  ⛔ no new entries ±{BLACKOUT_MINUTES_BEFORE}/{BLACKOUT_MINUTES_AFTER}min" if e.is_blocked else ""
            lines.append(f"• {to_et(e.time_utc):%H:%M} ET {e.title}{f' ({extra})' if extra else ''}{flag}")
    else:
        lines.append("• none scheduled")
    for s in snaps:
        lines += ["", f"*{s.symbol}* {s.price:,.2f} — weekly bias *{s.bias}* ({s.bias_why})" if s.price is not None else f"*{s.symbol}* — no data",
                  f"Prev day {_hl(s.prev_day)}", f"Prev week {_hl(s.prev_week)}", f"Today so far {_hl(s.overnight)}"]
    lines += ["", "ℹ️ Context only. In the 3-year test the weekly bias was not a profitable signal by itself."]
    return "\n".join(lines).replace("_", " ")
