"""half_be exit mode: split sizing, config, order placement, runner management,
end-of-day closing and the MT5 'no take-profit' order path.

Every test uses a fake broker or a fake MetaTrader5 module; nothing here can
reach a real terminal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.main as main_mod
import app.strategy.news_filter as news_mod
from app.config import settings
from app.core.types import Bar, Position, Side
from app.execution.base import Broker
from app.strategy.exits import FIXED, HALF_BE, normalize_exit_mode, split_lots
from app.strategy.prev_day_breakout import TradeSignal
from app.symbols import SymbolConfigError, load_symbol_configs

GOLD = {"point": 0.01, "digits": 2, "contract_size": 100, "tick_value": 1.0, "spread_points": 20,
        "min_lot": 0.01, "max_lot": 100, "lot_step": 0.01}


# ── fake broker ─────────────────────────────────────────────────────────────
class FakeBroker(Broker):
    def __init__(self, equity=1000.0, hedging=True):
        self.equity, self.hedging = equity, hedging
        self.bid, self.ask = 4000.0, 4000.2
        self.positions: dict = {}
        self.realized: dict = {}
        self.orders, self.modifies, self.closes = [], [], []
        self.next_ticket = 1000
        self.fail_comment_suffix = None      # reject orders whose comment ends with this
        self.modify_ok = True
        self.close_fails = 0                 # number of close() calls that silently fail
        self.close_pnl: dict = {}            # ticket -> pnl booked when close() succeeds

    def connect(self): return True
    def disconnect(self): pass
    def account_equity(self): return self.equity
    def account_balance(self): return self.equity
    def symbol_info(self, symbol): return dict(GOLD)
    def get_bars(self, symbol, timeframe, n): return []
    def tick(self, symbol): return {"bid": self.bid, "ask": self.ask, "time": datetime.now(timezone.utc)}
    def can_hold_split_positions(self): return self.hedging

    def place_market(self, symbol, side, lots, sl, tp, comment=""):
        self.orders.append(dict(symbol=symbol, side=side, lots=lots, sl=sl, tp=tp, comment=comment))
        if self.fail_comment_suffix and comment.endswith(self.fail_comment_suffix):
            return None
        self.next_ticket += 1
        fill = self.ask if side == Side.BUY else self.bid
        p = Position(symbol=symbol, side=side, qty=lots, entry=fill, sl=sl, tp=tp or 0.0,
                     open_ts=datetime.now(timezone.utc), ticket=self.next_ticket, comment=comment)
        self.positions[p.ticket] = p
        return p

    def modify_sl_tp(self, ticket, sl, tp):
        self.modifies.append((ticket, sl, tp))
        if not self.modify_ok or ticket not in self.positions:
            return False
        self.positions[ticket].sl, self.positions[ticket].tp = sl, (tp or 0.0)
        return True

    def close(self, ticket):
        self.closes.append(ticket)
        if self.close_fails > 0:
            self.close_fails -= 1
            return 0.0
        if self.positions.pop(ticket, None) is not None:
            self.realized[ticket] = self.close_pnl.get(ticket, 0.0)
        return self.realized.get(ticket, 0.0)

    def open_positions(self, symbol=None):
        return [p for p in self.positions.values() if symbol is None or p.symbol == symbol]

    def realized_pnl(self, ticket):
        return self.realized.get(ticket)

    # test helper: the broker closed a position by itself (TP / SL hit)
    def hit(self, ticket, pnl):
        self.positions.pop(ticket)
        self.realized[ticket] = pnl


def _engine(monkeypatch, tmp_path, broker, exit_mode="half_be"):
    monkeypatch.chdir(tmp_path)                                     # no .env
    monkeypatch.setattr(settings, "TRADE_SYMBOLS", "")
    monkeypatch.setattr(settings, "RISK_PER_TRADE", 0.02)
    monkeypatch.setattr(settings, "EXIT_MODE", exit_mode)
    monkeypatch.setattr(news_mod, "is_news_blackout", lambda *a, **k: (False, ""))
    for name in ("ORB5MinStrategy", "PrevDayBreakoutStrategy", "PrevWeekBreakoutStrategy", "TWKMomentumStrategy"):
        monkeypatch.setattr(getattr(main_mod, name), "set_broker", lambda self, b: None, raising=False)
    monkeypatch.setattr(main_mod, "detect_broker_offset_hours", lambda b, *a: 3.0)
    monkeypatch.setattr(main_mod, "confirmed_broker_offset", lambda: None)
    eng = main_mod.Engine(broker, MagicMock())
    return eng, eng._live_by_symbol["XAUUSD"][0]


def _sig(side=Side.BUY, entry=4000.2, risk=10.0, rr=1.2):
    sl = entry - risk if side == Side.BUY else entry + risk
    tp = entry + risk * rr if side == Side.BUY else entry - risk * rr
    return TradeSignal(ts=datetime(2026, 11, 16, 6, 0), symbol="XAUUSD", side=side, entry=entry, sl=sl, tp=tp,
                       risk_dist=risk, strategy="PREV_DAY_BREAKOUT", note="test")


def _sent(eng):
    return " | ".join(str(c.args[0]) for c in eng.notifier.send.call_args_list)


@pytest.fixture
def clock(monkeypatch):
    t = {"now": 1000.0}
    monkeypatch.setattr(main_mod.time_mod, "monotonic", lambda: t["now"])
    return t


# ── split sizing ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("lots,expected", [
    (0.01, None), (0.02, (0.01, 0.01)), (0.03, (0.02, 0.01)), (0.05, (0.03, 0.02)),
    (0.10, (0.05, 0.05)), (0.0, None), (-0.02, None),
])
def test_split_lots(lots, expected):
    assert split_lots(lots, 0.01, 0.01) == expected


def test_split_lots_always_adds_up_and_respects_min_lot():
    for n in range(2, 300):
        lots = round(n * 0.01, 2)
        t, r = split_lots(lots, 0.01, 0.01)
        assert t >= r >= 0.01 and abs(t + r - lots) < 1e-9


def test_split_lots_other_steps_and_bad_input():
    assert split_lots(0.2, 0.1, 0.1) == (0.1, 0.1)
    assert split_lots(0.1, 0.1, 0.1) is None
    assert split_lots("x", 0.01, 0.01) is None
    assert split_lots(0.02, 0, 0.01) is None


# ── configuration ───────────────────────────────────────────────────────────
def test_exit_mode_config_defaults_overrides_and_validation():
    base = {"TRADE_SYMBOLS": "XAUUSD,BTCUSD", "PRIMARY_SYMBOL": "XAUUSD", "BTCUSD_PRICE_SCALE": "69.23"}
    assert [c.exit_mode for c in load_symbol_configs(base)] == [FIXED, FIXED]
    cfg = load_symbol_configs({**base, "EXIT_MODE": "half_be", "XAUUSD_EXIT_MODE": " Fixed "})
    assert [c.exit_mode for c in cfg] == [FIXED, HALF_BE]
    with pytest.raises(SymbolConfigError):
        load_symbol_configs({**base, "BTCUSD_EXIT_MODE": "trail"})
    assert normalize_exit_mode("HALF_BE") == HALF_BE and normalize_exit_mode(None) is None


# ── entry: fixed / fallbacks ────────────────────────────────────────────────
def test_fixed_mode_places_one_order_with_target(monkeypatch, tmp_path):
    b = FakeBroker()
    eng, pd = _engine(monkeypatch, tmp_path, b, exit_mode="fixed")
    eng._execute(pd, _sig())
    assert len(b.orders) == 1 and b.orders[0]["lots"] == 0.02 and b.orders[0]["tp"] == pytest.approx(4012.2)
    assert "runner_ticket" not in eng._open_positions["PREV_DAY_BREAKOUT"]


def test_half_be_with_min_lot_falls_back_to_fixed(monkeypatch, tmp_path):
    b = FakeBroker(equity=600.0)              # 2% = $12 -> 0.01 lots on a $10 stop
    eng, pd = _engine(monkeypatch, tmp_path, b)
    eng._execute(pd, _sig())
    assert len(b.orders) == 1 and b.orders[0]["lots"] == 0.01 and b.orders[0]["tp"] > 0
    assert "runner_ticket" not in eng._open_positions["PREV_DAY_BREAKOUT"]


def test_half_be_on_a_netting_account_falls_back_to_fixed(monkeypatch, tmp_path):
    b = FakeBroker(hedging=False)
    eng, pd = _engine(monkeypatch, tmp_path, b)
    eng._execute(pd, _sig())
    assert len(b.orders) == 1 and b.orders[0]["lots"] == 0.02 and b.orders[0]["tp"] > 0


# ── entry: split ────────────────────────────────────────────────────────────
def test_half_be_opens_target_leg_and_runner_without_tp(monkeypatch, tmp_path):
    b = FakeBroker()
    eng, pd = _engine(monkeypatch, tmp_path, b)
    eng._execute(pd, _sig())
    tgt, run = b.orders
    assert (tgt["lots"], run["lots"]) == (0.01, 0.01)
    assert tgt["tp"] == pytest.approx(4012.2) and run["tp"] is None
    assert tgt["sl"] == run["sl"] == pytest.approx(3990.2)
    assert run["comment"].endswith("_R")
    meta = eng._open_positions["PREV_DAY_BREAKOUT"]
    assert meta["runner_ticket"] != meta["ticket"] and meta["lots"] == pytest.approx(0.02)
    assert meta["risk_ccy"] == pytest.approx(20.0)          # whole trade = 2% of $1000, unchanged by the split
    assert eng._sym["XAUUSD"].trades_today == 1 and len(eng._open_positions) == 1


def test_runner_stop_is_reanchored_to_its_own_fill(monkeypatch, tmp_path):
    b = FakeBroker()
    eng, pd = _engine(monkeypatch, tmp_path, b)
    real_place = b.place_market
    def place(symbol, side, lots, sl, tp, comment=""):
        if comment.endswith("_R"):
            b.ask = 4001.0                     # runner fills $0.80 worse
        return real_place(symbol, side, lots, sl, tp, comment)
    b.place_market = place
    eng._execute(pd, _sig())
    meta = eng._open_positions["PREV_DAY_BREAKOUT"]
    assert meta["runner_entry"] == pytest.approx(4001.0)
    assert (meta["runner_ticket"], pytest.approx(3991.0), None) in [(t, s, tp) for t, s, tp in b.modifies]


def test_runner_rejected_leaves_target_leg_alone_and_alerts(monkeypatch, tmp_path):
    b = FakeBroker(); b.fail_comment_suffix = "_R"
    eng, pd = _engine(monkeypatch, tmp_path, b)
    eng._execute(pd, _sig())
    meta = eng._open_positions["PREV_DAY_BREAKOUT"]
    assert "runner_ticket" not in meta and meta["lots"] == pytest.approx(0.01)
    assert meta["risk_ccy"] == pytest.approx(10.0)
    assert "runner leg rejected" in _sent(eng)


# ── runner management ───────────────────────────────────────────────────────
def _open_split(monkeypatch, tmp_path, side=Side.BUY):
    b = FakeBroker()
    eng, pd = _engine(monkeypatch, tmp_path, b)
    entry = b.ask if side == Side.BUY else b.bid
    eng._execute(pd, _sig(side=side, entry=entry))
    meta = eng._open_positions["PREV_DAY_BREAKOUT"]
    return eng, b, meta


def test_target_banked_moves_runner_stop_to_breakeven_buy(monkeypatch, tmp_path, clock):
    eng, b, meta = _open_split(monkeypatch, tmp_path)
    b.hit(meta["ticket"], +12.0); b.bid, b.ask = 4012.3, 4012.5
    eng._tick()
    assert (meta["runner_ticket"], pytest.approx(4000.2), None) in b.modifies
    assert b.positions[meta["runner_ticket"]].sl == pytest.approx(4000.2)
    assert meta["runner_protected"] and "PREV_DAY_BREAKOUT" in eng._open_positions
    assert "breakeven" in _sent(eng)
    n = len(b.modifies); eng._tick()
    assert len(b.modifies) == n                               # done once, not every loop


def test_target_banked_moves_runner_stop_to_breakeven_sell(monkeypatch, tmp_path, clock):
    eng, b, meta = _open_split(monkeypatch, tmp_path, side=Side.SELL)
    b.hit(meta["ticket"], +12.0); b.bid, b.ask = 3987.8, 3988.0
    eng._tick()
    assert b.positions[meta["runner_ticket"]].sl == pytest.approx(4000.0)


def test_breakeven_not_placeable_closes_runner_at_market(monkeypatch, tmp_path, clock):
    eng, b, meta = _open_split(monkeypatch, tmp_path)
    b.hit(meta["ticket"], +12.0); b.bid, b.ask = 4000.1, 4000.3       # price already back at entry
    b.close_pnl[meta["runner_ticket"]] = -0.10
    eng._tick()
    assert meta["runner_ticket"] in b.closes and not b.modifies[1:]  # only the (absent) re-anchor, no BE
    eng._tick()                                                       # both legs gone -> booked
    assert "PREV_DAY_BREAKOUT" not in eng._open_positions


def test_refused_breakeven_is_retried_then_runner_closed(monkeypatch, tmp_path, clock):
    eng, b, meta = _open_split(monkeypatch, tmp_path)
    b.hit(meta["ticket"], +12.0); b.bid, b.ask = 4012.3, 4012.5; b.modify_ok = False
    for _ in range(eng.BE_MAX_TRIES - 1):
        eng._tick()
    assert meta["runner_ticket"] not in b.closes
    eng._tick()
    assert meta["runner_ticket"] in b.closes


def test_target_leg_stopped_closes_the_runner(monkeypatch, tmp_path, clock):
    eng, b, meta = _open_split(monkeypatch, tmp_path)
    b.hit(meta["ticket"], -10.0)
    b.bid, b.ask = 4012.3, 4012.5        # even with price far in profit, a lost target leg ends the trade
    eng._tick()
    assert meta["runner_ticket"] in b.closes
    assert not [m for m in b.modifies if m[1] == pytest.approx(4000.2)]   # no breakeven move


def test_trade_is_booked_once_with_both_legs_pnl(monkeypatch, tmp_path, clock):
    eng, b, meta = _open_split(monkeypatch, tmp_path)
    booked = []
    real = eng._record_close
    eng._record_close = lambda sn, t, pnl, bar: (booked.append((sn, t, pnl)), real(sn, t, pnl, bar))
    b.hit(meta["ticket"], +12.0); b.bid, b.ask = 4012.3, 4012.5
    for _ in range(eng.PNL_WAIT_PASSES + 5):                 # runner keeps riding: nothing is booked
        eng._tick()
    assert booked == [] and "PREV_DAY_BREAKOUT" in eng._open_positions
    b.hit(meta["runner_ticket"], +25.0)
    eng._tick(); eng._tick()
    assert booked == [("PREV_DAY_BREAKOUT", meta["ticket"], pytest.approx(37.0))]
    assert eng._sym["XAUUSD"].last_outcome_today == "WIN"
    assert "+1.85R" in _sent(eng)                            # $37 on $20 total risk


def test_booking_waits_for_deal_history_then_books_known_part(monkeypatch, tmp_path, clock):
    eng, b, meta = _open_split(monkeypatch, tmp_path)
    b.hit(meta["ticket"], -10.0)
    b.positions.pop(meta["runner_ticket"])                   # runner gone, but no deal history yet
    for _ in range(eng.PNL_WAIT_PASSES):
        eng._tick()
        assert "PREV_DAY_BREAKOUT" in eng._open_positions
    eng._tick()
    assert "PREV_DAY_BREAKOUT" not in eng._open_positions


def test_runner_stopped_while_target_open_leaves_target_alone(monkeypatch, tmp_path, clock):
    eng, b, meta = _open_split(monkeypatch, tmp_path)
    b.hit(meta["runner_ticket"], -10.0)
    eng._tick()
    assert b.closes == [] and meta["ticket"] in b.positions and "PREV_DAY_BREAKOUT" in eng._open_positions


# ── end of day ──────────────────────────────────────────────────────────────
def test_eod_closes_both_legs_and_retries_a_failed_close(monkeypatch, tmp_path, clock):
    eng, b, meta = _open_split(monkeypatch, tmp_path)
    b.close_fails = 1                                        # the first close attempt silently fails
    bar = Bar(ts=datetime(2026, 11, 16, 21, 31), open=4005, high=4005, low=4005, close=4005, volume=1)
    eng._force_close("PREV_DAY_BREAKOUT", bar)
    assert "PREV_DAY_BREAKOUT" in eng._open_positions        # never booked while a leg is still open
    clock["now"] += 5; eng._force_close("PREV_DAY_BREAKOUT", bar)
    n = len(b.closes)
    assert n == 2                                            # rate-limited: no retry within 10 s
    clock["now"] += 11; eng._force_close("PREV_DAY_BREAKOUT", bar)
    assert not b.positions
    eng._tick()
    assert "PREV_DAY_BREAKOUT" not in eng._open_positions


def test_stuck_close_alerts_once(monkeypatch, tmp_path, clock):
    eng, b, meta = _open_split(monkeypatch, tmp_path)
    b.close_fails = 10**6
    bar = Bar(ts=datetime(2026, 11, 16, 21, 31), open=4005, high=4005, low=4005, close=4005, volume=1)
    for _ in range(10):
        eng._force_close("PREV_DAY_BREAKOUT", bar); clock["now"] += 11
    assert _sent(eng).count("will not close") == 2         # one alert per stuck leg
    assert "PREV_DAY_BREAKOUT" in eng._open_positions


def test_eod_bar_routes_split_trade_through_engine(monkeypatch, tmp_path, clock):
    eng, b, meta = _open_split(monkeypatch, tmp_path)
    pd = eng._live_by_symbol["XAUUSD"][0]
    monkeypatch.setattr(type(pd), "should_close_eod", lambda self, bar: True)
    eng._on_bar("XAUUSD", Bar(ts=datetime(2026, 11, 16, 21, 31), open=4005, high=4005, low=4005, close=4005, volume=1))
    assert set(b.closes) == {meta["ticket"], meta["runner_ticket"]}


# ── startup ─────────────────────────────────────────────────────────────────
def test_orphan_positions_are_reported(monkeypatch, tmp_path):
    b = FakeBroker()
    b.place_market("XAUUSD", Side.BUY, 0.01, 3990.0, None, comment="PREV_DAY_BUY_R")
    eng, _ = _engine(monkeypatch, tmp_path, b)
    eng._report_orphans()
    assert "NOT managed" in _sent(eng) and "TP=none" in _sent(eng)


def _orphan(b, comment, opened_broker_clock):
    p = b.place_market("XAUUSD", Side.BUY, 0.01, 3990.0, None, comment=comment)
    p.open_ts = opened_broker_clock                          # MT5 position time = broker clock (test offset +3)
    return p.ticket


class _FrozenNow(datetime):
    now_utc = datetime(2026, 11, 16, 18, 29, tzinfo=timezone.utc)
    @classmethod
    def now(cls, tz=None):
        return cls.now_utc


def test_untracked_runner_is_closed_only_after_its_deadline(monkeypatch, tmp_path, clock):
    b = FakeBroker()
    eng, _ = _engine(monkeypatch, tmp_path, b)
    monkeypatch.setattr(main_mod, "datetime", _FrozenNow)
    runner = _orphan(b, "PREV_DAY_BUY_R", datetime(2026, 11, 16, 9, 5, tzinfo=timezone.utc))   # 06:05 UTC
    target = _orphan(b, "PREV_DAY_BUY", datetime(2026, 11, 16, 9, 5, tzinfo=timezone.utc))
    _FrozenNow.now_utc = datetime(2026, 11, 16, 18, 29, tzinfo=timezone.utc)
    eng._manage_all_splits()
    assert b.closes == []                                    # 18:29 UTC: not yet
    _FrozenNow.now_utc = datetime(2026, 11, 16, 18, 31, tzinfo=timezone.utc)
    eng._manage_all_splits()
    assert b.closes == []                                    # rate-limited (sweep every 30 s)
    clock["now"] += 31; eng._manage_all_splits()
    assert b.closes == [runner] and target in b.positions    # only the TP-less runner leg
    assert "untracked runner" in _sent(eng)


def test_sweep_reads_the_open_time_on_the_broker_clock(monkeypatch, tmp_path, clock):
    b = FakeBroker()
    eng, _ = _engine(monkeypatch, tmp_path, b)
    monkeypatch.setattr(main_mod, "datetime", _FrozenNow)
    # 20:00 broker clock = 17:00 UTC: its deadline is 18:30 UTC the SAME day.
    runner = _orphan(b, "PREV_WEE_SELL_R", datetime(2026, 11, 16, 20, 0, tzinfo=timezone.utc))
    _FrozenNow.now_utc = datetime(2026, 11, 16, 18, 31, tzinfo=timezone.utc)
    eng._manage_all_splits()
    assert b.closes == [runner]


def test_tracked_runner_is_never_swept(monkeypatch, tmp_path, clock):
    eng, b, meta = _open_split(monkeypatch, tmp_path)
    monkeypatch.setattr(main_mod, "datetime", _FrozenNow)
    _FrozenNow.now_utc = datetime(2026, 11, 17, 3, 0, tzinfo=timezone.utc)
    b.positions[meta["runner_ticket"]].open_ts = datetime(2026, 11, 16, 9, 5, tzinfo=timezone.utc)
    clock["now"] += 100; eng._manage_all_splits()
    assert meta["runner_ticket"] not in b.closes


def test_management_errors_never_escape_the_loop(monkeypatch, tmp_path, clock):
    eng, b, meta = _open_split(monkeypatch, tmp_path)
    b.open_positions = lambda symbol=None: (_ for _ in ()).throw(RuntimeError("terminal gone"))
    eng._manage_all_splits()                                 # must not raise


# ── MT5 broker: the 'no take-profit' order path (fake MetaTrader5 module) ───
def _fake_mt5(margin_mode=2):
    sent = []
    info = SimpleNamespace(point=0.01, digits=2, trade_stops_level=0, trade_freeze_level=0)
    m = SimpleNamespace(
        ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, TRADE_ACTION_DEAL=1, TRADE_ACTION_SLTP=6,
        ORDER_TIME_GTC=0, ORDER_FILLING_IOC=1, ORDER_FILLING_FOK=0, ORDER_FILLING_RETURN=2,
        TRADE_RETCODE_DONE=10009, ACCOUNT_MARGIN_MODE_RETAIL_HEDGING=2,
        symbol_info=lambda s: info, symbol_select=lambda s, on: True,
        symbol_info_tick=lambda s: SimpleNamespace(bid=4000.0, ask=4000.2),
        order_send=lambda req: (sent.append(dict(req)), SimpleNamespace(retcode=10009, order=77, price=4000.2, deal=0))[1],
        positions_get=lambda **k: [SimpleNamespace(symbol="XAUUSD")],
        account_info=lambda: SimpleNamespace(margin_mode=margin_mode),
    )
    return m, sent


def _mt5_broker(monkeypatch, fake):
    import app.execution.mt5_broker as mb
    monkeypatch.setattr(mb, "mt5", fake)
    monkeypatch.setattr(mb, "MT5_AVAILABLE", True)
    return mb.MT5Broker()


def test_mt5_order_without_tp_sends_zero_not_a_clamped_tp(monkeypatch):
    fake, sent = _fake_mt5()
    br = _mt5_broker(monkeypatch, fake)
    br.place_market("XAUUSD", Side.BUY, 0.01, 3990.2, None, comment="x_R")
    br.place_market("XAUUSD", Side.SELL, 0.01, 4010.0, 0.0, comment="y_R")
    assert [r["tp"] for r in sent] == [0.0, 0.0]
    br.place_market("XAUUSD", Side.BUY, 0.01, 3990.2, 4012.2)
    assert sent[-1]["tp"] == 4012.2                          # a real TP is unchanged


def test_mt5_modify_with_no_tp_sends_zero(monkeypatch):
    fake, sent = _fake_mt5()
    br = _mt5_broker(monkeypatch, fake)
    assert br.modify_sl_tp(77, 4000.2, None)
    assert sent[-1]["tp"] == 0.0 and sent[-1]["sl"] == 4000.2


@pytest.mark.parametrize("mode,expected", [(2, True), (0, False), (1, False)])
def test_mt5_hedging_detection(monkeypatch, mode, expected):
    fake, _ = _fake_mt5(margin_mode=mode)
    assert _mt5_broker(monkeypatch, fake).can_hold_split_positions() is expected


def test_mt5_hedging_detection_fails_safe(monkeypatch):
    fake, _ = _fake_mt5()
    fake.account_info = lambda: None
    assert _mt5_broker(monkeypatch, fake).can_hold_split_positions() is False
