"""Central configuration. Reads .env + environment variables.

All tunables live here. Import `settings` anywhere in the codebase.
"""
from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import List, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import os

def _parse_time(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # runtime
    MODE: Literal["paper", "live", "backtest"] = "paper"

    # symbols
    SYMBOLS: str = "XAUUSD,EURUSD,GBPUSD,USDJPY,GBPJPY,AUDUSD"
    PRIMARY_SYMBOL: str = "XAUUSD"

    # timeframes
    BIAS_TIMEFRAME: str = "M15"
    EXECUTION_TIMEFRAME: str = "M1"

    # risk
    RISK_PER_TRADE: float = 0.02
    MAX_DAILY_LOSS: float = 0.06
    MAX_WEEKLY_LOSS: float = 0.10
    MAX_TRADES_PER_DAY: int = 1
    MAX_CONCURRENT_TRADES: int = 1
    MIN_RR: float = 1.0
    CONSEC_LOSS_COOLDOWN_HOURS: int = 2
    MAX_SPREAD_POINTS_XAUUSD: int = 40
    MAX_SPREAD_POINTS_FOREX: int = 20
    RR_TARGET: float = 2.0
    STARTING_EQUITY: float = 1000.0

    # sessions
    LONDON_OPEN: str = "07:00"
    LONDON_CLOSE: str = "10:00"
    NY_OPEN: str = "12:30"
    NY_CLOSE: str = "16:00"
    BLOCK_ASIAN_SESSION: bool = True
    # PREV_DAY_BREAKOUT only: no NEW entries at/after this UTC hour (same
    # clock strategies already use internally via broker_time.to_utc).
    # Does NOT apply to ORB_5MIN, PREV_WEEK_BREAKOUT, or FVG_RETEST.
    PREV_DAY_NO_ENTRY_AFTER_UTC_HOUR: int = 11
    # Used by broker_time.detect_broker_offset_hours() only when this process
    # has never confirmed a real offset from a live tick/bar (e.g. very first
    # tick, or broker data unavailable). IC Markets is UTC+3 in summer / +2 in
    # winter (server DST) — this is a last-resort guess, not a hardcoded truth.
    BROKER_OFFSET_FALLBACK_HOURS: float = 3.0
    # PREV_DAY_BREAKOUT only: only take a breakout if its direction agrees
    # with the D1 EMA50 regime (prev day's D1 close vs D1 EMA50). Toggle off
    # to trade every fresh cross regardless of daily trend, with no code change.
    PREV_DAY_TREND_FILTER_ENABLED: bool = True

    # MT5
    MT5_LOGIN: int | None = None
    MT5_PASSWORD: str | None = None
    MT5_SERVER: str | None = None
    MT5_PATH: str | None = None
    MT5_MAGIC: int = 20251116

    # DB
    DATABASE_URL: str = "sqlite:///./smc.db"

    # ML
    ML_ENABLED: bool = True
    ML_MIN_TRADES: int = 100
    ML_PROB_THRESHOLD: float = 0.55
    ML_MODEL_PATH: str = "./models/latest.pkl"

    # Telegram
    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_CHAT_ID: str | None = None

    # API
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8080
    API_TOKEN: str = "change-me"

    # logging
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "./logs"

    FTMO_MODE: bool = False
    FTMO_DAILY_LOSS_PCT: float = 0.05
    FTMO_MAX_LOSS_PCT: float = 0.10
    FTMO_PROFIT_TARGET_PCT: float = 0.05
    FTMO_MIN_TRADING_DAYS: int = 2
    FTMO_DAILY_BUFFER_PCT: float = 0.04
    FTMO_MAX_BUFFER_PCT: float = 0.08

    # ---- helpers ----
    @property
    def symbols_list(self) -> List[str]:
        return [s.strip().upper() for s in self.SYMBOLS.split(",") if s.strip()]

    @property
    def london_open_t(self) -> time: return _parse_time(self.LONDON_OPEN)
    @property
    def london_close_t(self) -> time: return _parse_time(self.LONDON_CLOSE)
    @property
    def ny_open_t(self) -> time: return _parse_time(self.NY_OPEN)
    @property
    def ny_close_t(self) -> time: return _parse_time(self.NY_CLOSE)

    @field_validator("RISK_PER_TRADE", "MAX_DAILY_LOSS", "MAX_WEEKLY_LOSS")
    @classmethod
    def _nonneg_small(cls, v: float) -> float:
        if v < 0 or v > 0.5:
            raise ValueError("risk fractions must be in [0, 0.5]")
        return v

    def ensure_dirs(self) -> None:
        Path(self.LOG_DIR).mkdir(parents=True, exist_ok=True)
        Path(self.ML_MODEL_PATH).parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
