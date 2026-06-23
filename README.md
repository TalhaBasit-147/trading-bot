# XAUUSD Previous Day Breakout Bot

An automated gold trading bot for MetaTrader 5 built on the Previous Day
High/Low breakout strategy. Runs on a Windows VPS, trades XAUUSD, and sends
Telegram alerts on every trade.

## Live Results

- Starting balance: $150 (demo account, IC Markets)
- Current balance: $222
- Net profit: +$72 (~48% return)
- Strategy live since: May 2026
- Backtest (Feb-May 2026): 65% win rate, 72 trades

## What the bot does

- Detects breakouts above previous day high or below previous day low
- Enters trades automatically during the NY session
- Sets SL and TP automatically based on configurable RR ratio (default 1.2)
- Skips trading 30 minutes before/after high-impact news (NFP, CPI, FOMC)
- Sends Telegram message on every entry and exit
- Logs all trades to local database

## Requirements

- Windows PC or Windows VPS (required for MT5)
- MetaTrader 5 installed and logged into your broker account
- Python 3.11+
- IC Markets or any MT5 broker (XAUUSD must be available)
- Telegram bot (free, setup instructions below)
- News API key from forex-calendar.pro (free tier works)

## Setup

### 1. Install Python dependencies

pip install -r requirements.txt

### 2. Configure your .env file

Copy .env.example to .env:

copy .env.example .env

Open .env and fill in:
- MT5_LOGIN: your MT5 account number
- MT5_PASSWORD: your MT5 password
- MT5_SERVER: your broker server name (visible on MT5 login screen)
- MT5_PATH: full path to terminal64.exe (leave blank to auto-detect)
- TELEGRAM_BOT_TOKEN: from @BotFather on Telegram
- TELEGRAM_CHAT_ID: your Telegram user ID (get from @userinfobot)
- NEWS_API_KEY: from forex-calendar.pro (free registration)
- STARTING_EQUITY: your account starting balance in USD

### 3. Export historical data (for backtest only)

With MT5 open and logged in, run:

python scripts/export_chunks.py

### 4. Run a backtest (optional but recommended)

$env:PYTHONPATH = "."; python -m scripts.run_backtest --csv data/XAUUSD_M1_ALL.csv

### 5. Start the bot

$env:PYTHONPATH = "."; python scripts/run_live.py

The bot will connect to MT5, wait for the next breakout signal, and trade
automatically. Leave it running on your VPS 24/7.

## Risk settings (in .env)

Setting            | Default | Description
RISK_PER_TRADE     | 0.02    | 2% of balance per trade
MAX_DAILY_LOSS     | 0.06    | Stop trading if down 6% in a day
MAX_WEEKLY_LOSS    | 0.10    | Stop trading if down 10% in a week
MAX_TRADES_PER_DAY | 1       | Max 1 trade per day
RR_TARGET          | 1.2     | Risk to Reward ratio

## Telegram setup (5 minutes)

1. Open Telegram, search @BotFather
2. Send /newbot, follow the prompts, copy the token
3. Search @userinfobot, start it, copy your Chat ID
4. Paste both into your .env file

## News API setup (2 minutes)

1. Go to forex-calendar.pro
2. Register for a free account
3. Copy your API key into NEWS_API_KEY in .env

## Disclaimer

This bot was tested on a demo account. Past results do not guarantee future
performance. Always test on a demo account before using real funds. You are
responsible for your own trading decisions.

## Support

If you have setup issues contact via the platform you purchased from.