"""Telegram notifications.

Implemented directly against the Bot HTTP API with ``httpx`` (already a
transitive dependency of the MCP SDK) rather than pulling in the full
``python-telegram-bot`` framework: the bot only ever sends one-way messages, and
an unused polling/Updater stack is dead weight in a Railway image.

Design notes:
* Sending must never be able to break trading, so every failure is swallowed and
  logged - a dead Telegram bot is not a reason to stop managing live risk.
* Alerts are de-duplicated over a short window, because an auth outage would
  otherwise emit one message per reconnect attempt and get the bot rate-limited
  exactly when the operator needs to see the alert.
"""

from __future__ import annotations

import asyncio
import html
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from utils.logging import get_logger

log = get_logger("telegram")

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_LEN = 4000


@dataclass
class _Dedup:
    key: str
    sent_at: float


class TelegramNotifier:
    """Fire-and-forget Telegram sender with dedup and graceful degradation."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        timeout: float = 10.0,
        dedup_seconds: float = 120.0,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout
        self._dedup_seconds = dedup_seconds
        self._recent: dict[str, float] = {}
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat_id)

    async def start(self) -> None:
        if self.enabled and self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, text: str, *, dedup_key: Optional[str] = None) -> bool:
        """Send a message.  Returns True if Telegram accepted it."""
        if not self.enabled:
            log.debug("Telegram disabled; dropping message: %s", text.splitlines()[0][:120])
            return False

        if dedup_key and self._is_duplicate(dedup_key):
            log.debug("Suppressed duplicate Telegram alert %s", dedup_key)
            return False

        await self.start()
        payload = {
            "chat_id": self._chat_id,
            "text": text[:MAX_MESSAGE_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        url = f"{TELEGRAM_API}/bot{self._token}/sendMessage"
        try:
            async with self._lock:  # keeps ordering and avoids burst rate limits
                assert self._client is not None
                response = await self._client.post(url, json=payload)
            if response.status_code != 200:
                log.warning("Telegram rejected message (%s): %s",
                            response.status_code, response.text[:200])
                return False
            return True
        except Exception as exc:  # noqa: BLE001 - never let notification kill the bot
            log.warning("Telegram send failed: %s", exc)
            return False

    def _is_duplicate(self, key: str) -> bool:
        now = time.monotonic()
        # Opportunistic pruning keeps the dict from growing unbounded.
        self._recent = {k: t for k, t in self._recent.items() if now - t < self._dedup_seconds}
        if key in self._recent:
            return True
        self._recent[key] = now
        return False


def esc(value: object) -> str:
    """HTML-escape a value for safe interpolation into a Telegram message."""
    return html.escape(str(value), quote=False)


class NullNotifier(TelegramNotifier):
    """Used when Telegram is not configured; keeps call sites branch-free."""

    def __init__(self) -> None:
        super().__init__("", "")

    async def send(self, text: str, *, dedup_key: Optional[str] = None) -> bool:
        if dedup_key and self._is_duplicate(dedup_key):
            return False
        log.info("[notify] %s", text.replace("\n", " | ")[:400])
        return False
