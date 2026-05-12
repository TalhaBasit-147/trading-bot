"""Structured logging via loguru with size-based rotation."""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from app.config import settings


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.LOG_LEVEL,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>",
        enqueue=True,
    )
    log_dir = Path(settings.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "bot.log",
        level=settings.LOG_LEVEL,
        rotation="10 MB",
        retention=10,
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
        enqueue=True,
    )
    logger.add(
        log_dir / "errors.log",
        level="ERROR",
        rotation="10 MB",
        retention=10,
        compression="zip",
        enqueue=True,
    )
