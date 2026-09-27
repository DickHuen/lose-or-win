"""Secrets from .env (never logged, never written to the database)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ENV_KEYS = ("PM_PROXY_PRIVATE_KEY", "PM_PROXY_SECRET", "PM_WALLET_ADDRESS", "PM_PROXY_EXPIRES_AT",
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "HEALTHCHECK_PING_URL", "HEALTHCHECK_DECIDE_URL",
            "HEALTHCHECK_MANAGE_URL")
SECRET_KEYS = ("PM_PROXY_PRIVATE_KEY", "PM_PROXY_SECRET", "TELEGRAM_BOT_TOKEN")


class SecretsError(Exception):
    pass


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


@dataclass
class Secrets:
    proxy_private_key: str = field(default="", repr=False)
    proxy_secret: str = field(default="", repr=False)
    wallet_address: str = ""
    proxy_expires_at: datetime | None = None
    telegram_token: str = field(default="", repr=False)
    telegram_chat_id: str = ""
    proxy_address: str = ""
    healthcheck_url: str = field(default="", repr=False)          # fallback for both checks
    healthcheck_decide_url: str = field(default="", repr=False)
    healthcheck_manage_url: str = field(default="", repr=False)

    def heartbeat_url(self, command: str) -> str:
        own = {"decide": self.healthcheck_decide_url, "manage": self.healthcheck_manage_url}.get(command, "")
        return own or self.healthcheck_url

    def secret_values(self) -> list[str]:
        return [v for v in (self.proxy_private_key, self.proxy_secret, self.telegram_token, self.healthcheck_url,
                            self.healthcheck_decide_url, self.healthcheck_manage_url) if v and len(v) >= 6]

    def require_trading(self) -> None:
        missing = [n for n, v in (("PM_PROXY_PRIVATE_KEY", self.proxy_private_key), ("PM_PROXY_SECRET", self.proxy_secret),
                                  ("PM_WALLET_ADDRESS", self.wallet_address)) if not v]
        if missing:
            raise SecretsError(f"missing in .env: {', '.join(missing)}")
        if not self.proxy_address:
            raise SecretsError("PM_PROXY_PRIVATE_KEY is not a valid EVM private key")


def load_secrets(env_file: Path) -> Secrets:
    env = parse_env_file(env_file)
    for k in ENV_KEYS:  # real environment variables override the file
        if os.environ.get(k):
            env[k] = os.environ[k]
    s = Secrets(proxy_private_key=env.get("PM_PROXY_PRIVATE_KEY", ""), proxy_secret=env.get("PM_PROXY_SECRET", ""),
                wallet_address=env.get("PM_WALLET_ADDRESS", ""), telegram_token=env.get("TELEGRAM_BOT_TOKEN", ""),
                telegram_chat_id=env.get("TELEGRAM_CHAT_ID", ""), healthcheck_url=env.get("HEALTHCHECK_PING_URL", "").strip(),
                healthcheck_decide_url=env.get("HEALTHCHECK_DECIDE_URL", "").strip(),
                healthcheck_manage_url=env.get("HEALTHCHECK_MANAGE_URL", "").strip())
    for name, url in (("HEALTHCHECK_PING_URL", s.healthcheck_url), ("HEALTHCHECK_DECIDE_URL", s.healthcheck_decide_url),
                      ("HEALTHCHECK_MANAGE_URL", s.healthcheck_manage_url)):
        if url and not url.startswith("https://"):
            raise SecretsError(f"{name} must start with https:// (e.g. https://hc-ping.com/<your-uuid>)")
    exp = env.get("PM_PROXY_EXPIRES_AT", "").strip()
    if exp:
        try:
            s.proxy_expires_at = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        except ValueError as e:
            raise SecretsError("PM_PROXY_EXPIRES_AT must be ISO-8601, e.g. 2026-10-26T00:00:00Z") from e
    if s.proxy_private_key:
        try:
            from eth_account import Account

            s.proxy_address = Account.from_key(s.proxy_private_key).address
        except Exception:  # noqa: BLE001 - never echo the key
            s.proxy_address = ""
    return s
