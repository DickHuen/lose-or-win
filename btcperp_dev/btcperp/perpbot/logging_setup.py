"""Logging: one append-only file per UTC day in logs/, plus stdout. Secrets are redacted."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Iterable

from perpbot.timeutil import Clock

_HEX_KEY = re.compile(r"0x[0-9a-fA-F]{64}")
_TG_TOKEN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")


class RedactFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        text = _HEX_KEY.sub("0x***", text)
        return _TG_TOKEN.sub("***:***", text)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        red = self.redact(msg)
        if record.exc_info:
            import traceback

            red += "\n" + self.redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
            record.exc_text = None
        record.msg = red
        record.args = None
        return True


_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(logs_dir: Path, clock: Clock, level: str = "INFO", secrets: Iterable[str] = (),
                  stdout: bool = True) -> tuple[Path, RedactFilter]:
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / f"btcperp_{clock.now().strftime('%Y-%m-%d')}.log"
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    redact = RedactFilter(secrets)
    fmt = logging.Formatter(_FORMAT)
    fh = logging.FileHandler(path, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(redact)
    root.addHandler(fh)
    if stdout and sys.stdout is not None:           # pythonw.exe (Task Scheduler) has no console
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        sh.addFilter(redact)
        root.addHandler(sh)
    # httpx logs full request URLs at INFO (the Telegram token is in the URL).
    for noisy in ("httpx", "httpcore", "websockets", "hpack", "polymarket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path, redact


def tail(path: Path, lines: int = 40) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(data[-lines:])
