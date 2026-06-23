"""TWK Momentum Strategy (9/15 EMA + ATR momentum + engulfing/no-wick).

Reconstructed from a YouTube "secret momentum EMA" strategy. The "secret"
indicator is simply 9 EMA + 15 EMA highlighted when ATR-based momentum is
high. No proprietary indicator needed.

System (operates on M5 bars assembled from the M1 feed):
  1. MOMENTUM : 9 EMA & 15 EMA separated ("railway track") AND current
                candle range > ATR*mult (the "white EMA" condition).
  2. PULLBACK : price touches the EMA zone during the trend.
  3. STRENGTH : entry on engulfing OR no-wick candle in trend direction.
  Entry at candle close, SL beyond the signal candle, TP = RR * risk.

Backtest (74 days XAUUSD M5): 41 trades, 53.7% WR, +0.341R at RR=1.5.
NOTE: loses on M15 in the same data — this is an M5 strategy despite the
video's "works on every timeframe" claim. Validate live before trusting.

PAPER MODE by default.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Optional

from loguru import logger

from app.core.types import Bar, Side
from app.strategy.broker_time import detect_broker_offset_hours, to_utc

# ── Parameters ──
EMA_FAST       = 9
EMA_SLOW       = 15
ATR_LEN        = 14
ATR_MULT       = 1.0
TRACK_MIN_PCT  = 0.10     # min EMA separation as % of price for "railway track"
RR_TARGET      = 1.5
WICK_TOL_FRAC  = 0.10     # no-wick tolerance as fraction of candle range
SL_BUF_FRAC    = 0.10     # SL buffer beyond signal candle as fraction of range
MAX_RISK_DIST  = 20.0     # skip if SL distance too large ($)
MIN_RISK_DIST  = 1.5

# Session in TRUE UTC — trade active liquidity only (London + NY).
SESSION_START  = time(7, 0)
SESSION_END    = time(20, 0)
CLOSE_DEADLINE = time(20, 30)

PAPER_LOG_FILE = Path("logs/twk_momentum_paper.jsonl")


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


class TWKMomentumStrategy:
    def __init__(self, rr: float = RR_TARGET, risk_pct: float = 0.02,
                 paper: bool = True, atr_mult: float = ATR_MULT):
        self.rr            = rr
        self.risk_pct      = risk_pct
        self.paper         = paper
        self.atr_mult      = atr_mult
        self._broker_offset = 3
        self.strategy_name = "TWK_MOMENTUM" + ("_PAPER" if paper else "_LIVE")

        # M5 bar assembly from M1 feed
        self._m5_bucket: Optional[int] = None
        self._m5_o = self._m5_h = self._m5_l = self._m5_c = None
        self._m5_bars: deque = deque(maxlen=60)  # recent completed M5 bars

        self._current_day  = ""
        self._traded_today = False

        # Paper state
        self._paper_open:  dict = {}
        self._paper_ticket = 500000
        self._paper_equity = 1000.0

        if self.paper:
            PAPER_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"[{self.strategy_name}] PAPER MODE — logged to {PAPER_LOG_FILE}")

    def set_broker(self, broker) -> None:
        self._broker_offset = detect_broker_offset_hours(broker)

    # ── M5 assembly ────────────────────────────────────────────────────────

    def _update_m5(self, bar: Bar, utc_ts: datetime) -> Optional[dict]:
        """Fold an M1 bar into the current M5 bucket. Returns a completed
        M5 bar dict when a bucket closes, else None."""
        bucket = (utc_ts.hour * 60 + utc_ts.minute) // 5
        completed = None
        if self._m5_bucket is None:
            self._m5_bucket = bucket
            self._m5_o, self._m5_h, self._m5_l, self._m5_c = bar.open, bar.high, bar.low, bar.close
        elif bucket != self._m5_bucket:
            # close previous bucket
            completed = {'open': self._m5_o, 'high': self._m5_h,
                         'low': self._m5_l, 'close': self._m5_c}
            self._m5_bucket = bucket
            self._m5_o, self._m5_h, self._m5_l, self._m5_c = bar.open, bar.high, bar.low, bar.close
        else:
            self._m5_h = max(self._m5_h, bar.high)
            self._m5_l = min(self._m5_l, bar.low)
            self._m5_c = bar.close
        return completed

    def _ema(self, values, span):
        k = 2 / (span + 1)
        e = values[0]
        for v in values[1:]:
            e = v * k + e * (1 - k)
        return e

    def _atr(self, bars, n):
        if len(bars) < n + 1:
            return None
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i]['high'], bars[i]['low'], bars[i-1]['close']
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        if len(trs) < n:
            return None
        return sum(trs[-n:]) / n

    # ── Main handler ─────────────────────────────────────────────────────────

    def on_bar(self, bar: Bar, symbol: str = "XAUUSD") -> Optional[TradeSignal]:
        utc_ts = to_utc(bar.ts, self._broker_offset)
        day = utc_ts.strftime("%Y-%m-%d")
        t = utc_ts.time()

        if day != self._current_day:
            self._current_day = day
            self._traded_today = False
            if self.paper:
                logger.info(f"[{self.strategy_name}] New day: {day}")

        # paper fill checks on every M1 bar (finer granularity)
        self._check_paper_fills(bar)

        completed = self._update_m5(bar, utc_ts)
        if completed is None:
            return None
        self._m5_bars.append(completed)

        if self._traded_today:
            return None
        if not (SESSION_START <= t < SESSION_END):
            return None
        if len(self._m5_bars) < EMA_SLOW + ATR_LEN + 2:
            return None

        bars = list(self._m5_bars)
        closes = [b['close'] for b in bars]
        ema9  = self._ema(closes[-(EMA_FAST+10):], EMA_FAST)
        ema15 = self._ema(closes[-(EMA_SLOW+10):], EMA_SLOW)
        atr = self._atr(bars, ATR_LEN)
        if atr is None or atr <= 0:
            return None

        row = bars[-1]; prev = bars[-2]
        rng = row['high'] - row['low']
        if rng <= 0:
            return None

        is_mom = rng > atr * self.atr_mult
        sep = abs(ema9 - ema15)
        min_sep = row['close'] * TRACK_MIN_PCT / 100.0
        track_up = ema9 > ema15 and sep > min_sep
        track_dn = ema9 < ema15 and sep > min_sep
        near_ema = row['low'] <= max(ema9, ema15) and row['high'] >= min(ema9, ema15)

        is_bull = row['close'] > row['open']
        is_bear = row['close'] < row['open']
        wick_tol = rng * WICK_TOL_FRAC
        nowick_bull = is_bull and (row['high'] - row['close']) <= wick_tol
        nowick_bear = is_bear and (row['close'] - row['low']) <= wick_tol
        bull_eng = (is_bull and prev['close'] < prev['open']
                    and row['close'] >= prev['open'] and row['open'] <= prev['close'])
        bear_eng = (is_bear and prev['close'] > prev['open']
                    and row['close'] <= prev['open'] and row['open'] >= prev['close'])

        long_sig  = track_up and is_mom and near_ema and (bull_eng or nowick_bull)
        short_sig = track_dn and is_mom and near_ema and (bear_eng or nowick_bear)

        if not (long_sig or short_sig):
            return None

        entry = row['close']
        if long_sig:
            sl = row['low'] - rng * SL_BUF_FRAC
            risk = entry - sl
            side = Side.BUY
            tp = entry + risk * self.rr
        else:
            sl = row['high'] + rng * SL_BUF_FRAC
            risk = sl - entry
            side = Side.SELL
            tp = entry - risk * self.rr

        if risk < MIN_RISK_DIST or risk > MAX_RISK_DIST:
            return None

        self._traded_today = True
        note = f"TWK M5 {('BULL' if side==Side.BUY else 'BEAR')} ema9={ema9:.1f} ema15={ema15:.1f} atr={atr:.1f}"

        if self.paper:
            self._open_paper(bar.ts, symbol, side, entry, sl, tp, risk, note)
            return None

        return TradeSignal(ts=bar.ts, symbol=symbol, side=side, entry=entry,
                           sl=sl, tp=tp, risk_dist=risk,
                           strategy=self.strategy_name, note=note)

    # ── Paper sim ────────────────────────────────────────────────────────────

    def _open_paper(self, ts, symbol, side, entry, sl, tp, risk, note):
        self._paper_ticket += 1
        ticket = self._paper_ticket
        lots = max(0.01, round((self._paper_equity * self.risk_pct) / (risk * 100), 2))
        risk_ccy = risk * 100 * lots
        self._paper_open[ticket] = {'ts': ts, 'side': side, 'entry': entry,
                                    'sl': sl, 'tp': tp, 'lots': lots, 'risk_ccy': risk_ccy}
        logger.info(f"[{self.strategy_name}] PAPER OPEN #{ticket} {side.value} {lots} "
                    f"@ {entry:.2f} SL={sl:.2f} TP={tp:.2f} risk=${risk_ccy:.2f} | {note}")
        self._log({'event':'OPEN','ticket':ticket,'time':ts.isoformat(),'side':side.value,
                   'entry':entry,'sl':sl,'tp':tp,'lots':lots,'risk_ccy':risk_ccy})

    def _check_paper_fills(self, m1: Bar):
        closed = []
        for ticket, m in self._paper_open.items():
            if m['side'] == Side.BUY:
                if m1.low <= m['sl']: self._close_paper(ticket, m['sl'], m1.ts, "LOSS"); closed.append(ticket)
                elif m1.high >= m['tp']: self._close_paper(ticket, m['tp'], m1.ts, "WIN"); closed.append(ticket)
            else:
                if m1.high >= m['sl']: self._close_paper(ticket, m['sl'], m1.ts, "LOSS"); closed.append(ticket)
                elif m1.low <= m['tp']: self._close_paper(ticket, m['tp'], m1.ts, "WIN"); closed.append(ticket)
        for t in closed:
            del self._paper_open[t]

    def _close_paper(self, ticket, exit_price, ts, outcome):
        m = self._paper_open[ticket]
        pts = ((exit_price - m['entry']) if m['side'] == Side.BUY
               else (m['entry'] - exit_price)) * 100
        pnl = pts * m['lots']
        r = pnl / m['risk_ccy'] if m['risk_ccy'] > 0 else 0
        self._paper_equity += pnl
        emoji = "✅" if outcome == "WIN" else "❌"
        logger.info(f"[{self.strategy_name}] {emoji} PAPER CLOSE #{ticket} {outcome} "
                    f"@ {exit_price:.2f} pnl=${pnl:+.2f} ({r:+.2f}R) eq=${self._paper_equity:.2f}")
        self._log({'event':'CLOSE','ticket':ticket,'time':ts.isoformat(),'exit':exit_price,
                   'outcome':outcome,'pnl':pnl,'r_multiple':r,'paper_equity':self._paper_equity})

    def _log(self, event):
        try:
            with open(PAPER_LOG_FILE, 'a') as f:
                f.write(json.dumps(event) + "\n")
        except Exception as e:
            logger.warning(f"paper log failed: {e}")

    # ── Engine compatibility ──────────────────────────────────────────────────

    def should_close_eod(self, bar: Bar) -> bool:
        return to_utc(bar.ts, self._broker_offset).time() >= CLOSE_DEADLINE

    def compute_lots(self, equity, risk_dist, point=0.01, tick_value=1.0,
                     min_lot=0.01, max_lot=1.0):
        if self.paper:
            return 0.0
        risk_cash = equity * self.risk_pct
        loss_per_lot = (risk_dist / point) * tick_value
        return min(max_lot, max(min_lot, round(risk_cash / loss_per_lot, 2))) if loss_per_lot > 0 else 0.0
