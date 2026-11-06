"""Multi-symbol configuration loader (app/symbols.py)."""
import pytest

from app.config import settings
from app.symbols import KNOWN_STRATEGIES, PREV_WEEK_DEFAULT_RR, SymbolConfigError, load_symbol_configs


def test_unset_trade_symbols_means_primary_only_with_global_defaults():
    cfgs = load_symbol_configs({"PRIMARY_SYMBOL": "XAUUSD"})
    assert len(cfgs) == 1
    c = cfgs[0]
    assert c.symbol == "XAUUSD"
    assert c.strategies == KNOWN_STRATEGIES
    assert c.price_scale == 1.0
    assert c.risk_per_trade == settings.RISK_PER_TRADE
    assert c.rr_prev_day == settings.RR_TARGET
    assert c.rr_prev_week == PREV_WEEK_DEFAULT_RR
    assert c.max_trades_per_day == settings.MAX_TRADES_PER_DAY


def test_second_symbol_reads_its_own_keys():
    cfgs = load_symbol_configs({
        "PRIMARY_SYMBOL": "XAUUSD", "TRADE_SYMBOLS": "xauusd, btcusd",
        "BTCUSD_PRICE_SCALE": "69.23", "BTCUSD_STRATEGIES": "prev_week", "BTCUSD_RISK_PER_TRADE": "0.01",
        "BTCUSD_RR_PREV_DAY": "1.5", "BTCUSD_RR_PREV_WEEK": "2.0", "BTCUSD_MAX_TRADES_PER_DAY": "2",
    })
    assert [c.symbol for c in cfgs] == ["XAUUSD", "BTCUSD"]
    btc = cfgs[1]
    assert btc.price_scale == pytest.approx(69.23)
    assert btc.strategies == ("PREV_WEEK",)
    assert (btc.risk_per_trade, btc.rr_prev_day, btc.rr_prev_week, btc.max_trades_per_day) == (0.01, 1.5, 2.0, 2)
    assert cfgs[0].price_scale == 1.0          # gold untouched by BTC keys


def test_non_primary_symbol_without_price_scale_is_refused():
    # Gold-dollar stops on BTC would be nonsense; refusing beats guessing.
    with pytest.raises(SymbolConfigError, match="BTCUSD_PRICE_SCALE is required"):
        load_symbol_configs({"PRIMARY_SYMBOL": "XAUUSD", "TRADE_SYMBOLS": "XAUUSD,BTCUSD"})


@pytest.mark.parametrize("env, msg", [
    ({"TRADE_SYMBOLS": "XAUUSD,BTCUSD", "BTCUSD_PRICE_SCALE": "0"}, "must be > 0"),
    ({"TRADE_SYMBOLS": "XAUUSD,BTCUSD", "BTCUSD_PRICE_SCALE": "abc"}, "not a valid number"),
    ({"TRADE_SYMBOLS": "XAUUSD,BTCUSD", "BTCUSD_PRICE_SCALE": "69", "BTCUSD_STRATEGIES": "ORB"}, "STRATEGIES"),
    ({"TRADE_SYMBOLS": "XAUUSD,BTCUSD", "BTCUSD_PRICE_SCALE": "69", "BTCUSD_RISK_PER_TRADE": "0.9"}, "RISK_PER_TRADE"),
    ({"TRADE_SYMBOLS": "XAUUSD,BTCUSD", "BTCUSD_PRICE_SCALE": "69", "BTCUSD_MAX_TRADES_PER_DAY": "0"}, "MAX_TRADES_PER_DAY"),
    ({"TRADE_SYMBOLS": "XAUUSD,XAUUSD"}, "twice"),
    ({"TRADE_SYMBOLS": "BTCUSD", "BTCUSD_PRICE_SCALE": "69"}, "must include PRIMARY_SYMBOL"),
])
def test_bad_configs_are_refused_at_startup(env, msg):
    with pytest.raises(SymbolConfigError, match=msg):
        load_symbol_configs({"PRIMARY_SYMBOL": "XAUUSD", **env})
