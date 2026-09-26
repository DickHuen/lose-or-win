"""BTC-PERP trading bot for Polymarket Perps."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def code_version() -> str:
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


__version__ = code_version()
