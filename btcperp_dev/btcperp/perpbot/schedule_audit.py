"""Scheduled-slot bookkeeping: run lateness and missed runs (from timestamps, never run counts)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from perpbot.timeutil import HKT, hkt_at, hkt_date, to_ms

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def intraday_times(cfg: Any) -> list[str]:
    """v2.0.0: HH:MM HKT of the 15-minute intraday runs (empty when intraday is off)."""
    sec = cfg.get("intraday") if hasattr(cfg, "get") else None
    if sec is None or not sec.get("enabled"):
        return []
    every, off = int(cfg.schedule.intraday_every_minutes), int(cfg.schedule.intraday_offset_minutes)
    return [f"{m // 60:02d}:{m % 60:02d}" for m in range(off, 24 * 60, every)]


def slots_for_day(cfg: Any, d: date) -> list[tuple[str, datetime]]:
    sc = cfg.schedule
    out = [("decide", hkt_at(d, t)) for t in list(sc.decide_times_hkt) + intraday_times(cfg)]
    out += [("manage", hkt_at(d, t)) for t in sc.manage_times_hkt]
    out.append(("report_daily", hkt_at(d, sc.report_daily_time_hkt)))
    out.append(("backup", hkt_at(d, sc.backup_time_hkt)))
    wk = sc.report_weekly
    if WEEKDAYS[d.weekday()] == str(wk.weekday).lower():
        out.append(("report_weekly", hkt_at(d, wk.time_hkt)))
    mo = sc.report_monthly
    if d.weekday() == 6 and d.day <= 7 and str(mo.rule) == "first_sunday":
        out.append(("report_monthly", hkt_at(d, mo.time_hkt)))
    return out


def nearest_slot(cfg: Any, command: str, when: datetime) -> tuple[datetime | None, float | None]:
    tol = timedelta(minutes=float(cfg.schedule.missed_tolerance_minutes))
    best = None
    for off in (-1, 0, 1):
        d = hkt_date(when) + timedelta(days=off)
        for cmd, t in slots_for_day(cfg, d):
            if cmd == command and abs(when - t) <= tol and (best is None or abs(when - t) < abs(when - best)):
                best = t
    if best is None:
        return None, None
    return best, (when - best).total_seconds() / 60.0


def audit(store: Any, cfg: Any, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Every slot in [start, end) with status ok / late / missed."""
    tol = timedelta(minutes=float(cfg.schedule.missed_tolerance_minutes))
    late = float(cfg.schedule.late_tolerance_minutes)
    runs = store.query("SELECT command, ts_ms FROM runs WHERE event='start' AND ts_ms >= ? AND ts_ms < ?",
                       [to_ms(start - tol), to_ms(end + tol)])
    out = []
    d = hkt_date(start)
    while d <= hkt_date(end):
        for cmd, t in slots_for_day(cfg, d):
            if not (start <= t < end):
                continue
            starts = [r["ts_ms"] for r in runs if r["command"] == cmd and abs(r["ts_ms"] - to_ms(t)) <= tol.total_seconds() * 1000]
            if not starts:
                status, lateness = "missed", None
            else:
                first = min(starts, key=lambda ms: abs(ms - to_ms(t)))
                lateness = (first - to_ms(t)) / 60_000
                status = "late" if lateness > late else "ok"
            out.append({"command": cmd, "slot_hkt": t.astimezone(HKT).strftime("%Y-%m-%d %H:%M"), "status": status,
                        "lateness_min": lateness})
        d += timedelta(days=1)
    return out
