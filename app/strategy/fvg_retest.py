"""Fair Value Gap (FVG) Retest Strategy — ICT-compliant rules.

PAPER MODE ONLY by default. This strategy logs signals and simulates fills
but does not place real orders. Run alongside the live PREV_DAY_BREAKOUT to
collect real-time validation data without risk.

Rules (sourced from Huddleston / ICT / Equiti / FundedTradingPlus):

1. FVG = 3 consecutive M5 candles where:
   - Bullish (BISI): c2.low > c0.high AND c1 is bullish AND c1 is impulsive
   - Bearish (SIBI): c2.high < c0.low AND c1 is bearish AND c1 is impulsive
   - Impulsive = c1 body >= 1.0 * ATR(14)

2. Entry: CE (Consequent Encroachment) = 50% midpoint of FVG
   - "Most reactive level" per ICT documentation
   - Wait for price to retrace into the FVG and tag CE

3. Stop loss: BEYOND THE FVG EXTREME
   - Bullish: below c0.low - small buffer
   - Bearish: above c0.high + small buffer
   - This is critical — SL is NOT just past the entry candle

4. Take profit: 2.5R (raised from 1.5R after backtesting validation)

5. Invalidation: if price closes through the FVG extreme before retest, kill setup

6. Session: London kill zone only (07:00-12:00 broker time)
   - NY consistently underperformed in backtest

7. One trade per day max. One FVG can only be traded once.

Backtest on 73 days XAUUSD M1 (Feb-May 2026), AT RR=1.5 (now RR_TARGET=2.5,
see rule 4 — these figures predate that change and haven't been re-run at 2.5):
   64 trades, 54.7% WR, +0.367R expectancy, PF 1.81
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, time, timezone
from pathlib import Path
from typing import List, Optional

from loguru import logger

from app.core.types import Bar, Side
from app.strategy.broker_time import detect_broker_offset_hours, to_utc


# ============== ICT FVG configuration ==============

# Session: London kill zone in TRUE UTC (bar.ts converted via dynamic offset).
# Backtest used 07:00-12:00 on UTC timestamps, so we keep 07:00-12:00 UTC.
SESSION_START = time(7, 0)
SESSION_END = time(12, 0)
CLOSE_DEADLINE = time(18, 0)  # force close any open trades at EOD (UTC)

# Detection
M5_BARS_PER_M1 = 5
IMPULSE_ATR_MULT = 1.0   # middle candle body must be >= this * ATR(14)
ATR_PERIOD = 14
MIN_GAP_SIZE = 0.5       # minimum dollar size of the FVG to consider

# Entry / risk
SL_BUFFER = 0.5          # dollars beyond FVG extreme
MIN_RISK_DIST = 1.5
MAX_RISK_DIST = 15.0
RR_TARGET = 2.5

# Paper-only by default. Set to False to enable live trading (NOT recommended yet)
PAPER_MODE = True
PAPER_LOG_FILE = Path("logs/fvg_paper.jsonl")


# ============== Data types ==============

@dataclass
class M5Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    @property
    def body(self) -> float:
        return abs(self.close - self.open)
    @property
    def is_bullish(self) -> bool:
        return self.close > self.open
    @property
    def is_bearish(self) -> bool:
        return self.close < self.open


@dataclass
class FVG:
    """Represents a detected Fair Value Gap awaiting retest."""
    formed_at: datetime
    side: str           # 'bullish' or 'bearish'
    fvg_low: float      # gap bottom edge
    fvg_high: float     # gap top edge
    sl_anchor: float    # c0 low (bullish) or c0 high (bearish) — SL goes BEYOND this
    ce: float           # 50% midpoint
    state: str = "ACTIVE"   # ACTIVE | RETESTED | INVALIDATED | EXPIRED
    
    @property
    def gap_size(self) -> float:
        return self.fvg_high - self.fvg_low


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


# ============== Strategy ==============

class FVGRetestStrategy:
    """ICT-compliant FVG retest at CE level. Paper mode by default."""

    def __init__(self, rr: float = RR_TARGET, risk_pct: float = 0.02, paper: bool = PAPER_MODE):
        self.rr = rr
        self.risk_pct = risk_pct
        self.paper = paper
        self.strategy_name = "FVG_RETEST" + ("_PAPER" if paper else "_LIVE")

        # M5 bar assembly from incoming M1 bars
        self._m5_current: List[Bar] = []
        self._m5_history: List[M5Bar] = []

        # Active FVGs awaiting retest
        self._active_fvgs: List[FVG] = []

        # Daily state
        self._current_day = ""
        self._traded_today = False

        # Paper trades currently "open" (simulated)
        self._paper_open: dict = {}  # ticket → meta
        self._paper_ticket_counter = 100000
        self._paper_equity = 1000.0  # tracking equity for paper sim only

        if self.paper:
            PAPER_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"[{self.strategy_name}] PAPER MODE — signals logged to {PAPER_LOG_FILE}")

    # ---- compatibility with the engine's broker injection ----
    def set_broker(self, broker) -> None:
        self._broker_offset = detect_broker_offset_hours(broker)

    # ---- M5 bar assembly ----
    def _update_m5(self, m1: Bar) -> Optional[M5Bar]:
        """Aggregate M1 bars into M5. Returns a completed M5 bar when one closes."""
        if not self._m5_current:
            self._m5_current.append(m1)
            return None
        first_ts = self._m5_current[0].ts
        # M5 buckets align to clock 5-minute boundaries
        first_bucket = first_ts.replace(second=0, microsecond=0)
        first_bucket = first_bucket.replace(minute=(first_bucket.minute // 5) * 5)
        this_bucket = m1.ts.replace(second=0, microsecond=0)
        this_bucket = this_bucket.replace(minute=(this_bucket.minute // 5) * 5)

        if this_bucket == first_bucket:
            self._m5_current.append(m1)
            return None
        # different bucket → close previous M5
        bars = self._m5_current
        m5 = M5Bar(
            ts=first_bucket,
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            close=bars[-1].close,
        )
        self._m5_current = [m1]
        return m5

    # ---- ATR ----
    def _atr(self) -> float:
        if len(self._m5_history) < ATR_PERIOD + 1: return 0.0
        recent = self._m5_history[-ATR_PERIOD:]
        return sum(b.high - b.low for b in recent) / ATR_PERIOD

    # ---- FVG detection ----
    def _detect_fvg(self) -> Optional[FVG]:
        """Check whether the last 3 M5 bars form a valid FVG."""
        if len(self._m5_history) < 3: return None
        c0 = self._m5_history[-3]
        c1 = self._m5_history[-2]
        c2 = self._m5_history[-1]

        atr = self._atr()
        if atr <= 0: return None
        if c1.body < IMPULSE_ATR_MULT * atr: return None

        # Bullish FVG
        if c2.low > c0.high and c1.is_bullish:
            gap = c2.low - c0.high
            if gap < MIN_GAP_SIZE: return None
            return FVG(
                formed_at=c2.ts, side='bullish',
                fvg_low=c0.high, fvg_high=c2.low,
                sl_anchor=c0.low,
                ce=(c0.high + c2.low) / 2,
            )
        # Bearish FVG
        if c2.high < c0.low and c1.is_bearish:
            gap = c0.low - c2.high
            if gap < MIN_GAP_SIZE: return None
            return FVG(
                formed_at=c2.ts, side='bearish',
                fvg_low=c2.high, fvg_high=c0.low,
                sl_anchor=c0.high,
                ce=(c2.high + c0.low) / 2,
            )
        return None

    # ---- main entry point ----
    def on_bar(self, bar: Bar, symbol: str = "XAUUSD") -> Optional[TradeSignal]:
        utc_ts = to_utc(bar.ts, getattr(self, "_broker_offset", 3))
        day = utc_ts.strftime("%Y-%m-%d")
        t = utc_ts.time()

        # New day reset
        if day != self._current_day:
            self._current_day = day
            self._traded_today = False
            self._active_fvgs.clear()  # FVGs don't carry across days (M5 session-bound)
            logger.info(f"[{self.strategy_name}] New day: {day}")

        # Aggregate to M5
        m5 = self._update_m5(bar)
        if m5 is None:
            # Still check for paper SL/TP fills on current intra-bar prices
            self._check_paper_fills(bar)
            return None

        self._m5_history.append(m5)
        # Cap history size
        if len(self._m5_history) > 500:
            self._m5_history = self._m5_history[-500:]

        # Check existing FVGs for invalidation or retest using this completed M5
        signal = self._update_active_fvgs(m5, symbol)

        # Detect new FVG on the most recent 3 bars
        new_fvg = self._detect_fvg()
        if new_fvg is not None:
            # Only consider FVGs that form during the session
            if SESSION_START <= new_fvg.formed_at.time() < SESSION_END:
                self._active_fvgs.append(new_fvg)
                logger.info(
                    f"[{self.strategy_name}] NEW {new_fvg.side.upper()} FVG @ {new_fvg.formed_at} "
                    f"gap={new_fvg.fvg_low:.2f}-{new_fvg.fvg_high:.2f} CE={new_fvg.ce:.2f} "
                    f"size=${new_fvg.gap_size:.2f}"
                )

        # Check paper position fills (simulated SL/TP)
        self._check_paper_fills(bar)

        # Live signals fire from _update_active_fvgs -> _fire_signal; propagate
        # whatever it returned (None for paper mode / no fire / risk rejected).
        return signal

    def _update_active_fvgs(self, m5: M5Bar, symbol: str) -> Optional[TradeSignal]:
        """For each active FVG, check invalidation or retest. Emits at most
        one signal per call (one trade per day)."""
        if self._traded_today:
            return None
        if not (SESSION_START <= m5.ts.time() < SESSION_END):
            return None

        for fvg in self._active_fvgs:
            if fvg.state != "ACTIVE": continue

            # Invalidation: close beyond FVG extreme (against direction)
            if fvg.side == 'bullish' and m5.close < fvg.fvg_low:
                fvg.state = "INVALIDATED"
                logger.info(f"[{self.strategy_name}] FVG invalidated (close {m5.close:.2f} < low {fvg.fvg_low:.2f})")
                continue
            if fvg.side == 'bearish' and m5.close > fvg.fvg_high:
                fvg.state = "INVALIDATED"
                logger.info(f"[{self.strategy_name}] FVG invalidated (close {m5.close:.2f} > high {fvg.fvg_high:.2f})")
                continue

            # Retest of CE level
            if fvg.side == 'bullish':
                if m5.low <= fvg.ce <= m5.high or m5.low <= fvg.ce:
                    signal = self._fire_signal(fvg, m5, Side.BUY, symbol)
                    fvg.state = "RETESTED"
                    return signal  # one trade per day
            else:
                if m5.low <= fvg.ce <= m5.high or m5.high >= fvg.ce:
                    signal = self._fire_signal(fvg, m5, Side.SELL, symbol)
                    fvg.state = "RETESTED"
                    return signal
        return None

    def _fire_signal(self, fvg: FVG, m5: M5Bar, side: Side, symbol: str) -> Optional[TradeSignal]:
        entry = fvg.ce
        if side == Side.BUY:
            sl = fvg.sl_anchor - SL_BUFFER
            risk = entry - sl
        else:
            sl = fvg.sl_anchor + SL_BUFFER
            risk = sl - entry

        if risk < MIN_RISK_DIST or risk > MAX_RISK_DIST:
            logger.info(f"[{self.strategy_name}] Signal skipped — risk ${risk:.2f} out of bounds")
            return None

        tp = entry + risk * self.rr if side == Side.BUY else entry - risk * self.rr
        self._traded_today = True

        if self.paper:
            self._open_paper_trade(m5.ts, symbol, side, entry, sl, tp, risk, fvg)
            return None

        # Live mode — return the signal so Engine._tick()/_execute() places the
        # real order through the exact same path as ORB_5MIN and
        # PREV_DAY_BREAKOUT (position sizing via compute_lots(), SL/TP via
        # place_market(), MAX_TRADES_PER_DAY / daily-loss / FTMO guards all
        # live in the engine, not here).
        return TradeSignal(
            ts=m5.ts, symbol=symbol, side=side,
            entry=entry, sl=sl, tp=tp, risk_dist=risk,
            strategy=self.strategy_name,
            note=(f"FVG {fvg.side} CE={fvg.ce:.2f} "
                  f"gap={fvg.fvg_low:.2f}-{fvg.fvg_high:.2f} "
                  f"size=${fvg.gap_size:.2f} RR={self.rr}"),
        )

    # ---- Paper trade simulation ----
    def _open_paper_trade(self, ts, symbol, side, entry, sl, tp, risk, fvg):
        self._paper_ticket_counter += 1
        ticket = self._paper_ticket_counter
        lots = max(0.01, round((self._paper_equity * self.risk_pct) / (risk * 100), 2))
        risk_ccy = risk * 100 * lots

        self._paper_open[ticket] = {
            'ts': ts, 'symbol': symbol, 'side': side, 'entry': entry,
            'sl': sl, 'tp': tp, 'lots': lots, 'risk_ccy': risk_ccy,
            'fvg_size': fvg.gap_size, 'fvg_side': fvg.side,
        }

        logger.info(
            f"[{self.strategy_name}] PAPER OPEN #{ticket} {side.value} {lots} {symbol} "
            f"@ {entry:.2f} SL={sl:.2f} TP={tp:.2f} risk=${risk_ccy:.2f} "
            f"(FVG_size=${fvg.gap_size:.2f})"
        )
        self._log_paper_event({
            'event': 'OPEN', 'ticket': ticket, 'time': ts.isoformat(),
            'symbol': symbol, 'side': side.value,
            'entry': entry, 'sl': sl, 'tp': tp,
            'lots': lots, 'risk_ccy': risk_ccy,
            'fvg_size': fvg.gap_size, 'fvg_side': fvg.side,
        })

    def _check_paper_fills(self, m1: Bar) -> None:
        """Check if any paper-open trade hit SL or TP on this M1 bar."""
        closed_tickets = []
        for ticket, meta in self._paper_open.items():
            side = meta['side']
            sl, tp = meta['sl'], meta['tp']
            if side == Side.BUY:
                if m1.low <= sl:
                    self._close_paper_trade(ticket, sl, m1.ts, "LOSS")
                    closed_tickets.append(ticket)
                elif m1.high >= tp:
                    self._close_paper_trade(ticket, tp, m1.ts, "WIN")
                    closed_tickets.append(ticket)
            else:
                if m1.high >= sl:
                    self._close_paper_trade(ticket, sl, m1.ts, "LOSS")
                    closed_tickets.append(ticket)
                elif m1.low <= tp:
                    self._close_paper_trade(ticket, tp, m1.ts, "WIN")
                    closed_tickets.append(ticket)
        for t in closed_tickets:
            del self._paper_open[t]

    def _close_paper_trade(self, ticket, exit_price, ts, outcome):
        meta = self._paper_open[ticket]
        side = meta['side']
        if side == Side.BUY:
            pnl_pts = (exit_price - meta['entry']) * 100
        else:
            pnl_pts = (meta['entry'] - exit_price) * 100
        pnl = pnl_pts * meta['lots']
        r = pnl / meta['risk_ccy'] if meta['risk_ccy'] > 0 else 0
        self._paper_equity += pnl
        emoji = "✅" if outcome == "WIN" else "❌"

        logger.info(
            f"[{self.strategy_name}] {emoji} PAPER CLOSE #{ticket} {outcome} "
            f"@ {exit_price:.2f} pnl=${pnl:+.2f} ({r:+.2f}R) "
            f"paper_eq=${self._paper_equity:.2f}"
        )
        self._log_paper_event({
            'event': 'CLOSE', 'ticket': ticket, 'time': ts.isoformat(),
            'exit': exit_price, 'outcome': outcome,
            'pnl': pnl, 'r_multiple': r,
            'paper_equity': self._paper_equity,
        })

    def _log_paper_event(self, event: dict) -> None:
        try:
            with open(PAPER_LOG_FILE, 'a') as f:
                f.write(json.dumps(event) + "\n")
        except Exception as e:
            logger.warning(f"paper log write failed: {e}")

    # ---- engine compatibility ----
    def should_close_eod(self, bar: Bar) -> bool:
        return to_utc(bar.ts, getattr(self, '_broker_offset', 3)).time() >= CLOSE_DEADLINE

    def compute_lots(self, equity, risk_dist, point=0.01, tick_value=1.0,
                     min_lot=0.01, max_lot=1.0):
        # Paper strategy doesn't actually place via the engine — return 0.
        # Live: identical formula to ORB_5MIN/PrevDayBreakoutStrategy —
        # risk_cash / (risk in points * tick_value), clamped to [min_lot, max_lot].
        if self.paper:
            return 0.0
        risk_cash = equity * self.risk_pct
        loss_per_lot = (risk_dist / point) * tick_value
        if loss_per_lot <= 0:
            return 0.0
        return min(max_lot, max(min_lot, round(risk_cash / loss_per_lot, 2)))
