# Windows VPS setup (for LIVE trading via MT5)

MetaTrader 5 is Windows-only, so live trading must run on a Windows host.
Docker Desktop on Windows works, but the simpler and cheaper path is to run
MT5 + Python directly on a cheap Windows VPS.

## Recommended providers

| Provider       | Example plan               | Approx /mo | Notes                                          |
|----------------|----------------------------|------------|------------------------------------------------|
| Contabo        | Windows VPS S (4GB, 3 vCPU)| ~$10       | Great price; higher latency to some brokers.  |
| ForexVPS.net   | Basic                      | ~$25–$35   | Low latency to major broker DCs; forex-tuned. |
| AccuWebHosting | Windows VPS (2GB)          | ~$7        | Cheapest end; watch CPU under load.           |
| BeeksFX        | Crossconnect plans         | ~$50+      | Institutional-grade latency; pricier.         |

If you care about milliseconds (you don't, for 1m/15m SMC), pay for ForexVPS or
Beeks. Otherwise Contabo Windows VPS S is an excellent default for this bot.

## One-time setup

1. Install Python 3.11 (x64) from python.org. Check "Add to PATH".
2. Install MetaTrader 5 terminal from your broker.
3. Log MT5 into your demo account first, then live. Enable **AutoTrading**.
4. Clone the repo and install deps:
   ```
   git clone <your-repo> C:\smc-bot
   cd C:\smc-bot
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   pip install MetaTrader5==5.0.45
   ```
5. Copy `.env.example` to `.env`, set `MODE=live`, `MT5_LOGIN`, `MT5_PASSWORD`,
   `MT5_SERVER`. Leave `MT5_PATH` empty unless you have multiple terminals.
6. Initialize the DB: `python scripts/init_db.py`.
7. Run a dry check: `python scripts/run_live.py` — confirm it connects and
   logs "MT5 connected" with your account number.

## Running as a Windows service (auto-restart)

The cleanest way is **NSSM** (free, ~2 MB):

1. Download nssm: https://nssm.cc/download
2. `nssm install SMCBot`
   - Path: `C:\smc-bot\.venv\Scripts\python.exe`
   - Startup directory: `C:\smc-bot`
   - Arguments: `scripts\run_live.py`
3. Tab "I/O": redirect stdout/stderr to `C:\smc-bot\logs\service.out.log` / `.err.log`
4. Tab "Exit actions": Restart application, 5000 ms delay.
5. `nssm start SMCBot`

Verify from another machine: `curl http://<vps-ip>:8080/health` (open port 8080
in Windows firewall and cloud security group; restrict by IP if possible).

## Keeping MT5 alive

MT5 reconnects automatically on network blips. If the terminal ever crashes,
NSSM won't know — it only watches the Python process. Two options:

- **Simplest**: let the Python engine crash when `mt5.initialize()` fails
  repeatedly; NSSM restarts it; on restart, MT5 is re-initialized.
- **More robust**: install MT5 as a Windows Scheduled Task set to run at
  logon and restart on failure; Python engine starts after it.

## Time sync

Windows time drift can break signal timing. Force NTP sync:

```
w32tm /config /manualpeerlist:"time.windows.com,time.google.com" /syncfromflags:manual /reliable:yes /update
w32tm /resync
```

## Backups

```
robocopy C:\smc-bot\logs  \\backup\smc\logs  /MIR
robocopy C:\smc-bot\models \\backup\smc\models /MIR
# plus a daily pg_dump if using Postgres; see below
```

For SQLite (default), just schedule a daily copy of `smc.db`.
For Postgres:
```
"C:\Program Files\PostgreSQL\16\bin\pg_dump.exe" -U smc -h localhost smc > C:\backups\smc_$(Get-Date -f yyyyMMdd).sql
```

## Cost summary for a typical solo setup

- Windows VPS (Contabo S):       ~$10/mo
- SQLite (local, free):          $0
- Monitoring (self-hosted):      $0
- Backup storage (Backblaze B2): ~$1/mo
- **Total: ~$11/mo**
