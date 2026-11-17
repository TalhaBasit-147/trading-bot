"""Fire-and-forget Telegram notifier. No-op if tokens are not configured.

Uses httpx with a short timeout. Never raises to callers.
"""
from __future__ import annotations

from typing import Optional

import httpx
from loguru import logger

from app.config import settings


class Telegram:
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None,
                 label: Optional[str] = None):
        self.token = token or settings.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or settings.TELEGRAM_CHAT_ID
        self.label = (settings.BOT_LABEL if label is None else label).strip()
        self.enabled = bool(self.token and self.chat_id)
        if not self.enabled:
            logger.info("Telegram disabled (no token/chat id).")

    def format(self, text: str) -> str:
        """Message as sent: prefixed with the bot label when one is configured.
        Plain text prefix (no brackets): '[x]' could be read as Markdown link syntax."""
        return f"{self.label} | {text}" if self.label else text

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        text = self.format(text)
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            with httpx.Client(timeout=5.0) as client:
                client.post(url, data={"chat_id": self.chat_id, "text": text[:4000], "parse_mode": "Markdown"})
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")
