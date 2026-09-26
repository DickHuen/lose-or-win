"""FOMC / CPI / NFP event calendar and the event-window gate.

Release times are stored in US Eastern local time and converted to UTC with
zoneinfo, so US daylight-saving changes are handled per date.
Window: from the HKT 08:30 anchor at or before the release until release + N hours.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from perpbot.timeutil import UTC, latest_hkt_anchor_at_or_before

VALID_TYPES = ("FOMC", "CPI", "NFP")


class CalendarError(Exception):
    pass


@dataclass(frozen=True)
class Event:
    type: str
    release_utc: datetime
    note: str

    def window(self, anchor_hkt: str, post_hours: float) -> tuple[datetime, datetime]:
        start = latest_hkt_anchor_at_or_before(self.release_utc, anchor_hkt)
        end = self.release_utc + timedelta(hours=post_hours)
        return start, end


@dataclass(frozen=True)
class EventCalendar:
    version: str
    events: tuple[Event, ...]
    coverage_end: dict[str, date]

    def active_windows(self, now: datetime, anchor_hkt: str, post_hours: float) -> list[tuple[Event, datetime, datetime]]:
        out = []
        for ev in self.events:
            start, end = ev.window(anchor_hkt, post_hours)
            if start <= now <= end:
                out.append((ev, start, end))
        return out

    def is_blocked(self, now: datetime, anchor_hkt: str, post_hours: float) -> bool:
        return bool(self.active_windows(now, anchor_hkt, post_hours))

    def upcoming(self, now: datetime, days: int) -> list[Event]:
        limit = now + timedelta(days=days)
        return [e for e in self.events if now <= e.release_utc <= limit]

    def coverage_warnings(self, today: date, warn_days: int) -> list[str]:
        warns = []
        for t in VALID_TYPES:
            end = self.coverage_end.get(t)
            if end is None:
                warns.append(f"calendar has no coverage for {t}")
            elif (end - today).days <= warn_days:
                warns.append(f"calendar coverage for {t} ends {end.isoformat()} "
                             f"({(end - today).days} days) - please send an updated calendar.yaml")
        return warns


def parse_calendar(raw: dict) -> EventCalendar:
    if not isinstance(raw, dict):
        raise CalendarError("calendar is not a mapping")
    tzname = raw.get("timezone", "America/New_York")
    try:
        tz = ZoneInfo(tzname)
    except Exception as e:  # noqa: BLE001
        raise CalendarError(f"unknown calendar timezone {tzname}: {e}") from e
    events = []
    for i, item in enumerate(raw.get("events") or []):
        try:
            typ = str(item["type"]).upper()
            if typ not in VALID_TYPES:
                raise CalendarError(f"event {i}: unknown type {typ}")
            d = date.fromisoformat(str(item["date"]))
            hh, mm = str(item["time"]).split(":")
            local = datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=tz)
            events.append(Event(type=typ, release_utc=local.astimezone(UTC), note=str(item.get("note", ""))))
        except (KeyError, ValueError) as e:
            raise CalendarError(f"event {i} invalid: {e}") from e
    cov = {}
    for k, v in (raw.get("coverage_end") or {}).items():
        cov[str(k).upper()] = date.fromisoformat(str(v))
    events.sort(key=lambda e: e.release_utc)
    return EventCalendar(version=str(raw.get("calendar_version", "")), events=tuple(events), coverage_end=cov)


def load_calendar(path: Path) -> EventCalendar:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise CalendarError(f"cannot load calendar {path}: {e}") from e
    return parse_calendar(raw)
