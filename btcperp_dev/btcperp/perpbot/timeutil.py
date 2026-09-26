"""Time helpers. The bot's local timezone is HKT (fixed UTC+8, no DST).

All windows and counts are derived from UTC timestamps and UTC daily candles.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

UTC = timezone.utc
HKT = timezone(timedelta(hours=8), "HKT")
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
MINUTE_MS = 60_000


class Clock:
    def now(self) -> datetime:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class FixedClock(Clock):
    """Test clock. Always returns an aware UTC datetime."""

    def __init__(self, dt: datetime) -> None:
        self._dt = dt.astimezone(UTC)

    def now(self) -> datetime:
        return self._dt

    def set(self, dt: datetime) -> None:
        self._dt = dt.astimezone(UTC)

    def advance(self, **kwargs: float) -> None:
        self._dt = self._dt + timedelta(**kwargs)


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def utc_day(dt: datetime) -> date:
    return dt.astimezone(UTC).date()


def day_start_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def day_start_ms(d: date) -> int:
    return to_ms(day_start_utc(d))


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_hkt(dt: datetime) -> str:
    return dt.astimezone(HKT).strftime("%Y-%m-%d %H:%M:%S HKT")


def parse_hhmm(s: str) -> time:
    hh, mm = s.strip().split(":")
    return time(int(hh), int(mm))


def hkt_at(hkt_date: date, hhmm: str) -> datetime:
    """HKT wall-clock time on an HKT date, returned in UTC."""
    t = parse_hhmm(hhmm)
    return datetime(hkt_date.year, hkt_date.month, hkt_date.day, t.hour, t.minute, tzinfo=HKT).astimezone(UTC)


def hkt_date(dt: datetime) -> date:
    return dt.astimezone(HKT).date()


def entry_window(now: datetime, start_hkt: str, end_hkt: str) -> tuple[datetime, datetime]:
    """Entry window for the HKT date of `now`, in UTC."""
    d = hkt_date(now)
    return hkt_at(d, start_hkt), hkt_at(d, end_hkt)


def latest_hkt_anchor_at_or_before(moment: datetime, hhmm: str) -> datetime:
    """Latest instant <= moment whose HKT wall clock equals hhmm (UTC-aware)."""
    d = hkt_date(moment)
    cand = hkt_at(d, hhmm)
    if cand > moment:
        cand = hkt_at(d - timedelta(days=1), hhmm)
    return cand
