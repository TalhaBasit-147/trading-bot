# Operations runbook

A one-page reference for running this bot day-to-day.

## First-time checklist

- [ ] Windows VPS provisioned, time synced to NTP.
- [ ] MT5 installed, logged into demo account, AutoTrading enabled.
- [ ] Python 3.11 installed, repo cloned to `C:\smc-bot`.
- [ ] `pip install -r requirements.txt && pip install MetaTrader5==5.0.45`
- [ ] `.env` filled in with `MODE=live`, MT5 credentials, Telegram tokens, strong `API_TOKEN`.
- [ ] `python scripts/init_db.py`
- [ ] Run **30-day MT5 backtest** per symbol, inspect reports:
      `python scripts/run_backtest.py --symbol XAUUSD --csv data/XAUUSD_M1.csv`
- [ ] Confirm demo `python scripts/run_live.py` → Telegram "🟢 SMC bot starting" arrives.
- [ ] Let it run **2 weeks on demo** before switching to live.
- [ ] Install NSSM, register `SMCBot` service, set auto-restart.
- [ ] Verify `curl http://<vps-ip>:8080/health` returns ok from outside.

## Normal day

- Bot runs 24/7. It stays idle outside London/NY.
- Watch Telegram for `ENTRY`, `WIN/LOSS`, and daily summary notifications.
- Don't interfere with positions manually — let SL/TP run. Manual closes confuse
  reconciliation and corrupt training labels.

## Weekly

- Glance at `/trades` endpoint or run: `sqlite3 smc.db 'SELECT outcome, COUNT(*) FROM trades WHERE close_ts > DATE("now","-7 days") GROUP BY outcome;'`
- Check Telegram for anything red. Check `logs/errors.log`.
- Verify disk space on the VPS (`dir C:\smc-bot\logs`).

## Monthly

- Review the nightly training runs: `sqlite3 smc.db 'SELECT trained_at, n_trades, auc, brier FROM model_versions ORDER BY id DESC LIMIT 10;'`
- If AUC has drifted below 0.55 for three runs in a row → investigate.
  Likely data regime change or strategy parameter drift.
- Back up `smc.db` and `models/latest.pkl` off-box.

## Kill switch

If you see weird behavior and want to stop NOW:

```
echo > C:\smc-bot\KILL
```

The risk manager checks for this file on every tick. Delete the file to resume.

For a hard stop, stop the NSSM service: `nssm stop SMCBot`.

## Control API

All require header `X-API-Token: <your token>`:

```
GET  /health                      → liveness (no auth)
GET  /status                      → equity, daily pnl, pause state
POST /pause?reason=manual         → pause trading
POST /resume                      → resume trading
GET  /trades?limit=50             → recent closed trades
POST /reload_model                → pick up newly trained model without restart
```

## Common issues

**"Broker failed to connect"** on startup →
MT5 terminal isn't running or isn't logged in. Open the terminal, log in,
enable AutoTrading, then restart the service.

**Signals fire but orders get rejected** →
Check `logs/bot.log` for `order_send failed: retcode=...`. Most common
retcodes: `10019 TRADE_RETCODE_NO_MONEY` (margin), `10016 TRADE_RETCODE_INVALID_STOPS`
(SL/TP too close to market — increase the buffer in `smc_strategy.py`).

**"daily loss cap hit" in logs** →
Intended behavior. Bot will resume next UTC day.

**Telegram silent** →
Wrong `TELEGRAM_CHAT_ID` (try both positive and negative IDs for group chats)
or bot not added to the chat. Test with:
`curl "https://api.telegram.org/bot<TOKEN>/getUpdates"`

**Win-rate degrading over weeks** →
Check if broker changed spreads / symbol specs. Run a fresh backtest on
MT5-exported M1 history for the same period. If backtest still looks OK but
live doesn't, it's a broker/execution issue, not strategy.
