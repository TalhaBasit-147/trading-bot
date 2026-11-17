"""Restart-safe daily state, the wrong-account guard, and the Telegram label.

Uses fake brokers / a fake MetaTrader5 module only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.main as main_mod
from app.config import settings
from app.notify.telegram import Telegram
from app.strategy.daily_state import rebuild_day
from tests.test_multi_symbol_engine import _engine, _park_pw, _push, _seed_pd


def D(h, m, sym, pos, kind, pnl=0.0, comment="PREV_DAY_SELL", s=0):
    return {"time": datetime(2026, 11, 16, h, m, s), "symbol": sym, "position": pos, "kind": kind,
            "comment": comment, "pnl": pnl}


# ── replay rules ────────────────────────────────────────────────────────────
def test_16_nov_case_one_closed_win():
    d = rebuild_day([D(5, 21, "XAUUSD", 1, "in"), D(7, 49, "XAUUSD", 1, "out", 18.88)])
    g = d["XAUUSD"]
    assert (g.trades, g.last_outcome, g.recovery_taken, g.open_trades) == (1, "WIN", False, 0)


def test_loss_then_recovery_then_win():
    d = rebuild_day([D(5, 0, "XAUUSD", 1, "in"), D(6, 0, "XAUUSD", 1, "out", -15),
                     D(7, 0, "XAUUSD", 2, "in")])
    assert (d["XAUUSD"].trades, d["XAUUSD"].last_outcome, d["XAUUSD"].recovery_taken) == (2, "LOSS", True)
    d = rebuild_day([D(5, 0, "XAUUSD", 1, "in"), D(6, 0, "XAUUSD", 1, "out", -15),
                     D(7, 0, "XAUUSD", 2, "in"), D(8, 0, "XAUUSD", 2, "out", 18)])
    assert (d["XAUUSD"].last_outcome, d["XAUUSD"].recovery_taken) == ("WIN", False)


def test_half_be_legs_are_one_trade_closed_only_when_both_exit():
    deals = [D(5, 0, "BTCUSD", 10, "in", comment="PREV_DAY_BUY"),
             D(5, 0, "BTCUSD", 11, "in", comment="PREV_DAY_BUY_R", s=1),
             D(6, 0, "BTCUSD", 10, "out", 12.0)]
    b = rebuild_day(deals)["BTCUSD"]
    assert (b.trades, b.open_trades, b.last_outcome) == (1, 1, None)
    b = rebuild_day(deals + [D(9, 0, "BTCUSD", 11, "out", -0.1)])["BTCUSD"]
    assert (b.trades, b.open_trades, b.last_outcome, b.closes[0][1]) == (1, 0, "WIN", pytest.approx(11.9))


def test_unlinked_runner_counts_as_its_own_trade_and_old_positions_are_ignored():
    d = rebuild_day([D(5, 0, "XAUUSD", 1, "in"), D(5, 5, "XAUUSD", 2, "in", comment="X_R"),
                     D(4, 0, "XAUUSD", 99, "out", -50.0)])        # 99 opened yesterday
    assert d["XAUUSD"].trades == 2 and d["XAUUSD"].last_outcome is None


def test_symbols_are_independent():
    d = rebuild_day([D(5, 0, "XAUUSD", 1, "in"), D(6, 0, "XAUUSD", 1, "out", -10),
                     D(7, 0, "BTCUSD", 2, "in")])
    assert d["XAUUSD"].last_outcome == "LOSS" and d["BTCUSD"].trades == 1 and not d["BTCUSD"].recovery_taken


# ── engine restore ──────────────────────────────────────────────────────────
def test_restart_after_a_trade_blocks_a_second_trade_that_day(monkeypatch, tmp_path):
    """The 16 Nov incident, replayed: gold already traded (and won) before the
    restart; a fresh breakout later the same day must NOT be traded. BTC, which
    had not traded, still can."""
    eng, feed = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    eng.broker.deals_today = lambda syms: [
        {"time": datetime(2026, 6, 2, 6, 0), "symbol": "XAUUSD", "position": 1, "kind": "in", "comment": "PREV_DAY_SELL", "pnl": 0.0},
        {"time": datetime(2026, 6, 2, 7, 0), "symbol": "XAUUSD", "position": 1, "kind": "out", "comment": "[tp]", "pnl": 18.88}]
    eng._restore_daily_state()
    assert eng._sym["XAUUSD"].trades_today == 1 and eng._sym["XAUUSD"].current_day == "2026-06-02"
    assert eng.risk.state.daily.trades == 1 and eng.risk.state.daily.pnl_ccy == pytest.approx(18.88)

    t0 = datetime(2026, 6, 2, 8, 0)
    gpd, gpw = eng._live_by_symbol["XAUUSD"]; bpd, bpw = eng._live_by_symbol["BTCUSD"]
    _seed_pd(gpd, "2026-06-02", 4010.0, 3990.0); _seed_pd(bpd, "2026-06-02", 70000.0, 68000.0)
    _park_pw(gpw, t0); _park_pw(bpw, t0)
    for i, (g, b) in enumerate([((4000, 4002, 3998, 4001), (69000, 69050, 68950, 69010)),
                                ((4001, 4011, 4000, 4010.5), (69990, 70120, 69950, 70100))]):
        ts = t0 + timedelta(minutes=i)
        _push(feed, "XAUUSD", ts, *g); _push(feed, "BTCUSD", ts, *b)
    for sym in feed:
        _push(feed, sym, t0 + timedelta(minutes=2), *([feed[sym][-1].close] * 4))
    eng._tick()
    assert set(eng._open_positions) == {"PREV_DAY_BREAKOUT_BTCUSD"}     # gold blocked, BTC traded
    assert eng._sym["XAUUSD"].trades_today == 1


def test_restore_single_symbol_mode_uses_account_daily_cap(monkeypatch, tmp_path):
    eng, _ = _engine(monkeypatch, tmp_path, "")
    eng.broker.deals_today = lambda syms: [
        {"time": datetime(2026, 6, 2, 6, 0), "symbol": "XAUUSD", "position": 1, "kind": "in", "comment": "", "pnl": 0.0}]
    eng._restore_daily_state()
    ok, why = eng.risk.can_trade()
    assert not ok and "max trades" in why


def test_restore_without_history_is_a_no_op(monkeypatch, tmp_path):
    eng, _ = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    eng.broker.deals_today = lambda syms: []
    eng._restore_daily_state()
    assert eng._sym["XAUUSD"].trades_today == 0 and eng._sym["XAUUSD"].current_day == ""


def test_unreadable_history_alerts(monkeypatch, tmp_path):
    eng, _ = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    eng.broker.deals_today = lambda syms: (_ for _ in ()).throw(RuntimeError("terminal gone"))
    eng._restore_daily_state()
    assert "Could not read" in str(eng.notifier.send.call_args_list)


# ── wrong-account guard in the loop ─────────────────────────────────────────
def test_wrong_account_suspends_the_whole_tick_and_alerts_once(monkeypatch, tmp_path):
    eng, _ = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    clock = {"t": 100.0}
    monkeypatch.setattr(main_mod.time_mod, "monotonic", lambda: clock["t"])
    reason = {"v": "terminal is on account 111, but this bot is configured for 222"}
    eng.broker.wrong_account_reason = lambda: reason["v"]
    managed = []
    eng._manage_all_splits = lambda: managed.append(1)
    for _ in range(3):
        eng._tick(); clock["t"] += 10
    assert managed == []
    assert str(eng.notifier.send.call_args_list).count("WRONG ACCOUNT") == 1
    reason["v"] = None
    eng._tick()
    assert managed == [1] and "resumed" in str(eng.notifier.send.call_args_list)


# ── MT5 broker: account check, portable start, today's deals (fake module) ──
def _fake_mt5(login=222, deals=()):
    calls = {"init": None, "shutdown": 0}
    m = SimpleNamespace(
        DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_ENTRY_OUT_BY=4,
        initialize=lambda **k: (calls.__setitem__("init", k), True)[1],
        shutdown=lambda: calls.__setitem__("shutdown", calls["shutdown"] + 1),
        last_error=lambda: (0, ""),
        account_info=lambda: SimpleNamespace(login=login, server="S", equity=1000.0),
        terminal_info=lambda: SimpleNamespace(path="C:/MT5-Live"),
        symbol_info_tick=lambda s: SimpleNamespace(time=int(datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc).timestamp())),
        history_deals_get=lambda a, b: (calls.__setitem__("range", (a, b)), list(deals))[1],
    )
    return m, calls


def _mt5_broker(monkeypatch, fake, **cfg):
    import app.execution.mt5_broker as mb
    monkeypatch.setattr(mb, "mt5", fake); monkeypatch.setattr(mb, "MT5_AVAILABLE", True)
    for k, v in {"MT5_LOGIN": None, "MT5_PASSWORD": None, "MT5_SERVER": None, "MT5_PATH": None,
                 "MT5_PORTABLE": False, "MT5_EXPECTED_LOGIN": None, **cfg}.items():
        monkeypatch.setattr(settings, k, v)
    return mb.MT5Broker()


def test_connect_refuses_the_wrong_account(monkeypatch):
    fake, calls = _fake_mt5(login=111)
    br = _mt5_broker(monkeypatch, fake, MT5_EXPECTED_LOGIN=222)
    assert br.connect() is False and calls["shutdown"] == 1


def test_connect_accepts_the_right_account_and_passes_portable(monkeypatch):
    fake, calls = _fake_mt5(login=222)
    br = _mt5_broker(monkeypatch, fake, MT5_EXPECTED_LOGIN=222, MT5_PATH="C:/MT5-Live/terminal64.exe", MT5_PORTABLE=True)
    assert br.connect() is True
    assert calls["init"] == {"path": "C:/MT5-Live/terminal64.exe", "portable": True}
    assert br.wrong_account_reason() is None


def test_no_expectation_means_no_refusal(monkeypatch):
    fake, _ = _fake_mt5(login=111)
    assert _mt5_broker(monkeypatch, fake).connect() is True


def test_unreadable_account_is_refused(monkeypatch):
    fake, _ = _fake_mt5()
    fake.account_info = lambda: None
    br = _mt5_broker(monkeypatch, fake, MT5_EXPECTED_LOGIN=222)
    assert "unreadable" in br.wrong_account_reason()


def test_deals_today_filters_magic_and_starts_at_broker_midnight(monkeypatch):
    t = lambda h: int(datetime(2026, 11, 16, h, 0, tzinfo=timezone.utc).timestamp())
    deals = [SimpleNamespace(magic=20251116, entry=0, time=t(5), symbol="XAUUSD", position_id=1, comment="PD", profit=0, commission=0, swap=0),
             SimpleNamespace(magic=20251116, entry=1, time=t(7), symbol="XAUUSD", position_id=1, comment="[tp]", profit=19.0, commission=-0.07, swap=0),
             SimpleNamespace(magic=999, entry=0, time=t(8), symbol="XAUUSD", position_id=5, comment="manual", profit=0, commission=0, swap=0),
             SimpleNamespace(magic=20251116, entry=2, time=t(9), symbol="XAUUSD", position_id=6, comment="", profit=0, commission=0, swap=0)]
    fake, calls = _fake_mt5(deals=deals)
    br = _mt5_broker(monkeypatch, fake, MT5_MAGIC=20251116)
    br._magic = 20251116
    out = br.deals_today(["XAUUSD"])
    assert [(d["position"], d["kind"]) for d in out] == [(1, "in"), (1, "out")]
    assert out[1]["pnl"] == pytest.approx(18.93) and out[0]["time"] == datetime(2026, 11, 16, 5, 0)
    assert calls["range"][0] == datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc)


# ── Telegram label + startup refusal alert ──────────────────────────────────
def test_telegram_label_prefix():
    assert Telegram(token="t", chat_id="c", label="LIVE").format("hi") == "LIVE | hi"
    assert Telegram(token="t", chat_id="c", label="").format("hi") == "hi"


def test_telegram_send_applies_the_label(monkeypatch):
    import app.notify.telegram as tg
    posted = []
    class _Client:
        def __init__(self, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, data): posted.append(data)
    monkeypatch.setattr(tg.httpx, "Client", _Client)
    Telegram(token="t", chat_id="c", label="LIVE").send("trade opened")
    assert posted[0]["text"] == "LIVE | trade opened"


def test_main_alerts_when_broker_refuses(monkeypatch):
    sent = []
    broker = MagicMock(); broker.connect.return_value = False
    monkeypatch.setattr(main_mod, "setup_logging", lambda: None)
    monkeypatch.setattr(main_mod, "init_db", lambda: None)
    monkeypatch.setattr(main_mod, "_make_broker", lambda: broker)
    monkeypatch.setattr(main_mod, "Telegram", lambda: SimpleNamespace(send=sent.append))
    monkeypatch.setattr(main_mod, "Engine", lambda *a: pytest.fail("engine must not start"))
    main_mod.main()
    assert sent and "NOT started" in sent[0]
