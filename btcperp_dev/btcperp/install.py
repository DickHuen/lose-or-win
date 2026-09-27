#!/usr/bin/env python3
"""Installer / upgrader.

  python install.py                      install, or re-install in place   (Windows: windows\\1_Install.bat)
  python install.py --from-zip FILE.zip  upgrade from a new btcperp_vX.Y.Z.zip (Windows: windows\\Upgrade.bat)
  python install.py --from-zip FILE.zip --test-restore
                                         go-live check B3: installs the zip, then acts as if it failed and
                                         restores the installed version (an equal version is allowed here)

- checks the Python version (3.11+)
- creates ./venv (or reuses it) and installs the pinned requirements
- creates data/ and logs/ if missing; copies .env.example to .env only if .env does not exist
- never touches data/, logs/, venv/ or .env
- on Windows, if the bot's scheduled tasks exist (review v1.2.0 item 8): disables them and waits for the
  bot's file lock BEFORE changing any file, so no run can see half-old / half-new code. If Task Scheduler
  cannot be read, nothing is changed (review v1.3.0 U1).
- upgrade: refuses a zip that is not newer and prints its SHA-256 (U3); backs up the installed version into
  data/upgrade_backup_<version>_<time>/ first; if ANY step fails (files, pip, tests) it restores the old
  version, reinstalls its packages and re-enables the tasks (U2). If even the restore fails, the tasks stay
  disabled and the open position with its stop-loss is printed.
- removes code files left over from an older version (per MANIFEST.txt)
- runs the unit tests and prints PASS/FAIL and the next step
Safe to re-run.
"""

import argparse
import hashlib
import json
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


class TaskQueryError(Exception):
    pass


def existing_tasks() -> list[str]:
    """Names of the bot's scheduled tasks (Windows only). Raises TaskQueryError if Task Scheduler can't be read."""
    if os.name != "nt":
        return []
    rc, out = _schtasks("/Query", "/FO", "CSV", "/NH")
    if rc != 0:
        raise TaskQueryError(f"schtasks /Query failed (exit {rc}): {out.strip()[:300]}")
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
    for sub in ("perpbot", "tests", "windows", "offline_sign"):
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


def version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except ValueError:
        return (0,)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


CODE_DIRS = ("perpbot", "tests", "windows", "offline_sign")


def backup_install(dest: Path) -> list[str]:
    """Copy the installed version (code, config, top-level files; never data/logs/venv/.env) into dest."""
    saved = []
    for p in sorted(ROOT.iterdir()):
        if p.name in PROTECTED or p.name.startswith(".") and p.name != ".env.example":
            continue
        if p.is_dir() and p.name in CODE_DIRS + ("config",):
            shutil.copytree(p, dest / p.name, ignore=shutil.ignore_patterns("__pycache__"))
            saved.append(p.name + "/")
        elif p.is_file():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest / p.name)
            saved.append(p.name)
    return saved


def restore_install(src: Path, new_members: list[str]) -> None:
    """Put the backed-up version back and remove files that only the new version had."""
    new_dirs = {rel.split("/")[0] for rel in new_members if "/" in rel}
    for d in CODE_DIRS + ("config",):
        if (src / d).exists():
            if (ROOT / d).exists():
                shutil.rmtree(ROOT / d)
            shutil.copytree(src / d, ROOT / d)
        elif d in new_dirs and (ROOT / d).is_dir():
            shutil.rmtree(ROOT / d)                         # a folder only the new version had
    for p in src.iterdir():
        if p.is_file():
            shutil.copy2(p, ROOT / p.name)
    for rel in new_members:
        top = rel.split("/")[0]
        if "/" not in rel and not (src / top).exists() and (ROOT / top).is_file():
            (ROOT / top).unlink()


def reinstall_requirements() -> None:
    """Restore step: put the old version's pinned packages back. Without internet, accept packages that are
    already installed (pip --no-index); anything else fails the restore."""
    req = str(ROOT / "requirements.txt")
    try:
        subprocess.check_call([str(venv_python()), "-m", "pip", "install", "--quiet", "-r", req])
    except subprocess.CalledProcessError:
        say("pip could not reach the package index; checking the installed packages offline ...")
        subprocess.check_call([str(venv_python()), "-m", "pip", "install", "--quiet", "--no-index", "-r", req])


def show_position() -> None:
    """After a failed restore: print the open position and its stop-loss from the bot's database."""
    import sqlite3

    db = ROOT / "data" / "btcperp.sqlite3"
    if not db.exists():
        print("(no database: no position recorded)")
        return
    try:
        con = sqlite3.connect(str(db))
        for table in ("dash_snapshots", "manage_log"):
            row = con.execute(f"SELECT ts_hkt, data FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                d = json.loads(row[1])
                pos = d.get("position") or {}
                print(f"LAST KNOWN ({table}, {row[0]}): position size {pos.get('size')} @ {pos.get('entry_price')}, "
                      f"stop-loss {d.get('sl')}, take-profit {d.get('tp')}, mark {d.get('mark')}")
                return
        print("(no position snapshot recorded)")
    except Exception as e:  # noqa: BLE001
        print(f"(could not read the database: {e})")


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


def _install_steps(members: list[tuple[str, bytes]] | None) -> tuple[Path, int]:
    """Write files, venv, pip, tests. Returns (venv python, test rc). Raises on a failed step."""
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
    return py, run_tests(py)


def main() -> int:
    ap = argparse.ArgumentParser(description="btcperp installer / upgrader")
    ap.add_argument("--from-zip", help="upgrade from this btcperp_vX.Y.Z.zip")
    ap.add_argument("--test-restore", action="store_true",
                    help="with --from-zip: install it, then simulate a failure and restore the installed version")
    args = ap.parse_args()
    if args.test_restore and not args.from_zip:
        say("FAIL: --test-restore needs --from-zip")
        return 1
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
        say(f"zip SHA-256: {sha256_file(zp)}  (compare with the value published with the release)")
        new_version, members = read_zip(zp)
        if args.test_restore:
            if old_version == "?" or version_tuple(new_version) < version_tuple(old_version):
                say(f"FAIL: the restore test needs an installed version and a zip that is not older "
                    f"(installed {old_version}, zip {new_version}). Nothing changed.")
                return 1
            say(f"RESTORE TEST: installs {new_version}, then simulates a failure and must restore {old_version}")
        elif old_version != "?" and version_tuple(new_version) <= version_tuple(old_version):
            say(f"FAIL: {zp.name} is version {new_version}, not newer than the installed {old_version}. Nothing changed.")
            return 1
        else:
            say(f"upgrade {old_version} -> {new_version} from {zp.name}")
    for d in ("data", "logs"):
        (ROOT / d).mkdir(exist_ok=True)

    # --- stop the scheduled bot before changing any file
    try:
        tasks = existing_tasks()
    except TaskQueryError as e:
        say(f"FAIL: cannot read Task Scheduler ({e}). Nothing was changed.")
        return 1
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
    backup = None
    rc, py, failure = 1, venv_python(), None
    try:
        if members is not None and old_version != "?":
            from datetime import datetime

            backup = ROOT / "data" / f"upgrade_backup_{old_version}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            n = 1
            while backup.exists():
                n += 1
                backup = backup.with_name(f"{backup.name.split('#')[0]}#{n}")
            say(f"backing up version {old_version} to {backup} ...")
            backup_install(backup)
        try:
            py, rc = _install_steps(members)
            if rc != 0:
                failure = "the tests failed"
            elif args.test_restore:
                failure = "restore test: failure simulated after the new version installed and passed its tests"
        except (OSError, subprocess.CalledProcessError, SystemExit) as e:
            failure = f"{type(e).__name__}: {e}"
        if failure and backup is not None:
            say(f"UPGRADE FAILED ({failure}) - restoring version {old_version} ...")
            try:
                restore_install(backup, [m[0] for m in members or []])
                reinstall_requirements()
                restored = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
                say(f"restored version {restored}")
                if tasks:
                    set_tasks(tasks, enable=True)
                    _schtasks("/Run", "/TN", TASK_FOLDER + "dashboard")
                if args.test_restore:
                    ok = restored == old_version
                    print(f"\nRESTORE TEST {'PASSED' if ok else 'FAILED'}: version {restored} is installed"
                          f"{' again' if ok else f' (expected {old_version})'}"
                          f"{' and the scheduled tasks are enabled' if tasks else ''}. Backup: {backup}")
                    return 0 if ok else 1
                print(f"\nUPGRADE FAILED - version {restored} was restored and the scheduled bot runs as before.")
                print("Keep the output above and the logs folder (never .env) for troubleshooting.")
                return 1
            except Exception as e:  # noqa: BLE001
                print("\n" + "!" * 78)
                print(f"!!! RESTORE FAILED ({type(e).__name__}: {e}).")
                print("!!! THE SCHEDULED TASKS STAY DISABLED: THE BOT IS NOT RUNNING.")
                print("!!! An open position is protected only by its stop-loss / take-profit on the exchange.")
                show_position()
                print(f"!!! Backup of the old version: {backup}")
                print("!" * 78)
                return 1
    finally:
        lock.release()
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

    if failure:
        print(f"\nINSTALL FAIL (btcperp v{version}): {failure} - keep the output above and the logs folder "
              f"(never .env) for troubleshooting.")
        if tasks:                                      # in-place reinstall: the code did not change
            set_tasks(tasks, enable=True)
            print("The files were not changed, so the scheduled tasks were re-enabled.")
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
