"""Previous Day High/Low Breakout Strategy — uses MT5 history for accurate levels.

Fixes in this version:
  1. FRESH CROSS only: enters only when price crosses from INSIDE the prev-day
     range to OUTSIDE it. Prevents chasing a stale break when the bot restarts
     mid-day after a level already broke hours earlier.
  2. RR GUARD: rejects the signal if price already ran far past the level
     (overshoot), which would create a bad entry / risk:reward.
  3. Queries MT5 for the COMPLETE previous trading day's M1 high/low.

Logic:
  1. On each new day, fetch previous day's COMPLETE M1 history (skips weekends)
  2. Track whether price is currently inside the prev-day range
  3. During session, enter ONLY on the bar where price first crosses out:
     - Cross above prev_day_high -> BUY
     - Cross below prev_day_low  -> SELL
  4. SL capped at $15. TP = 1.5R.
  5. Reject if price already ran more than MAX_OVERSHOOT past the level.
  6. One trade per day max.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from app.config import settings
from app.core.types import Bar, Side
from app.strategy.broker_time import detect_broker_offset_hours, to_utc


@dataclass
class PrevDayLevels:
    date: str
    high: float
    low: float
    range_size: float


@dataclass
class DailyTrend:
    """D1 regime as of the previous COMPLETE trading day (no lookahead)."""
    prev_close: float
    ema50: float


@dataclass
class TradeSignal:
    ts: datetime
    symbol: str
    side: Side
    entry: float
    sl: float
    tp: float
    risk_dist: float
    strategy: str
    note: str = ""


# Session windows in broker bar time
# TRUE UTC (bar.ts converted via dynamic broker offset). Was 8:00-23:30
# broker time; under the old UTC+5 assumption that meant 03:00-18:30 UTC.
SESSION_START = time(3, 0)
SESSION_END = time(18, 0)
CLOSE_DEADLINE = time(18, 30)

# Risk parameters
MAX_RISK_DIST = 15.0
MIN_RISK_DIST = 3.0
RANGE_FRACTION = 0.3
SL_BUFFER = 0.21
RR_TARGET = 1.5

# Fresh-cross guard: reject if price has already run more than this far past
# the level by the time we see it (means we missed the clean entry).
MAX_OVERSHOOT = 3.0  # dollars past the level

CACHE_FILE = Path("data/prev_day_levels.json")

# Daily trend filter (D1 EMA50 regime vs breakout direction)
DAILY_TREND_D1_BARS = 60      # bars requested from MT5 (includes the current, incomplete day)
DAILY_TREND_EMA_PERIOD = 50
DAILY_TREND_MIN_COMPLETE_BARS = 51  # below this, fail open rather than block


class PrevDayBreakoutStrategy:
    def __init__(self, rr: float = RR_TARGET, risk_pct: float = 0.02, broker=None):
        self.rr = rr
        self.risk_pct = risk_pct
        self.strategy_name = "PREV_DAY_BREAKOUT"
        self.broker = broker
        self._trend_regime: Optional[DailyTrend] = None
        self._current_day = ""
        self._traded_today = False
        self._prev_levels: Optional[PrevDayLevels] = None
        # Track where price was on the PREVIOUS bar to detect a fresh cross
        self._was_inside_range = None  # None = unknown yet

    def set_broker(self, broker) -> None:
        self.broker = broker
        self._broker_offset = detect_broker_offset_hours(broker)

    def reset_day(self, new_day: str, symbol: str = "XAUUSD") -> None:
        self._current_day = new_day
        self._traded_today = False
        self._was_inside_range = None  # reset cross tracking
        self._prev_levels = self._fetch_previous_day_levels(symbol, new_day)
        if self._prev_levels:
            logger.info(
                f"[{self.strategy_name}] Day={new_day} "
                f"PrevDay levels: H={self._prev_levels.high:.2f} "
                f"L={self._prev_levels.low:.2f} "
                f"range=${self._prev_levels.range_size:.2f} "
                f"(from {self._prev_levels.date})"
            )
        else:
            logger.warning(f"[{self.strategy_name}] No prev day levels available for {new_day}")

        # Refreshed ONLY here, on new-day rollover — never re-fetched per tick.
        if settings.PREV_DAY_TREND_FILTER_ENABLED:
            self._trend_regime = self._fetch_daily_trend_regime(symbol)
            if self._trend_regime is not None:
                regime = ("BULLISH" if self._trend_regime.prev_close > self._trend_regime.ema50
                          else "BEARISH" if self._trend_regime.prev_close < self._trend_regime.ema50
                          else "FLAT")
                logger.info(
                    f"[{self.strategy_name}] [TREND_FILTER] D1 regime={regime} "
                    f"prev_close={self._trend_regime.prev_close:.2f} "
                    f"ema50={self._trend_regime.ema50:.2f}"
                )
        else:
            self._trend_regime = None

    def _fetch_daily_trend_regime(self, symbol: str) -> Optional[DailyTrend]:
        """D1 close vs EMA50 as of the previous COMPLETE trading day.

        Fail-open contract: on any fetch failure or insufficient history,
        return None and log a WARNING. Callers must treat None as "don't
        apply the filter today", never as a reason to block trading.
        """
        try:
            import MetaTrader5 as mt5
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_D1, 0, DAILY_TREND_D1_BARS)
            if rates is None or len(rates) == 0:
                logger.warning(
                    f"[{self.strategy_name}] [TREND_FILTER] D1 fetch returned no bars "
                    f"— failing open (filter not applied today)"
                )
                return None
            # copy_rates_from_pos(..., 0, ...) includes the CURRENT (possibly
            # incomplete) day at the end — drop it. No lookahead: everything
            # used below is a fully closed D1 bar.
            completed = rates[:-1]
            if len(completed) < DAILY_TREND_MIN_COMPLETE_BARS:
                logger.warning(
                    f"[{self.strategy_name}] [TREND_FILTER] Only {len(completed)} complete "
                    f"D1 bars available (<{DAILY_TREND_MIN_COMPLETE_BARS} needed) "
                    f"— failing open (filter not applied today)"
                )
                return None
            closes = [float(r['close']) for r in completed]
            prev_close = closes[-1]
            ema50 = self._ema(closes, DAILY_TREND_EMA_PERIOD)
            return DailyTrend(prev_close=prev_close, ema50=ema50)
        except Exception as e:
            logger.warning(
                f"[{self.strategy_name}] [TREND_FILTER] D1 fetch failed: {e} "
                f"— failing open (filter not applied today)"
            )
            return None

    @staticmethod
    def _ema(closes: list, period: int) -> float:
        """Standard EMA: seed with the SMA of the first `period` values, then
        apply the exponential multiplier through the rest. Returns the final
        (most recent) value."""
        if len(closes) < period:
            return sum(closes) / len(closes)
        multiplier = 2.0 / (period + 1)
        value = sum(closes[:period]) / period
        for price in closes[period:]:
            value = (price - value) * multiplier + value
        return value

    def _fetch_previous_day_levels(self, symbol: str, today: str) -> Optional[PrevDayLevels]:
        if self.broker is None:
            return self._load_from_cache()
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            for days_back in range(1, 8):
                check_date = today_dt - timedelta(days=days_back)
                check_str = check_date.strftime("%Y-%m-%d")
                bars = self._get_day_bars(symbol, check_date)
                if bars and len(bars) >= 100:
                    high = max(b.high for b in bars)
                    low = min(b.low for b in bars)
                    levels = PrevDayLevels(check_str, high, low, high - low)
                    self._save_to_cache(levels)
                    return levels
                else:
                    logger.debug(f"[{self.strategy_name}] {check_str} has {len(bars) if bars else 0} bars, skip")
            logger.warning(f"[{self.strategy_name}] No valid prev trading day in last 7 days")
            return self._load_from_cache()
        except Exception as e:
            logger.exception(f"[{self.strategy_name}] fetch prev day failed: {e}")
            return self._load_from_cache()

    def _get_day_bars(self, symbol: str, day: datetime) -> list:
        try:
            import MetaTrader5 as mt5
            day_start = datetime(day.year, day.month, day.day, 0, 0, 0)
            day_end = day_start + timedelta(days=1)
            rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, day_start, day_end)
            if rates is None or len(rates) == 0:
                return []
            return [type('B', (), {'high': float(r['high']), 'low': float(r['low'])}) for r in rates]
        except Exception as e:
            logger.warning(f"_get_day_bars failed: {e}")
            return []

    def _save_to_cache(self, levels: PrevDayLevels) -> None:
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps({
                "date": levels.date, "high": levels.high,
                "low": levels.low, "range_size": levels.range_size,
            }, indent=2))
        except Exception as e:
            logger.warning(f"cache save failed: {e}")

    def _load_from_cache(self) -> Optional[PrevDayLevels]:
        try:
            if not CACHE_FILE.exists():
                return None
            return PrevDayLevels(**json.loads(CACHE_FILE.read_text()))
        except Exception as e:
            logger.warning(f"cache load failed: {e}")
            return None

    def on_bar(self, bar: Bar, symbol: str = "XAUUSD") -> Optional[TradeSignal]:
        utc_ts = to_utc(bar.ts, getattr(self, "_broker_offset", 3))
        day = utc_ts.strftime("%Y-%m-%d")
        t = utc_ts.time()

        if day != self._current_day:
            self.reset_day(day, symbol)

        if self._prev_levels is None:
            return None

        pdh = self._prev_levels.high
        pdl = self._prev_levels.low
        pd_range = self._prev_levels.range_size

        # Is THIS bar's close inside the prev-day range?
        inside_now = pdl <= bar.close <= pdh

        # First bar we see: just record state, never trade (avoids chasing a
        # break that happened before the bot started).
        if self._was_inside_range is None:
            self._was_inside_range = inside_now
            return None

        if self._traded_today or t < SESSION_START or t >= SESSION_END:
            self._was_inside_range = inside_now
            return None

        risk = min(pd_range * RANGE_FRACTION, MAX_RISK_DIST)
        if risk < MIN_RISK_DIST:
            risk = MIN_RISK_DIST

        side = None
        entry = None
        sl = None

        crossed_up = self._was_inside_range and bar.high > pdh
        crossed_down = self._was_inside_range and bar.low < pdl

        if crossed_up:
            overshoot = bar.close - pdh
            if overshoot > MAX_OVERSHOOT:
                logger.info(f"[{self.strategy_name}] BUY cross but overshoot ${overshoot:.1f} > "
                            f"${MAX_OVERSHOOT} - skip stale break")
                self._was_inside_range = inside_now
                return None
            side = Side.BUY
            entry = pdh + SL_BUFFER
            sl = entry - risk
        elif crossed_down:
            overshoot = pdl - bar.close
            if overshoot > MAX_OVERSHOOT:
                logger.info(f"[{self.strategy_name}] SELL cross but overshoot ${overshoot:.1f} > "
                            f"${MAX_OVERSHOOT} - skip stale break")
                self._was_inside_range = inside_now
                return None
            side = Side.SELL
            entry = pdl - SL_BUFFER
            sl = entry + risk

        self._was_inside_range = inside_now

        if side is None:
            return None

        # Daily EMA50 trend filter: only take the breakout if its direction
        # agrees with the D1 regime as of the previous complete day. Skips
        # silently (same as the overshoot guard above) -- doesn't consume
        # _traded_today, so a later same-day breakout in the OTHER direction
        # can still fire if it agrees with the (fixed-for-the-day) regime.
        if settings.PREV_DAY_TREND_FILTER_ENABLED and self._trend_regime is not None:
            prev_close = self._trend_regime.prev_close
            ema50 = self._trend_regime.ema50
            if side == Side.BUY and not (prev_close > ema50):
                logger.info(
                    f"[TREND_FILTER] Skipping PREV_DAY BUY — price below EMA50 "
                    f"(prev_close={prev_close:.2f}, ema50={ema50:.2f})"
                )
                return None
            if side == Side.SELL and not (prev_close < ema50):
                logger.info(
                    f"[TREND_FILTER] Skipping PREV_DAY SELL — price above EMA50 "
                    f"(prev_close={prev_close:.2f}, ema50={ema50:.2f})"
                )
                return None

        risk_dist = abs(entry - sl)
        tp = entry + risk_dist * self.rr if side == Side.BUY else entry - risk_dist * self.rr

        self._traded_today = True
        return TradeSignal(
            ts=bar.ts, symbol=symbol, side=side,
            entry=entry, sl=sl, tp=tp,
            risk_dist=risk_dist,
            strategy=self.strategy_name,
            note=f"FreshCross PrevDay({self._prev_levels.date}) H={pdh:.2f} L={pdl:.2f} range=${pd_range:.1f}",
        )

    def should_close_eod(self, bar: Bar) -> bool:
        return to_utc(bar.ts, getattr(self, '_broker_offset', 3)).time() >= CLOSE_DEADLINE

    def compute_lots(
        self, equity: float, risk_dist: float,
        point: float = 0.01, tick_value: float = 1.0,
        min_lot: float = 0.01, max_lot: float = 1.0,
    ) -> float:
        risk_cash = equity * self.risk_pct
        risk_points = risk_dist / point
        loss_per_lot = risk_points * tick_value
        if loss_per_lot <= 0:
            return 0.0
        lots = max(min_lot, round(risk_cash / loss_per_lot, 2))
        return min(lots, max_lot)
