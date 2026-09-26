#!/usr/bin/env python3
"""Installer / upgrader.

  python install.py                      install, or re-install in place   (Windows: windows\\1_Install.bat)
  python install.py --from-zip FILE.zip  upgrade from a new btcperp_vX.Y.Z.zip (Windows: windows\\Upgrade.bat)

- checks the Python version (3.11+)
- creates ./venv (or reuses it) and installs the pinned requirements
- creates data/ and logs/ if missing; copies .env.example to .env only if .env does not exist
- never touches data/, logs/, venv/ or .env
- on Windows, if the bot's scheduled tasks exist (review v1.2.0 item 8): disables them and waits for the
  bot's file lock BEFORE changing any file, so no run can see half-old / half-new code; after the
  tests pass it re-registers the tasks (new version's settings) and restarts the dashboard. If the tests
  fail, the tasks stay DISABLED and it says so.
- removes code files left over from an older version (per MANIFEST.txt)
- runs the unit tests and prints PASS/FAIL and the next step
Safe to re-run.
"""

import argparse
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
MIN = (3, 11)
PROTECTED = {"data", "logs", "venv", ".env"}
TASK_FOLDER = "\\btcperp\\"
LOCK_WAIT_SECONDS = 20 * 60


def say(msg: str) -> None:
    print(f"[install] {msg}", flush=True)


def venv_python() -> Path:
    return ROOT / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _schtasks(*args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["schtasks", *args], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, (r.stdout or b"").decode(errors="replace") + (r.stderr or b"").decode(errors="replace")


def existing_tasks() -> list[str]:
    """Names of the bot's scheduled tasks (Windows only)."""
    if os.name != "nt":
        return []
    rc, out = _schtasks("/Query", "/FO", "CSV", "/NH")
    names = []
    for line in out.splitlines():
        first = line.split(",")[0].strip().strip('"')
        if first.lower().startswith(TASK_FOLDER.lower()) and first not in names:
            names.append(first)
    return names


def set_tasks(names: list[str], enable: bool) -> None:
    for n in names:
        _schtasks("/Change", "/TN", n, "/ENABLE" if enable else "/DISABLE")


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


def read_zip(path: Path) -> tuple[str, list[tuple[str, bytes]]]:
    """Validate a btcperp release zip; return (version, [(relative path, bytes)])."""
    with zipfile.ZipFile(path) as z:
        members = []
        for info in z.infolist():
            if info.is_dir():
                continue
            parts = PurePosixPath(info.filename).parts
            if not parts or parts[0] != "btcperp" or ".." in parts or len(parts) < 2:
                raise SystemExit(f"not a btcperp release zip (unexpected entry {info.filename})")
            rel = "/".join(parts[1:])
            if parts[1] in PROTECTED or rel.endswith(".env"):
                raise SystemExit(f"refusing zip: it contains {rel}")
            members.append((rel, z.read(info)))
    names = {m[0] for m in members}
    if "VERSION" not in names or "MANIFEST.txt" not in names or "install.py" not in names:
        raise SystemExit("not a btcperp release zip (VERSION / MANIFEST.txt / install.py missing)")
    version = dict(members)["VERSION"].decode().strip()
    return version, members


def extract(members: list[tuple[str, bytes]]) -> None:
    for rel, data in members:
        dest = ROOT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".new")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        if rel in ("run.py", "install.py") and os.name != "nt":
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR)


def run_tests(py: Path) -> int:
    env = dict(os.environ, PYTHONPATH=str(ROOT), BTCPERP_NO_TOAST="1", BTCPERP_NO_SCHTASKS="1")
    check = ("from pathlib import Path; from perpbot.config import load_config; from perpbot.envsecrets import "
             "load_secrets; from perpbot.calendar_events import load_calendar; c = load_config(Path('config/config.yaml')); "
             "load_calendar(Path(c.gates.calendar_file)); load_secrets(Path('.env')); print('config, calendar and .env OK')")
    if subprocess.call([str(py), "-c", check], cwd=str(ROOT), env=env) != 0:
        say("FAIL: config/config.yaml, calendar or .env could not be read (see above)")
        return 1
    say("running unit tests ...")
    rc = subprocess.call([str(py), "-m", "pytest", "-q", "-p", "no:cacheprovider", str(ROOT / "tests")],
                         cwd=str(ROOT), env=env)
    print("SELFTEST", "PASS" if rc == 0 else "FAIL")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="btcperp installer / upgrader")
    ap.add_argument("--from-zip", help="upgrade from this btcperp_vX.Y.Z.zip")
    args = ap.parse_args()
    if sys.version_info < MIN:
        say(f"FAIL: Python {MIN[0]}.{MIN[1]}+ required, found {sys.version.split()[0]}")
        return 1
    say(f"Python {sys.version.split()[0]} OK")
    old_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip() if (ROOT / "VERSION").exists() else "?"
    members = None
    if args.from_zip:
        zp = Path(args.from_zip)
        if not zp.is_file():
            say(f"FAIL: {zp} not found")
            return 1
        new_version, members = read_zip(zp)
        say(f"upgrade {old_version} -> {new_version} from {zp.name}")
    for d in ("data", "logs"):
        (ROOT / d).mkdir(exist_ok=True)

    # --- stop the scheduled bot before changing any file
    tasks = existing_tasks()
    if tasks:
        say(f"disabling {len(tasks)} scheduled tasks for the upgrade")
        set_tasks(tasks, enable=False)
        _schtasks("/End", "/TN", TASK_FOLDER + "dashboard")
    sys.path.insert(0, str(ROOT))
    from perpbot.lockfile import FileLock, LockTimeout  # stdlib only; safe before the venv exists

    lock = FileLock(ROOT / "data" / "btcperp.lock")
    try:
        say("waiting for any running bot command to finish ...")
        lock.acquire(LOCK_WAIT_SECONDS, poll=2.0)
    except LockTimeout as e:
        say(f"FAIL: {e}. Nothing was changed.")
        if tasks:
            set_tasks(tasks, enable=True)
        return 1
    try:
        if members is not None:
            say(f"writing {len(members)} files ...")
            extract(members)
        env, example = ROOT / ".env", ROOT / ".env.example"
        if not env.exists():
            shutil.copy2(example, env)
            say(".env created from .env.example (fill it in with windows\\Edit_Secrets.bat; never share it)")
        else:
            say(".env exists - left untouched")
        if os.name != "nt":
            env.chmod(stat.S_IRUSR | stat.S_IWUSR)
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
        rc = run_tests(py)
    finally:
        lock.release()
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

    if rc != 0:
        print(f"\nINSTALL FAIL (btcperp v{version}): tests failed - keep the output above and the logs folder "
              f"(never .env) for troubleshooting.")
        if tasks:
            print("THE SCHEDULED TASKS STAY DISABLED: the bot is NOT running. An open position is protected only by "
                  "its stop-loss / take-profit on the exchange.")
        return 1
    if tasks:
        say("re-registering the scheduled tasks with this version's settings ...")
        r = subprocess.call([str(py), "-m", "perpbot", "schedule", "install", "--upgrade"], cwd=str(ROOT),
                            env=dict(os.environ, PYTHONPATH=str(ROOT)))
        if r != 0:
            say("re-registration failed; re-enabling the previous tasks")
            set_tasks(tasks, enable=True)
            _schtasks("/Run", "/TN", TASK_FOLDER + "dashboard")
    print(f"\nINSTALL PASS (btcperp v{version})")
    if os.name == "nt":
        if tasks:
            print("The scheduled bot is running again. Check windows\\Status.bat and the dashboard, then")
            print("windows\\Unpause.bat if you paused before the upgrade. Run windows\\2_Smoketest.bat when convenient.")
        else:
            print("Next steps (see START_HERE.md):")
            print("  1. windows\\Proxy_Key.bat      - create the proxy key (the main wallet key never touches this PC)")
            print("  2. windows\\2_Smoketest.bat    - live check at minimum size")
            print("  3. windows\\Backtest.bat       - download history and run the backtest")
            print("  4. windows\\3_Schedule_Install.bat - only when you decide to go live")
            print("  Dashboard any time: windows\\Dashboard.bat")
    else:
        print("Next step: fill in .env (proxy key, proxy secret, wallet address),")
        print("then run:  python3 run.py smoketest   and check the result. Do NOT schedule routines before GO.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
