# SMC Bot — Liquidity • Order Blocks • Fair Value Gaps

Production-grade intraday trading bot for **XAUUSD** and major forex pairs.
Uses Smart Money Concepts (SMC): liquidity sweeps, order blocks, fair value gaps,
with market-structure confirmation (BOS/CHoCH) and a LightGBM probability filter
that learns from closed trades.

Execution through **MetaTrader 5** (demo first, then live). Includes a paper broker
for development on any OS and a full event-driven backtester.

---

## Quick start (local, paper mode, no MT5 required)

```bash
# 1. Clone & install
git clone <your-repo> smc-bot && cd smc-bot
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# edit .env — MODE=paper is the default, no broker needed

# 3. Initialize DB (SQLite by default)
python scripts/init_db.py

# 4. Backtest on sample CSV data
python scripts/run_backtest.py --symbol XAUUSD --csv data/XAUUSD_M1.csv

# 5. Paper trade with synthetic/replayed data
python scripts/run_paper.py
```

## Live (MT5) on Windows VPS

```powershell
# Install Python 3.11 x64 + MetaTrader 5 terminal
pip install -r requirements.txt
# Login MT5 terminal to your demo/live account, enable AutoTrading
copy .env.example .env
# set MODE=live, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER
python scripts/run_live.py
```

## Docker (engine, Linux — paper/backtest only; MT5 is Windows-only)

```bash
docker compose -f docker/docker-compose.yml up -d
```

## Architecture

```
MT5 Terminal ── Python MetaTrader5 pkg ──┐
                                         ▼
 1m ticks/bars ─▶ Resampler(15m) ─▶ Features(SMC) ─▶ Strategy ─▶ Risk ─▶ Broker
                                         │                │
                                         ▼                ▼
                                    PostgreSQL ◀── Trade outcomes
                                         │
                                         ▼
                                    Nightly Learner (LightGBM)
                                         │
                                         ▼
                                    Scorer (live P(win) filter)
```

## Strategy in one paragraph

On the 15-minute chart we track swings and bias. When price sweeps an obvious
liquidity pool (equal highs/lows) and the next leg shows displacement (body ≥
1.5× avg body), we mark the last opposite candle as an **order block** and
check for a **fair value gap** inside the displacement. When price returns
into the OB/FVG overlap and prints a 1-minute CHoCH against the retrace, we
enter. SL goes beyond the sweep wick, TP targets opposite liquidity.
Minimum RR is 2.0. We only trade London & NY sessions.

Full strategy docs are in [`docs/STRATEGY.md`](docs/STRATEGY.md) (auto-generated
from the code — see `app/strategy/smc_strategy.py`).

## Day-to-day operation

- Bot runs 24/7 on the VPS. It is session-aware and stays idle outside London/NY.
- Telegram notifies on: startup, new signal, entry, exit, daily summary, errors.
- Control plane: `GET /health`, `POST /pause`, `POST /resume`, `GET /trades`.
- Nightly: learner retrains if ≥ 20 new closed trades exist. Drift guard auto
  flips to paper if live win-rate degrades >15% vs backtest.
- Log rotation via loguru (`logs/bot.log`, 10 MB × 10 files).

## Safety

- Daily loss cap, weekly loss cap, consecutive-loss cool-down, kill switch file
  (`./KILL`), spread guard, news-time pause (configurable), auto-pause on broker
  errors.

## License

MIT — use at your own risk. Trading involves substantial risk of loss.
