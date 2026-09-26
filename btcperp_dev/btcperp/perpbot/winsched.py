"""Windows Task Scheduler setup for the bot's routines.

`run.py schedule install` writes one task XML per routine into data/tasks/ and registers it with
`schtasks /Create /XML` under the Task Scheduler folder "\\btcperp\\". Tasks run the venv's pythonw.exe
(no console window) as the logged-on user, wake the computer if allowed, and start as soon as possible
after a missed start (a late `decide` is recognised by the bot and logged as missed; it never enters late).
"""

from __future__ import annotations

import os
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


def task_xml(spec: TaskSpec, root: Path, python_exe: Path, offset_hours: float, now_local: datetime) -> str:
    trig = ""
    if spec.kind == "logon":
        trig = "<LogonTrigger><Enabled>true</Enabled><Delay>PT1M</Delay></LogonTrigger>"
    else:
        h, m, shift = hkt_to_local(spec.hkt, offset_hours)
        boundary = start_boundary(h, m, now_local)
        if spec.kind == "daily":
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
      <LogonType>InteractiveToken</LogonType>
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
        xml = task_xml(spec, root, _python_exe(root), off, now_local)
        path = tasks_dir / f"{spec.name}.xml"
        path.write_text(xml, encoding="utf-16")
        name = f"\\{FOLDER}\\{spec.name}"
        if dry_run or os.name != "nt":
            results.append({"task": name, "ok": None, "xml": str(path), "output": "dry run (not registered)"})
            continue
        rc, out = _run(["schtasks", "/Create", "/TN", name, "/XML", str(path), "/F"])
        results.append({"task": name, "ok": rc == 0, "xml": str(path), "output": out})
        if rc == 0 and spec.kind == "logon":
            _run(["schtasks", "/Run", "/TN", name])        # start the dashboard now, not only at next logon
    return results


def remove(cfg: Any) -> list[dict[str, Any]]:
    results = []
    for spec in plan(cfg, True):
        name = f"\\{FOLDER}\\{spec.name}"
        if os.name != "nt":
            results.append({"task": name, "ok": None, "output": "not Windows"})
            continue
        if spec.kind == "logon":
            _run(["schtasks", "/End", "/TN", name])        # stop the running dashboard server first
        rc, out = _run(["schtasks", "/Delete", "/TN", name, "/F"])
        results.append({"task": name, "ok": rc == 0, "output": out})
    return results


def listing(cfg: Any) -> list[dict[str, Any]]:
    results = []
    for spec in plan(cfg, True):
        name = f"\\{FOLDER}\\{spec.name}"
        if os.name != "nt":
            results.append({"task": name, "ok": None, "output": "not Windows"})
            continue
        rc, out = _run(["schtasks", "/Query", "/TN", name, "/FO", "LIST"])
        results.append({"task": name, "ok": rc == 0, "output": out})
    return results


def summary_lines(cfg: Any, offset_hours: float | None = None) -> list[str]:
    off = local_offset_hours() if offset_hours is None else offset_hours
    lines = []
    if abs(off - 8.0) > 1e-9:
        lines.append(f"WARNING: this computer is at UTC{off:+.1f}, not HKT (UTC+8). Times were converted; if your "
                     f"clock changes for daylight saving, run `schedule install` again.")
    for spec in plan(cfg, True):
        if spec.kind == "logon":
            lines.append(f"{spec.name:<16} at logon (dashboard server)")
            continue
        h, m, shift = hkt_to_local(spec.hkt, off)
        when = {"daily": "daily", "weekly": f"every {spec.weekday}", "monthly_first": "first Sunday"}[spec.kind]
        lines.append(f"{spec.name:<16} {spec.hkt} HKT = {h:02d}:{m:02d} local{' (day shift %+d)' % shift if shift else ''}  {when}")
    return lines
