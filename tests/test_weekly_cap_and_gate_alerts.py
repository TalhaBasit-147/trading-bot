"""Weekly loss cap resets at the UTC week boundary; risk-gate blocks are alerted."""
from __future__ import annotations

from datetime import datetime, timezone

import app.strategy.risk as risk_mod
from app.strategy.risk import RiskManager
from tests.test_multi_symbol_engine import _engine


def _at(monkeypatch, dt):
    monkeypatch.setattr(risk_mod, "_utc_now", lambda: dt)


def test_tripped_weekly_cap_resets_at_monday_00_utc_without_a_day_roll(monkeypatch):
    _at(monkeypatch, datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc))        # Tuesday W47
    rm = RiskManager(starting_equity=324.65)
    _at(monkeypatch, datetime(2026, 11, 20, 0, 1, tzinfo=timezone.utc))         # losses spread over the week
    rm.on_new_day(294.64)
    _at(monkeypatch, datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc))
    rm.sync_equity(286.58)                                    # -2.7% today (under the daily cap), -11.7% for the week
    ok, why = rm.can_trade()
    assert not ok and "weekly loss cap" in why
    # Sunday 22:01 UTC: the broker's day roll calls on_new_day, but the UTC week is still W47
    _at(monkeypatch, datetime(2026, 11, 22, 22, 1, tzinfo=timezone.utc))
    rm.on_new_day(286.58)
    ok, why = rm.can_trade()
    assert not ok and "weekly loss cap" in why
    # Monday 00:00 UTC: new week, no further day roll needed
    _at(monkeypatch, datetime(2026, 11, 23, 0, 0, tzinfo=timezone.utc))
    ok, why = rm.can_trade()
    assert ok, why
    assert rm.state.week_start_equity == 286.58


def test_weekly_cap_still_holds_within_the_week(monkeypatch):
    _at(monkeypatch, datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc))
    rm = RiskManager(starting_equity=1000)
    rm.sync_equity(890)
    _at(monkeypatch, datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc))      # Saturday, same week
    rm.on_new_day(890)
    ok, why = rm.can_trade()
    assert not ok and "weekly loss cap" in why


def _sent(eng):
    return [str(c.args[0]) for c in eng.notifier.send.call_args_list]


def test_loss_cap_block_and_release_are_alerted_once(monkeypatch, tmp_path):
    eng, _ = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    eng._report_risk_gate(False, "weekly loss cap hit (-11.70%)")
    eng._report_risk_gate(False, "weekly loss cap hit (-11.95%)")      # same block, new number: no spam
    assert len(_sent(eng)) == 1 and "weekly loss cap" in _sent(eng)[0]
    eng._report_risk_gate(True, "ok")
    assert len(_sent(eng)) == 2 and "allowed again" in _sent(eng)[1]
    eng._report_risk_gate(True, "ok")
    assert len(_sent(eng)) == 2


def test_engine_bar_with_tripped_weekly_cap_alerts_and_does_not_trade(monkeypatch, tmp_path):
    from app.core.types import Bar
    eng, _ = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    eng.risk.state.week_start_equity = 1000.0
    eng.risk.state.equity_start_of_day = 880.0
    eng.risk.sync_equity(880.0)                      # -12% this week, 0% today
    called = []
    for s in eng._live_by_symbol["XAUUSD"]:
        monkeypatch.setattr(s, "on_bar", lambda *a, **k: called.append(1))
    eng._on_bar("XAUUSD", Bar(ts=datetime(2026, 11, 20, 8, 0), open=1, high=1, low=1, close=1, volume=1))
    assert called == [] and any("weekly loss cap" in m for m in _sent(eng))


def test_routine_gates_are_logged_not_alerted(monkeypatch, tmp_path):
    eng, _ = _engine(monkeypatch, tmp_path, "XAUUSD,BTCUSD")
    eng._report_risk_gate(False, "max trades per day reached")
    eng._report_risk_gate(True, "ok")
    eng._report_risk_gate(False, "cool-down until 2026-11-23T10:00:00+00:00")
    assert len(_sent(eng)) == 1 and "cool-down" in _sent(eng)[0]
