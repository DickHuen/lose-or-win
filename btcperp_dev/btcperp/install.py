#!/usr/bin/env python3
"""One-command installer / upgrader:  python install.py   (Windows: double-click windows\\1_Install.bat)

- checks the Python version (3.11+)
- creates ./venv (or reuses it) and installs the pinned requirements
- creates data/ and logs/ if missing
- copies .env.example to .env only if .env does not exist
- removes code files left over from an older version (per MANIFEST.txt);
  never touches data/, logs/, venv/ or .env
- on Windows: stops the background dashboard during the upgrade and restarts it afterwards
- runs `selftest` and prints PASS/FAIL and the next step
Safe to re-run.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MIN = (3, 11)
PROTECTED = {"data", "logs", "venv", ".env"}


def say(msg: str) -> None:
    print(f"[install] {msg}", flush=True)


def venv_python() -> Path:
    return ROOT / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def remove_stale_files() -> None:
    manifest = ROOT / "MANIFEST.txt"
    if not manifest.exists():
        return
    keep = {line.strip() for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()}
    for sub in ("perpbot", "tests", "windows"):
        base = ROOT / sub
        if not base.exists():
            continue
        for p in base.rglob("*"):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and "__pycache__" not in rel and rel not in keep:
                say(f"removing stale file from an older version: {rel}")
                p.unlink()


def dashboard_task(action: str) -> None:
    """Windows: /End or /Run the background dashboard task if it exists (files in use cannot be upgraded)."""
    if os.name != "nt":
        return
    try:
        subprocess.run(["schtasks", f"/{action}", "/TN", "\\btcperp\\dashboard"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def main() -> int:
    if sys.version_info < MIN:
        say(f"FAIL: Python {MIN[0]}.{MIN[1]}+ required, found {sys.version.split()[0]}")
        return 1
    say(f"Python {sys.version.split()[0]} OK")
    for d in ("data", "logs"):
        (ROOT / d).mkdir(exist_ok=True)
    env, example = ROOT / ".env", ROOT / ".env.example"
    if not env.exists():
        shutil.copy2(example, env)
        say(".env created from .env.example (fill it in; never commit or print it)")
    else:
        say(".env exists - left untouched")
    if os.name != "nt":
        env.chmod(stat.S_IRUSR | stat.S_IWUSR)
    dashboard_task("End")
    remove_stale_files()
    py = venv_python()
    if not py.exists():
        say("creating venv ...")
        subprocess.check_call([sys.executable, "-m", "venv", str(ROOT / "venv")])
    else:
        say("venv exists - reusing")
    say("installing pinned requirements ...")
    subprocess.check_call([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    subprocess.check_call([str(py), "-m", "pip", "install", "--quiet", "-r", str(ROOT / "requirements.txt")])
    say("running selftest ...")
    envv = dict(os.environ, PYTHONPATH=str(ROOT))
    rc = subprocess.call([str(py), "-m", "perpbot", "selftest"], cwd=str(ROOT), env=envv)
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    dashboard_task("Run")
    if rc == 0:
        print(f"\nINSTALL PASS (btcperp v{version})")
        if os.name == "nt":
            print("Next steps (see START_HERE.md):")
            print("  1. windows\\Edit_Secrets.bat  - fill in the proxy key, proxy secret and wallet address")
            print("  2. windows\\2_Smoketest.bat   - live check at minimum size")
            print("  3. windows\\3_Schedule_Install.bat - only when you decide to go live")
            print("  Dashboard any time: windows\\Dashboard.bat")
        else:
            print("Next step: fill in .env (proxy key, proxy secret, wallet address),")
            print("then run:  python3 run.py smoketest   and check the result. Do NOT schedule routines before GO.")
        return 0
    print(f"\nINSTALL FAIL (btcperp v{version}): selftest failed - keep the output above and the logs folder "
          f"(never .env) for troubleshooting.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
