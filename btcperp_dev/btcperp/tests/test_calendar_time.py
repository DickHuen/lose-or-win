"""Event window across US daylight saving, entry window, client order ids."""

from datetime import date, datetime, timedelta

from perpbot.calendar_events import load_calendar, parse_calendar
from perpbot.records import client_order_id
from perpbot.timeutil import UTC, entry_window, fmt_hkt

from conftest import ROOT, hkt


def cal(events):
    return parse_calendar({"timezone": "America/New_York", "events": events,
                           "coverage_end": {"FOMC": "2027-12-31", "CPI": "2026-12-31", "NFP": "2026-12-31"}})


def test_event_window_summer_edt():
    c = cal([{"type": "CPI", "date": "2026-07-14", "time": "08:30"}])
    ev = c.events[0]
    assert ev.release_utc == datetime(2026, 7, 14, 12, 30, tzinfo=UTC)          # EDT = UTC-4
    start, end = ev.window("08:30", 4)
    assert start == datetime(2026, 7, 14, 0, 30, tzinfo=UTC)                    # 08:30 HKT same day
    assert end == datetime(2026, 7, 14, 16, 30, tzinfo=UTC)
    assert c.is_blocked(hkt(2026, 7, 14, 8, 30), "08:30", 4)
    assert not c.is_blocked(hkt(2026, 7, 13, 8, 30), "08:30", 4)
    assert not c.is_blocked(datetime(2026, 7, 14, 16, 31, tzinfo=UTC), "08:30", 4)


def test_event_window_winter_est_and_dst_switch():
    c = cal([{"type": "CPI", "date": "2026-12-10", "time": "08:30"},
             {"type": "NFP", "date": "2026-11-06", "time": "08:30"},    # after US DST ended (Nov 1 2026)
             {"type": "NFP", "date": "2026-10-30", "time": "08:30"},    # before DST ended
             {"type": "FOMC", "date": "2026-12-09", "time": "14:00"}])
    by = {(e.type, e.release_utc.date().isoformat()): e for e in c.events}
    assert by[("CPI", "2026-12-10")].release_utc == datetime(2026, 12, 10, 13, 30, tzinfo=UTC)   # EST = UTC-5
    assert by[("NFP", "2026-11-06")].release_utc.hour == 13
    assert by[("NFP", "2026-10-30")].release_utc.hour == 12
    fomc = by[("FOMC", "2026-12-09")]
    assert fomc.release_utc == datetime(2026, 12, 9, 19, 0, tzinfo=UTC)       # 03:00 HKT Dec 10
    start, end = fomc.window("08:30", 4)
    assert start == datetime(2026, 12, 9, 0, 30, tzinfo=UTC)                  # 08:30 HKT Dec 9
    assert end == datetime(2026, 12, 9, 23, 0, tzinfo=UTC)
    assert c.is_blocked(hkt(2026, 12, 9, 8, 50), "08:30", 4)
    assert c.is_blocked(hkt(2026, 12, 10, 8, 30), "08:30", 4)                 # CPI Dec 10
    assert not c.is_blocked(hkt(2026, 12, 11, 8, 30), "08:30", 4)


def test_shipped_calendar_loads_and_warns_about_coverage():
    c = load_calendar(ROOT / "config" / "calendar.yaml")
    assert {e.type for e in c.events} == {"FOMC", "CPI", "NFP"}
    assert all(e.release_utc.year in (2026, 2027) for e in c.events)
    warns = c.coverage_warnings(date(2026, 12, 1), 45)
    assert any("CPI" in w for w in warns) and any("NFP" in w for w in warns)
    assert not c.coverage_warnings(date(2026, 9, 26), 45)


def test_entry_window_hkt():
    now = hkt(2026, 10, 5, 8, 50)
    ws, we = entry_window(now, "08:30", "09:30")
    assert fmt_hkt(ws).startswith("2026-10-05 08:30") and fmt_hkt(we).startswith("2026-10-05 09:30")
    assert ws == datetime(2026, 10, 5, 0, 30, tzinfo=UTC) and we - ws == timedelta(hours=1)


def test_client_order_id_format():
    c = client_order_id("2026-10-05", "entry:1")
    assert len(c) == 32 and c == c.lower() and all(ch in "0123456789abcdef" for ch in c)
    assert c == client_order_id("2026-10-05", "entry:1")
    assert c != client_order_id("2026-10-05", "entry:2") != client_order_id("2026-10-06", "entry:1")
