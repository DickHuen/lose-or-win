"""Telegram Bot API client: alerts, documents, and reading /pause /kill /status commands.

Uses only sendMessage, sendDocument and getUpdates. The bot token is part of
the URL, so the httpx logger is silenced and all log output is redacted.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("perpbot.telegram")


class Telegram:
    def __init__(self, cfg: Any, token: str | None, chat_id: str | None, client: httpx.Client | None = None) -> None:
        self._t = cfg.telegram
        self.token = token or ""
        self.chat_id = str(chat_id or "")
        self._client = client or httpx.Client(timeout=float(self._t.http_timeout_seconds))

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _url(self, method: str) -> str:
        return f"{self._t.api_base.rstrip('/')}/bot{self.token}/{method}"

    def send(self, text: str) -> bool:
        if not self.enabled:
            log.info("telegram disabled; message not sent: %s", text[:200])
            return False
        limit = int(self._t.max_message_chars)
        chunks = [text[i:i + limit] for i in range(0, len(text), limit)] or [""]
        ok = True
        for chunk in chunks:
            try:
                r = self._client.post(self._url("sendMessage"), data={"chat_id": self.chat_id, "text": chunk,
                                                                      "disable_web_page_preview": "true"})
                if r.status_code != 200 or not r.json().get("ok"):
                    ok = False
                    log.error("telegram sendMessage failed: HTTP %s", r.status_code)
            except (httpx.HTTPError, ValueError) as e:
                ok = False
                log.error("telegram sendMessage error: %s", type(e).__name__)
        return ok

    def send_document(self, path: Path, caption: str = "") -> bool:
        if not self.enabled:
            return False
        try:
            with path.open("rb") as fh:
                r = self._client.post(self._url("sendDocument"), data={"chat_id": self.chat_id, "caption": caption[:1000]},
                                      files={"document": (path.name, fh)})
            if r.status_code != 200 or not r.json().get("ok"):
                log.error("telegram sendDocument failed: HTTP %s", r.status_code)
                return False
            return True
        except (httpx.HTTPError, OSError, ValueError) as e:
            log.error("telegram sendDocument error: %s", type(e).__name__)
            return False

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        params: dict[str, Any] = {"timeout": 0, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        try:
            r = self._client.get(self._url("getUpdates"), params=params)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.error("telegram getUpdates error: %s", type(e).__name__)
            return []
        if not data.get("ok"):
            log.error("telegram getUpdates not ok: %s", str(data.get("description", ""))[:200])
            return []
        return list(data.get("result") or [])

    def close(self) -> None:
        self._client.close()


def parse_command(update: dict[str, Any], chat_id: str) -> tuple[int, str | None, str]:
    """(update_id, command or None, raw text). Only messages from the configured chat count."""
    uid = int(update.get("update_id", 0))
    msg = update.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    text = str(msg.get("text") or "").strip()
    if chat != str(chat_id) or not text.startswith("/"):
        return uid, None, text
    cmd = text.split()[0].split("@")[0].lower()
    if cmd in ("/pause", "/kill", "/status"):
        return uid, cmd[1:], text
    return uid, "unknown", text
