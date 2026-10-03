"""Delivery channels. Each has a name and `async send(text)`; raise on failure.

To add WhatsApp, email or Teams: write a class like TelegramSender and add it
in `enabled_senders`.
"""
import html
import logging
import os
import re

import httpx

log = logging.getLogger("notifier.senders")


_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"__(.+?)__")


def plain(text: str) -> str:
    """Message text uses **bold** and __italic__; channels without formatting drop the markers."""
    return _ITALIC.sub(r"\1", _BOLD.sub(r"\1", text))


def telegram_html(text: str) -> str:
    text = _BOLD.sub(r"<b>\1</b>", html.escape(text, quote=False))
    return _ITALIC.sub(r"<i>\1</i>", text)


class LogSender:
    name = "log"

    async def send(self, text: str) -> None:
        log.info("ALERT %s", plain(text).replace("\n", " | "))


class TelegramSender:
    name = "telegram"

    def __init__(self, token: str, chat_id: str):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id

    async def send(self, text: str) -> None:
        # Never log the URL or str(exception): both contain the bot token.
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(self._url, json={"chat_id": self._chat_id, "text": telegram_html(text), "parse_mode": "HTML"})
        except httpx.HTTPError as e:
            raise RuntimeError(f"telegram request failed: {type(e).__name__}") from None
        if r.status_code != 200:
            desc = ""
            try:
                desc = r.json().get("description", "")
            except ValueError:
                pass
            raise RuntimeError(f"telegram HTTP {r.status_code}: {desc}")


def enabled_senders() -> list:
    senders = [LogSender()]
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat:
        senders.append(TelegramSender(token, chat))
    elif token:
        log.warning("TELEGRAM_BOT_TOKEN is set but TELEGRAM_CHAT_ID is empty: Telegram disabled")
    return senders
