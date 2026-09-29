"""Windows Task Scheduler setup for the bot's routines.

`run.py schedule install` writes one task XML per routine into data/tasks/ and registers it with
`schtasks /Create /XML` under the Task Scheduler folder "\\btcperp\\". Tasks run the venv's pythonw.exe
(no console window) as the logged-on user, wake the computer if allowed, and start as soon as possible
after a missed start (a late `decide` is recognised by the bot and logged as missed; it never enters late).
Daily tasks are pinned to Hong Kong time (StartBoundary with +08:00), so a time-zone or daylight-saving
change on this computer does not move them (review v1.2.0 item 13).
The folder the tasks run in is recorded in %LOCALAPPDATA%\\btcperp\\install_root.txt; manual commands
refuse to run from any other copy (review v1.2.0 item 16).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

FOLDER = "btcperp"
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


@dataclass
class TaskSpec:
    name: str              # e.g. decide_0830
    args: str              # arguments after `-m perpbot`
    kind: str              # daily | weekly | monthly_first | logon
    hkt: str = ""          # HH:MM in HKT
    weekday: str = "Sunday"
    time_limit: str = "PT45M"


def plan(cfg: Any, with_dashboard: bool = True) -> list[TaskSpec]:
    sc = cfg.schedule
    out: list[TaskSpec] = []
    for t in sc.decide_times_hkt:
        out.append(TaskSpec(f"decide_{t.replace(':', '')}", "decide", "daily", t))
    for t in sc.manage_times_hkt:
        out.append(TaskSpec(f"manage_{t.replace(':', '')}", "manage", "daily", t))
    out.append(TaskSpec("report_daily", "report daily", "daily", sc.report_daily_time_hkt))
    out.append(TaskSpec("report_weekly", "report weekly", "weekly", sc.report_weekly.time_hkt,
                        str(sc.report_weekly.weekday).capitalize()))
    out.append(TaskSpec("report_monthly", "report monthly --only-first-sunday", "monthly_first",
                        sc.report_monthly.time_hkt))
    out.append(TaskSpec("backup", "backup", "daily", sc.backup_time_hkt))
    if with_dashboard:
        out.append(TaskSpec("dashboard", "dashboard --no-browser", "logon", time_limit="PT0S"))
    return out


def local_offset_hours() -> float:
    return -(time.altzone if time.localtime().tm_isdst > 0 else time.timezone) / 3600.0


def hkt_to_local(hhmm: str, offset_hours: float) -> tuple[int, int, int]:
    """(hour, minute, day_shift) of an HKT wall time on a machine at UTC+offset_hours."""
    hh, mm = (int(x) for x in hhmm.split(":"))
    total = hh * 60 + mm + int(round((offset_hours - 8.0) * 60))
    shift = total // (24 * 60)
    total %= 24 * 60
    return total // 60, total % 60, shift


def start_boundary(h: int, m: int, now_local: datetime) -> str:
    """Next future occurrence of local h:m, so registering a task never looks like a missed run."""
    first = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    if first <= now_local:
        first += timedelta(days=1)
    return first.strftime("%Y-%m-%dT%H:%M:%S")


def hkt_boundary(hhmm: str, now_local: datetime, offset_hours: float) -> str:
    """Next future HH:MM Hong Kong time, written with its +08:00 offset."""
    now_hkt = now_local - timedelta(hours=offset_hours) + timedelta(hours=8)
    hh, mm = (int(x) for x in hhmm.split(":"))
    first = now_hkt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if first <= now_hkt:
        first += timedelta(days=1)
    return first.strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"


def user_id() -> str | None:
    dom, user = os.environ.get("USERDOMAIN"), os.environ.get("USERNAME")
    return f"{dom}\\{user}" if (os.name == "nt" and dom and user) else None


def task_xml(spec: TaskSpec, root: Path, python_exe: Path, offset_hours: float, now_local: datetime,
             user: str | None = None) -> str:
    trig = ""
    uid = f"<UserId>{escape(user)}</UserId>" if user else ""
    if spec.kind == "logon":
        trig = f"<LogonTrigger><Enabled>true</Enabled>{uid}<Delay>PT1M</Delay></LogonTrigger>"
    else:
        h, m, shift = hkt_to_local(spec.hkt, offset_hours)
        boundary = start_boundary(h, m, now_local)
        if spec.kind == "daily":
            boundary = hkt_boundary(spec.hkt, now_local, offset_hours)
            sched = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
        elif spec.kind == "monthly_first" and shift == 0:
            months = "".join(f"<{mo} />" for mo in MONTHS)
            sched = ("<ScheduleByMonthDayOfWeek><Weeks><Week>1</Week></Weeks><DaysOfWeek><Sunday /></DaysOfWeek>"
                     f"<Months>{months}</Months></ScheduleByMonthDayOfWeek>")
        else:
            # weekly, or first-Sunday on a computer whose date differs from HKT at that time: run weekly on
            # the matching local weekday; `--only-first-sunday` checks the HKT date itself.
            base = spec.weekday if spec.kind == "weekly" else "Sunday"
            day = DAYS[(DAYS.index(base) + shift) % 7]
            sched = f"<ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek><{day} /></DaysOfWeek></ScheduleByWeek>"
        trig = f"<CalendarTrigger><StartBoundary>{boundary}</StartBoundary><Enabled>true</Enabled>{sched}</CalendarTrigger>"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>btcperp {escape(spec.args)} ({spec.hkt or 'at logon'} HKT)</Description>
  </RegistrationInfo>
  <Triggers>{trig}</Triggers>
  <Principals>
    <Principal id="Author">
      {uid}<LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>Queue</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>{spec.time_limit}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(python_exe))}</Command>
      <Arguments>-m perpbot {escape(spec.args)}</Arguments>
      <WorkingDirectory>{escape(str(root))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _live() -> bool:
    """Real Task Scheduler calls only on Windows and never from the test suite (conftest sets
    BTCPERP_NO_SCHTASKS): the selftest runs on the owner's PC and must never touch the real tasks."""
    return os.name == "nt" and not os.environ.get("BTCPERP_NO_SCHTASKS")


def _python_exe(root: Path) -> Path:
    return root / "venv" / "Scripts" / "pythonw.exe"


def _run(args: list[str]) -> tuple[int, str]:
    r = subprocess.run(args, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = (r.stdout or b"").decode(errors="replace") + (r.stderr or b"").decode(errors="replace")
    return r.returncode, out.strip()


def install(cfg: Any, root: Path, tasks_dir: Path, *, dry_run: bool = False, with_dashboard: bool = True,
            now_local: datetime | None = None, offset_hours: float | None = None) -> list[dict[str, Any]]:
    off = local_offset_hours() if offset_hours is None else offset_hours
    now_local = now_local or datetime.now()
    tasks_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for spec in plan(cfg, with_dashboard):
        xml = task_xml(spec, root, _python_exe(root), off, now_local, user_id())
        path = tasks_dir / f"{spec.name}.xml"
        path.write_text(xml, encoding="utf-16")
        name = f"\\{FOLDER}\\{spec.name}"
        if dry_run or not _live():
            results.append({"task": name, "ok": None, "xml": str(path), "output": "dry run (not registered)"})
            continue
        rc, out = _run(["schtasks", "/Create", "/TN", name, "/XML", str(path), "/F"])
        results.append({"task": name, "ok": rc == 0, "xml": str(path), "output": out})
        if rc == 0 and spec.kind == "logon":
            _run(["schtasks", "/Run", "/TN", name])        # start the dashboard now, not only at next logon
    if _live() and not dry_run:
        results += remove_stale(cfg)
    if _live() and not dry_run and any(r["ok"] for r in results):
        write_marker(root)
    return results


def registered_names() -> list[str] | None:
    """Every task registered in the \\btcperp\\ folder (None: Task Scheduler could not be read)."""
    rc, out = _run(["schtasks", "/Query", "/FO", "CSV", "/NH"])
    if rc != 0:
        return None
    prefix = f"\\{FOLDER}\\".lower()
    names = {line.split(",")[0].strip().strip('"') for line in out.splitlines()}
    return sorted(n for n in names if n.lower().startswith(prefix))


def remove_stale(cfg: Any) -> list[dict[str, Any]]:
    """v1.5.0: decide/manage tasks from an older schedule (e.g. the daily cadence's manage_1230) that are not in
    this plan are deleted, so the old and the new schedule never both run."""
    current = {f"\\{FOLDER}\\{spec.name}".lower() for spec in plan(cfg, True)}
    out = []
    for n in registered_names() or []:
        base = n.split("\\")[-1]
        if n.lower() not in current and base.startswith(("decide_", "manage_")):
            rc, text = _run(["schtasks", "/Delete", "/TN", n, "/F"])
            out.append({"task": n, "ok": rc == 0, "output": f"removed (not in this schedule) {text}".strip()})
    return out


def _marker() -> Path | None:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "btcperp" / "install_root.txt" if base else None


def write_marker(root: Path) -> None:
    m = _marker()
    if m is not None:
        m.parent.mkdir(parents=True, exist_ok=True)
        m.write_text(str(root), encoding="utf-8")


def registered_root() -> Path | None:
    """The install folder the scheduled tasks run in (None: not Windows, not installed, or folder gone)."""
    if not _live():
        return None
    m = _marker()
    if m is None or not m.exists():
        return None
    root = Path(m.read_text(encoding="utf-8").strip())
    return root if (root / "run.py").exists() else None


def power_lines() -> list[str]:
    """Review v1.2.0 item 14: sleep and wake-timer settings of the active power plan (plugged in)."""
    if os.name != "nt":
        return []
    out = []
    for label, sub, setting in (("sleep after (plugged in)", "SUB_SLEEP", "STANDBYIDLE"),
                                ("wake timers (plugged in)", "SUB_SLEEP", "RTCWAKE")):
        rc, text = _run(["powercfg", "/q", "SCHEME_CURRENT", sub, setting])
        vals = re.findall(r"0x[0-9a-fA-F]{8}", text)
        if rc != 0 or len(vals) < 2:
            out.append(f"power: {label}: unknown (powercfg failed)")
            continue
        ac = int(vals[-2], 16)
        if setting == "STANDBYIDLE":
            out.append(f"power: {label}: " + ("never  OK" if ac == 0 else f"{ac // 60} min  WARNING: set sleep to Never"))
        else:
            out.append(f"power: {label}: " + {0: "disabled  WARNING: enable wake timers", 1: "enabled  OK",
                                                2: "important only  WARNING: set to Enable"}.get(ac, str(ac)))
    return out


def remove(cfg: Any) -> list[dict[str, Any]]:
    results = []
    for spec in plan(cfg, True):
        name = f"\\{FOLDER}\\{spec.name}"
        if not _live():
            results.append({"task": name, "ok": None, "output": "not Windows"})
            continue
        if spec.kind == "logon":
            _run(["schtasks", "/End", "/TN", name])        # stop the running dashboard server first
        rc, out = _run(["schtasks", "/Delete", "/TN", name, "/F"])
        results.append({"task": name, "ok": rc == 0, "output": out})
    if _live():
        results += remove_stale(cfg)
    m = _marker()
    if _live() and m is not None and m.exists():
        m.unlink()
    return results


def listing(cfg: Any) -> list[dict[str, Any]]:
    results = []
    for spec in plan(cfg, True):
        name = f"\\{FOLDER}\\{spec.name}"
        if not _live():
            results.append({"task": name, "ok": None, "output": "not Windows"})
            continue
        rc, out = _run(["schtasks", "/Query", "/TN", name, "/FO", "LIST"])
        results.append({"task": name, "ok": rc == 0, "output": out})
    return results


def summary_lines(cfg: Any, offset_hours: float | None = None) -> list[str]:
    off = local_offset_hours() if offset_hours is None else offset_hours
    lines = []
    if abs(off - 8.0) > 1e-9:
        lines.append(f"NOTE: this computer is at UTC{off:+.1f}, not HKT (UTC+8). Daily tasks stay on HKT by themselves; "
                     f"after a daylight-saving change run `schedule install --upgrade` so the weekly reports follow.")
    for spec in plan(cfg, True):
        if spec.kind == "logon":
            lines.append(f"{spec.name:<16} at logon (dashboard server)")
            continue
        h, m, shift = hkt_to_local(spec.hkt, off)
        when = {"daily": "daily", "weekly": f"every {spec.weekday}", "monthly_first": "first Sunday"}[spec.kind]
        pinned = "  (pinned to HKT)" if spec.kind == "daily" else ""
        lines.append(f"{spec.name:<16} {spec.hkt} HKT = {h:02d}:{m:02d} local{' (day shift %+d)' % shift if shift else ''}  "
                     f"{when}{pinned}")
    return lines
