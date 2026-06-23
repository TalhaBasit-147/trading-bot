"""NY Open 5-Minute ORB Strategy (Delayed Entry).

Backtest on 73 days XAUUSD M1 (Feb-Jun 2026):
  68 trades, 72.1% WR, +0.441R expectancy, $1000→$1,812

Logic:
  1. Mark the ORB using only the FIRST 5 minutes of NY session
     (13:30-13:34 UTC = 18:30-18:34 broker bar time UTC+5)
  2. Wait 15 minutes for manipulation to finish (delay window)
  3. After 13:49 UTC (18:49 broker), enter on first breakout of ORB H/L
  4. SL = other side of ORB. TP = 1.0R (equal to risk)
  5. Max ORB range: $15 (wide ranges = no trade)
  6. One trade per day max
  7. Fresh-cross guard: skip if overshoot > $3
  8. News blackout filter: skip during NFP/CPI/FOMC ±30min

PAPER MODE by default. Set paper=False to go live.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Optional

from loguru import logger

from app.core.types import Bar, Side
from app.strategy.broker_time import detect_broker_offset_hours, to_utc


# ── Session times in TRUE UTC ─────────────────────────────────────────────
# IMPORTANT: these are UTC, NOT broker time. bar.ts (broker server time) is
# converted to UTC at the top of on_bar() using a dynamically-detected offset,
# so broker DST shifts can never silently break the NY-open timing again.
ORB_START      = time(13, 30)   # NY open
ORB_END        = time(13, 35)   # end of 5-min ORB
ENTRY_START    = time(13, 49)   # after 15-min delay (manipulation done)
ENTRY_END      = time(15, 30)   # entry window closes
CLOSE_DEADLINE = time(16, 0)    # EOD close

# ── Risk parameters ────────────────────────────────────────────────────────
MAX_ORB_RANGE  = 15.0   # skip if ORB wider than this
MIN_ORB_RANGE  = 2.0    # skip if ORB too narrow (no volatility)
SL_BUFFER      = 0.21   # small buffer beyond ORB level for entry/SL
RR_TARGET      = 1.0    # TP = 1.0R (equal to risk) — key to high WR
MAX_OVERSHOOT  = 3.0    # skip if price already ran > $3 past ORB level

# ── Paper mode ─────────────────────────────────────────────────────────────
PAPER_MODE     = True
PAPER_LOG_FILE = Path("logs/orb5min_paper.jsonl")


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


class ORB5MinStrategy:
    def __init__(self, rr: float = RR_TARGET, risk_pct: float = 0.02,
                 paper: bool = PAPER_MODE):
        self.rr            = rr
        self.risk_pct      = risk_pct
        self.paper         = paper
        self._broker_offset: int = 3  # detected in set_broker(); UTC+3 fallback
        self.strategy_name = "ORB_5MIN" + ("_PAPER" if paper else "_LIVE")

        self._current_day  = ""
        self._traded_today = False
        self._orb_bars: list = []
        self._orb_high: Optional[float] = None
        self._orb_low:  Optional[float] = None
        self._orb_formed   = False
        self._was_inside   = None  # fresh-cross tracker

        # Paper sim state
        self._paper_open:   dict = {}
        self._paper_ticket  = 300000
        self._paper_equity  = 1000.0

        if self.paper:
            PAPER_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"[{self.strategy_name}] PAPER MODE — "
                        f"signals logged to {PAPER_LOG_FILE}")

    def set_broker(self, broker) -> None:
        # Detect broker server offset from UTC dynamically (robust to DST).
        self._broker_offset = detect_broker_offset_hours(broker)

    # ── Day reset ─────────────────────────────────────────────────────────

    def _reset_day(self, new_day: str) -> None:
        self._current_day  = new_day
        self._traded_today = False
        self._orb_bars     = []
        self._orb_high     = None
        self._orb_low      = None
        self._orb_formed   = False
        self._was_inside   = None

    # ── Main bar handler ──────────────────────────────────────────────────

    def on_bar(self, bar: Bar, symbol: str = "XAUUSD") -> Optional[TradeSignal]:
        # Convert broker-server timestamp to TRUE UTC. All constants are UTC.
        utc_ts = to_utc(bar.ts, self._broker_offset)
        day = utc_ts.strftime("%Y-%m-%d")
        t   = utc_ts.time()

        if day != self._current_day:
            self._reset_day(day)

        # Check paper fills every bar
        self._check_paper_fills(bar)

        if self._traded_today:
            return None

        # Step 1: Collect ORB bars (13:30-13:34 UTC = 18:30-18:34 broker)
        if ORB_START <= t < ORB_END:
            self._orb_bars.append(bar)
            return None

        # Step 2: Finalise ORB when we pass 18:35
        if not self._orb_formed and t >= ORB_END:
            if len(self._orb_bars) < 3:
                # Not enough bars — skip today
                self._traded_today = True
                return None
            self._orb_high = max(b.high for b in self._orb_bars)
            self._orb_low  = min(b.low  for b in self._orb_bars)
            orb_range = self._orb_high - self._orb_low
            if orb_range < MIN_ORB_RANGE or orb_range > MAX_ORB_RANGE:
                logger.info(f"[{self.strategy_name}] ORB range ${orb_range:.1f} "
                            f"outside [{MIN_ORB_RANGE}, {MAX_ORB_RANGE}] — skip today")
                self._traded_today = True
                return None
            self._orb_formed = True
            logger.info(f"[{self.strategy_name}] ORB formed: "
                        f"H={self._orb_high:.2f} L={self._orb_low:.2f} "
                        f"range=${orb_range:.1f}")

        if not self._orb_formed:
            return None

        # Step 3: Wait for entry window (after 15-min delay)
        if t < ENTRY_START or t >= ENTRY_END:
            return None

        # News blackout
        try:
            from app.strategy.news_filter import is_news_blackout
            blocked, reason = is_news_blackout()
            if blocked:
                logger.info(f"[{self.strategy_name}] NEWS BLACKOUT: {reason}")
                return None
        except ImportError:
            pass

        oh = self._orb_high
        ol = self._orb_low
        inside_now = ol <= bar.close <= oh

        # First bar in entry window — record state, never trade
        if self._was_inside is None:
            self._was_inside = inside_now
            return None

        # Fresh cross detection
        crossed_up   = self._was_inside and bar.high > oh
        crossed_down = self._was_inside and bar.low  < ol
        self._was_inside = inside_now

        side  = None
        entry = None
        sl    = None

        if crossed_up:
            overshoot = bar.close - oh
            if overshoot > MAX_OVERSHOOT:
                logger.info(f"[{self.strategy_name}] BUY overshoot "
                            f"${overshoot:.1f} > ${MAX_OVERSHOOT} — skip")
                return None
            side  = Side.BUY
            entry = oh + SL_BUFFER
            sl    = ol - SL_BUFFER

        elif crossed_down:
            overshoot = ol - bar.close
            if overshoot > MAX_OVERSHOOT:
                logger.info(f"[{self.strategy_name}] SELL overshoot "
                            f"${overshoot:.1f} > ${MAX_OVERSHOOT} — skip")
                return None
            side  = Side.SELL
            entry = ol - SL_BUFFER
            sl    = oh + SL_BUFFER

        if side is None:
            return None

        risk_dist = abs(entry - sl)
        tp = (entry + risk_dist * self.rr if side == Side.BUY
              else entry - risk_dist * self.rr)

        self._traded_today = True

        if self.paper:
            self._open_paper(bar.ts, symbol, side, entry, sl, tp, risk_dist)
            return None

        return TradeSignal(
            ts=bar.ts, symbol=symbol, side=side,
            entry=entry, sl=sl, tp=tp, risk_dist=risk_dist,
            strategy=self.strategy_name,
            note=(f"ORB5 H={oh:.2f} L={ol:.2f} "
                  f"range=${oh-ol:.1f} RR={self.rr}"),
        )

    # ── Paper simulation ───────────────────────────────────────────────────

    def _open_paper(self, ts, symbol, side, entry, sl, tp, risk_dist):
        self._paper_ticket += 1
        ticket   = self._paper_ticket
        lots     = max(0.01, round(
            (self._paper_equity * self.risk_pct) / (risk_dist * 100), 2))
        risk_ccy = risk_dist * 100 * lots
        self._paper_open[ticket] = {
            'ts': ts, 'symbol': symbol, 'side': side,
            'entry': entry, 'sl': sl, 'tp': tp,
            'lots': lots, 'risk_ccy': risk_ccy,
        }
        logger.info(
            f"[{self.strategy_name}] PAPER OPEN #{ticket} "
            f"{side.value} {lots} {symbol} @ {entry:.2f} "
            f"SL={sl:.2f} TP={tp:.2f} risk=${risk_ccy:.2f}"
        )
        self._log({'event': 'OPEN', 'ticket': ticket,
                   'time': ts.isoformat(), 'symbol': symbol,
                   'side': side.value, 'entry': entry,
                   'sl': sl, 'tp': tp, 'lots': lots, 'risk_ccy': risk_ccy})

    def _check_paper_fills(self, m1: Bar) -> None:
        closed = []
        for ticket, meta in self._paper_open.items():
            side = meta['side']
            if side == Side.BUY:
                if m1.low  <= meta['sl']: self._close_paper(ticket, meta['sl'],  m1.ts, "LOSS"); closed.append(ticket)
                elif m1.high >= meta['tp']: self._close_paper(ticket, meta['tp'], m1.ts, "WIN");  closed.append(ticket)
            else:
                if m1.high >= meta['sl']: self._close_paper(ticket, meta['sl'],  m1.ts, "LOSS"); closed.append(ticket)
                elif m1.low  <= meta['tp']: self._close_paper(ticket, meta['tp'], m1.ts, "WIN");  closed.append(ticket)
        for t in closed:
            del self._paper_open[t]

    def _close_paper(self, ticket, exit_price, ts, outcome):
        meta    = self._paper_open[ticket]
        side    = meta['side']
        pts     = ((exit_price - meta['entry']) if side == Side.BUY
                   else (meta['entry'] - exit_price)) * 100
        pnl     = pts * meta['lots']
        r       = pnl / meta['risk_ccy'] if meta['risk_ccy'] > 0 else 0
        self._paper_equity += pnl
        emoji   = "✅" if outcome == "WIN" else "❌"
        logger.info(
            f"[{self.strategy_name}] {emoji} PAPER CLOSE #{ticket} "
            f"{outcome} @ {exit_price:.2f} pnl=${pnl:+.2f} "
            f"({r:+.2f}R) paper_eq=${self._paper_equity:.2f}"
        )
        self._log({'event': 'CLOSE', 'ticket': ticket,
                   'time': ts.isoformat(), 'exit': exit_price,
                   'outcome': outcome, 'pnl': pnl, 'r_multiple': r,
                   'paper_equity': self._paper_equity})

    def _log(self, event: dict) -> None:
        try:
            with open(PAPER_LOG_FILE, 'a') as f:
                f.write(json.dumps(event) + "\n")
        except Exception as e:
            logger.warning(f"paper log failed: {e}")

    # ── Engine compatibility ───────────────────────────────────────────────

    def should_close_eod(self, bar: Bar) -> bool:
        return to_utc(bar.ts, self._broker_offset).time() >= CLOSE_DEADLINE

    def compute_lots(self, equity, risk_dist, point=0.01, tick_value=1.0,
                     min_lot=0.01, max_lot=1.0):
        if self.paper:
            return 0.0
        risk_cash = equity * self.risk_pct
        loss_per_lot = (risk_dist / point) * tick_value
        return min(max_lot, max(min_lot, round(risk_cash / loss_per_lot, 2))) if loss_per_lot > 0 else 0.0
