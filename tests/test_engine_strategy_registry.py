"""Engine strategy registry: ORB_5MIN must run as PAPER (no real orders).

A 3-year DST-aware engine backtest had ORB_5MIN at -0.077R/trade inside the live
mix, so it was moved from `live_strategies` to `paper_strategies`. These tests pin
that wiring and prove the paper instance cannot place a real order while the live
one (same bars) would.
"""
import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

import app.main as main_mod
import app.strategy.orb_5min as orb_mod
from app.core.types import Bar, Side
from app.execution.paper_broker import PaperBroker
from app.strategy.orb_5min import ORB5MinStrategy


@pytest.fixture
def engine(monkeypatch, tmp_path):
    # Paper strategies create logs/ relative to the cwd; keep that out of the repo.
    monkeypatch.chdir(tmp_path)
    # Don't let strategies reach for MT5 history when the broker is injected.
    for name in ("ORB5MinStrategy", "PrevDayBreakoutStrategy", "PrevWeekBreakoutStrategy",
                 "FVGRetestStrategy", "TWKMomentumStrategy"):
        cls = getattr(main_mod, name, None)   # FVG may be absent once it is removed from main
        if cls is not None:
            monkeypatch.setattr(cls, "set_broker", lambda self, broker: None, raising=False)
    monkeypatch.setattr(main_mod, "detect_broker_offset_hours", lambda broker: 3.0)
    return main_mod.Engine(PaperBroker(starting_equity=1000.0), MagicMock())


def test_orb_is_paper_not_live(engine):
    live = [s.strategy_name for s in engine.live_strategies]
    paper = [s.strategy_name for s in engine.paper_strategies]
    assert "ORB_5MIN_LIVE" not in live
    assert "ORB_5MIN_PAPER" in paper


def test_prev_day_and_prev_week_remain_live(engine):
    live = [s.strategy_name for s in engine.live_strategies]
    assert "PREV_DAY_BREAKOUT" in live
    assert "PREV_WEEK_BREAKOUT_LIVE" in live


def test_no_live_strategy_is_paper_and_every_paper_strategy_is_paper(engine):
    assert all(not getattr(s, "paper", False) for s in engine.live_strategies)
    assert all(getattr(s, "paper", False) for s in engine.paper_strategies)


# ---- strategy-level: the same breakout bars -> real signal when live, none when paper ----
def _bar(hour, minute, o, h, l, c):
    return Bar(ts=datetime(2026, 6, 2, hour, minute), open=o, high=h, low=l, close=c, volume=1.0)


def _orb_breakout_day():
    """ORB 13:30-13:34 = H 4010 / L 4000; price sits inside until 13:49 (first bar of the
    entry window, state only), then 13:50 breaks above with a 0.5 overshoot."""
    bars = [_bar(13, m, 4005, 4010, 4000, 4005) for m in range(30, 35)]
    bars += [_bar(13, m, 4005, 4008, 4002, 4005) for m in range(35, 50)]
    bars.append(_bar(13, 50, 4006, 4011, 4005, 4010.5))
    return bars


def _run(strategy, bars):
    strategy._broker_offset = 0  # bar.ts is treated as already-UTC here
    return [strategy.on_bar(b) for b in bars]


@pytest.fixture(autouse=False)
def no_news(monkeypatch):
    import app.strategy.news_filter as news_mod
    monkeypatch.setattr(news_mod, "is_news_blackout", lambda *a, **k: (False, ""))


def test_live_orb_would_fire_a_real_signal_on_the_breakout(monkeypatch, tmp_path, no_news):
    monkeypatch.setattr(orb_mod, "PAPER_LOG_FILE", tmp_path / "orb.jsonl")
    out = _run(ORB5MinStrategy(rr=1.2, risk_pct=0.02, paper=False), _orb_breakout_day())
    sig = out[-1]
    assert sig is not None and sig.side == Side.BUY and sig.strategy == "ORB_5MIN_LIVE"


def test_paper_orb_never_returns_a_signal_but_still_logs_the_paper_trade(monkeypatch, tmp_path, no_news):
    log = tmp_path / "orb.jsonl"
    monkeypatch.setattr(orb_mod, "PAPER_LOG_FILE", log)
    out = _run(ORB5MinStrategy(rr=1.2, risk_pct=0.02, paper=True), _orb_breakout_day())
    assert all(sig is None for sig in out)          # nothing for the Engine to execute
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert any(e["event"] == "OPEN" and e["side"] == "BUY" for e in events)   # monitoring still works
