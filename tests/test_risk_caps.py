"""Lot sizing never rounds a trade above its risk target, and the order guard
refuses oversize or over-risk orders whatever the strategy computed.

Regression for the 2026-11-10 BTC trade: 2% of $307 / ($388.44 stop x 0.01 $/pt)
= 0.0158 lots was rounded UP to 0.02, so the trade risked $7.77 = 2.53%."""
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.core.types import Side
from app.strategy.prev_day_breakout import PrevDayBreakoutStrategy, TradeSignal
from app.strategy.prev_week_breakout import PrevWeekBreakoutStrategy
from app.strategy.sizing import floor_to_step, order_block_reason, risk_lots
from app.symbols import SymbolConfigError, load_symbol_configs
from tests.test_multi_symbol_engine import _engine

BTC = dict(point=0.01, tick_value=0.01)


def test_the_btc_trade_now_rounds_down():
    lots = risk_lots(307.0, 0.02, 388.44, **BTC)
    assert lots == 0.01                                   # was 0.02 (2.53% risk)
    assert (388.44 / 0.01) * 0.01 * lots <= 307.0 * 0.02


@pytest.mark.parametrize("strategy", [PrevDayBreakoutStrategy(risk_pct=0.02), PrevWeekBreakoutStrategy(risk_pct=0.02, paper=False)])
def test_live_strategies_never_exceed_their_risk_target(strategy):
    for equity in (250, 307, 999, 5000):
        for dist in (3.0, 4.7, 9.99, 15.0):
            lots = strategy.compute_lots(equity, dist, point=0.01, tick_value=1.0, min_lot=0.01, max_lot=100)
            if lots > 0.01:                               # above the broker minimum: must fit the 2% target
                assert dist * 100 * lots <= equity * 0.02 + 1e-9


def test_floor_is_float_safe():
    assert floor_to_step(0.07, 0.01) == 0.07            # 0.07/0.01 = 6.999999... in floats
    assert floor_to_step(0.129, 0.01) == 0.12
    assert floor_to_step(1.0, 0.1) == 1.0 and floor_to_step(0.35, 0.1) == 0.3


def test_guard_blocks_oversize_and_overrisk_and_allows_normal():
    ok = dict(equity=300.0, risk_dist=10.0, point=0.01, tick_value=1.0, max_lots=0.10, max_risk_pct=0.05)
    assert order_block_reason(0.01, **ok) is None                         # $10 = 3.3%: a normal min-lot gold trade
    assert "lot cap" in order_block_reason(0.5, **ok)
    assert "% of" in order_block_reason(0.02, **{**ok, "risk_dist": 10.0})  # $20 = 6.7%
    assert "equity" in order_block_reason(0.01, **{**ok, "equity": 0.0})
    assert "invalid" in order_block_reason(float("nan"), **ok)


def test_max_lots_is_per_symbol_and_validated():
    cfgs = {c.symbol: c for c in load_symbol_configs({"TRADE_SYMBOLS": "XAUUSD,BTCUSD", "PRIMARY_SYMBOL": "XAUUSD",
                                                      "BTCUSD_PRICE_SCALE": "69.23", "BTCUSD_MAX_LOTS": "0.03"})}
    assert cfgs["BTCUSD"].max_lots == 0.03 and cfgs["XAUUSD"].max_lots == settings.MAX_LOTS
    with pytest.raises(SymbolConfigError):
        load_symbol_configs({"TRADE_SYMBOLS": "XAUUSD", "PRIMARY_SYMBOL": "XAUUSD", "XAUUSD_MAX_LOTS": "0"})


def test_engine_refuses_and_alerts_an_over_risk_order(monkeypatch, tmp_path):
    eng, _ = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    sent, placed = [], []
    eng.notifier.send = lambda text: sent.append(text)
    eng.broker.place_market = lambda *a, **k: placed.append(a)
    pd_gold = eng._live_by_symbol["XAUUSD"][0]
    sig = TradeSignal(ts=datetime(2026, 6, 2, 8, 0), symbol="XAUUSD", side=Side.BUY, entry=4000.0, sl=3600.0, tp=4480.0,
                      risk_dist=400.0, strategy="PREV_DAY_BREAKOUT", note="test")      # absurd $400 stop: min lot loses $400
    eng._execute(pd_gold, sig)
    assert placed == [] and sent and "blocked" in sent[0]
