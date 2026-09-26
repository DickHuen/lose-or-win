#!/usr/bin/env python3
"""Launcher: `python run.py <command>` runs the bot inside ./venv (created by install.py).

Commands: decide | manage | report daily|weekly|monthly | backup | status | pause | kill |
          resume | alerts | selftest | smoketest | snapshot | dashboard | schedule | version
On Windows the .bat files in the windows folder call this for you.
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
        print("venv not found - run install.py first (Windows: windows\\1_Install.bat)", file=sys.stderr)
        return 2
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.call([str(py), "-m", "perpbot", *sys.argv[1:]], cwd=str(ROOT), env=env)


if __name__ == "__main__":
    sys.exit(main())
