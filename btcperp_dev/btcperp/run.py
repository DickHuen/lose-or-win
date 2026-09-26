#!/usr/bin/env python3
"""Launcher: `python3 run.py <command>` runs the bot inside ./venv (created by install.py).

Commands: decide | manage | report daily|weekly|monthly | backup | status | pause | kill |
          resume | selftest | smoketest | version
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def venv_python() -> Path:
    if os.name == "nt":
        return ROOT / "venv" / "Scripts" / "python.exe"
    return ROOT / "venv" / "bin" / "python"


def main() -> int:
    py = venv_python()
    if not py.exists():
        print("venv not found - run `python3 install.py` first", file=sys.stderr)
        return 2
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return subprocess.call([str(py), "-m", "perpbot", *sys.argv[1:]], cwd=str(ROOT), env=env)


if __name__ == "__main__":
    sys.exit(main())
