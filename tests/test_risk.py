from datetime import datetime, timedelta, timezone

from app.config import settings
from app.strategy.risk import RiskManager


def test_position_size_basic():
    rm = RiskManager(starting_equity=10_000)
    # XAUUSD-like: point 0.01, tick_value 1.0 per 1 lot
    lots = rm.position_size_lots(
        symbol="XAUUSD", entry=2000.00, sl=1998.00,
        point=0.01, contract_size=100, tick_value=1.0,
    )
    # risk_cash = 200 USD (RISK_PER_TRADE=0.02). dist_points = 200.
    # loss_per_lot = 200. lots = 1.0
    assert lots == 1.0


def test_daily_loss_cap_stops_trading():
    rm = RiskManager(starting_equity=10_000)
    rm.sync_equity(10_000 - 650)  # 6.5% loss > 6% cap (MAX_DAILY_LOSS)
    ok, reason = rm.can_trade()
    assert not ok
    assert "daily loss cap" in reason


def test_consecutive_loss_cooldown():
    # This gate (2 losses in a row -> cooldown) only matters once more than one
    # trade/day is allowed; bump the cap so it isn't masked by the unrelated
    # "max trades per day" gate (production default is 1).
    orig_max_trades = settings.MAX_TRADES_PER_DAY
    settings.MAX_TRADES_PER_DAY = 10
    try:
        rm = RiskManager(starting_equity=10_000)
        now = datetime.now(timezone.utc)
        rm.record_trade_open(); rm.record_trade_close(-10, now)
        rm.record_trade_open(); rm.record_trade_close(-10, now)
        ok, reason = rm.can_trade(now + timedelta(minutes=5))
        assert not ok
        assert "cool-down" in reason
        # after cool-down expires (CONSEC_LOSS_COOLDOWN_HOURS=2)
        ok, _ = rm.can_trade(now + timedelta(hours=3))
        assert ok
    finally:
        settings.MAX_TRADES_PER_DAY = orig_max_trades


def test_pause_resume():
    rm = RiskManager(starting_equity=10_000)
    rm.pause("test")
    ok, reason = rm.can_trade()
    assert not ok and "paused" in reason
    rm.resume()
    ok, _ = rm.can_trade()
    assert ok
