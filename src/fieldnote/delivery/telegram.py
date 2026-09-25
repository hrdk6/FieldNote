"""Telegram Bot API delivery (sendMessage + sendDocument) over requests."""

from __future__ import annotations

import time
from pathlib import Path

import requests

from fieldnote.config import Settings
from fieldnote.logging_setup import get_logger, redact

log = get_logger(__name__)
API = "https://api.telegram.org"


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None, timeout: float = 30.0) -> None:
        if not settings.has_telegram:
            raise TelegramError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not configured")
        self.token = settings.telegram_bot_token or ""
        self.chat_id = settings.telegram_chat_id or ""
        self.session = session or requests.Session()
        self.timeout = timeout

    def _post(self, method: str, **kwargs: object) -> dict:
        url = f"{API}/bot{self.token}/{method}"
        last = ""
        for attempt in range(3):
            try:
                resp = self.session.post(url, timeout=self.timeout, **kwargs)  # type: ignore[arg-type]
            except requests.RequestException as exc:
                last = type(exc).__name__
                time.sleep(1.5 * (2**attempt))
                continue
            if resp.status_code == 429:
                retry = resp.json().get("parameters", {}).get("retry_after", 2)
                time.sleep(min(float(retry), 30))
                continue
            data = resp.json() if resp.content else {}
            if not data.get("ok"):
                raise TelegramError(redact(f"Telegram {method} failed: {data.get('description', resp.status_code)}"))
            return data
        raise TelegramError(f"Telegram {method} failed after retries ({last})")

    def send_messages(self, chunks: list[str], parse_mode: str | None = "MarkdownV2") -> int:
        sent = 0
        for chunk in chunks:
            payload: dict[str, object] = {"chat_id": self.chat_id, "text": chunk, "disable_web_page_preview": True}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            self._post("sendMessage", json=payload)
            sent += 1
        return sent

    def send_document(self, path: Path, caption: str = "") -> None:
        with path.open("rb") as fh:
            self._post(
                "sendDocument",
                data={"chat_id": self.chat_id, "caption": caption[:1000]},
                files={
                    "document": (
                        path.name,
                        fh,
                        "application/pdf" if path.suffix == ".pdf" else "application/octet-stream",
                    )
                },
            )
