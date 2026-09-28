"""v1.2.0 Windows edition: Task Scheduler XML, desktop notifications, read-only snapshot, dashboard."""

import http.client
import json
import threading
import xml.dom.minidom
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from perpbot import notify, winsched
from perpbot.calendar_events import load_calendar
from perpbot.cli import Factories, main
from perpbot.config import load_config
from perpbot.dashboard import DashboardState, DashServer, build_summary, make_handler, serve
from perpbot.engine import Engine
from perpbot.exchange.mock import MockExchange
from perpbot.paths import Paths
from perpbot.storage import Store
from perpbot.timeutil import FixedClock

from conftest import FakeBinance, FakeTelegram, hkt, make_env, ok_leverage

TRADE_CALLS = {"place_order", "place_position_tpsl", "cancel_orders", "update_leverage"}


# ------------------------------------------------------------------ Task Scheduler
def _xml(spec, offset=8.0, now=datetime(2026, 10, 5, 10, 0)):
    x = winsched.task_xml(spec, Path("C:/btcperp"), Path("C:/btcperp/venv/Scripts/pythonw.exe"), offset, now)
    dom = xml.dom.minidom.parseString(x.replace('encoding="UTF-16"', ""))
    return x, dom


def _text(dom, tag):
    return dom.getElementsByTagName(tag)[0].firstChild.nodeValue


@pytest.mark.parametrize("hhmm, offset, expect", [
    ("08:30", 8.0, (8, 30, 0)),        # Hong Kong computer: unchanged
    ("08:30", 0.0, (0, 30, 0)),        # UTC computer
    ("04:30", 0.0, (20, 30, -1)),      # previous local day
    ("20:30", 9.0, (21, 30, 0)),       # Tokyo
    ("00:30", -4.0, (12, 30, -1)),     # New York (EDT)
    ("23:30", 10.0, (1, 30, 1)),       # next local day
])
def test_hkt_to_local(hhmm, offset, expect):
    assert winsched.hkt_to_local(hhmm, offset) == expect


def test_plan_covers_every_routine(cfg):
    names = {s.name for s in winsched.plan(cfg)}
    expected = {f"decide_{t.replace(':', '')}" for t in cfg.schedule.decide_times_hkt}
    expected |= {f"manage_{t.replace(':', '')}" for t in cfg.schedule.manage_times_hkt}
    expected |= {"report_daily", "report_weekly", "report_monthly", "backup", "dashboard"}
    assert names == expected
    assert "dashboard" not in {s.name for s in winsched.plan(cfg, with_dashboard=False)}
    monthly = next(s for s in winsched.plan(cfg) if s.name == "report_monthly")
    assert "--only-first-sunday" in monthly.args


def test_task_xml_daily_runs_pythonw_in_install_folder(cfg):
    spec = next(s for s in winsched.plan(cfg) if s.name == "decide_0830")
    x, dom = _xml(spec)
    assert _text(dom, "Command").endswith("pythonw.exe")
    assert _text(dom, "Arguments") == "-m perpbot decide"
    assert _text(dom, "WorkingDirectory") == str(Path("C:/btcperp"))
    assert _text(dom, "WakeToRun") == "true" and _text(dom, "StartWhenAvailable") == "true"
    assert _text(dom, "LogonType") == "InteractiveToken"
    assert dom.getElementsByTagName("ScheduleByDay")
    # registered at 10:00 local: 08:30 already passed today, so the first run is tomorrow (never "missed");
    # daily tasks are pinned to HKT with an explicit +08:00 offset (review v1.2.0 item 13)
    assert _text(dom, "StartBoundary") == "2026-10-06T08:30:00+08:00"


def test_task_xml_start_boundary_today_when_still_ahead(cfg):
    spec = next(s for s in winsched.plan(cfg) if s.name == "manage_1230")
    _, dom = _xml(spec)
    assert _text(dom, "StartBoundary") == "2026-10-05T12:30:00+08:00"
    # a London computer (UTC+1) at 10:00 local = 17:00 HKT: 12:30 HKT already passed -> tomorrow, still 12:30 HKT
    _, dom = _xml(spec, offset=1.0)
    assert _text(dom, "StartBoundary") == "2026-10-06T12:30:00+08:00"


def test_task_xml_weekly_and_monthly_day_shift(cfg):
    weekly = next(s for s in winsched.plan(cfg) if s.name == "report_weekly")
    monthly = next(s for s in winsched.plan(cfg) if s.name == "report_monthly")
    _, dom = _xml(weekly)
    assert dom.getElementsByTagName("Sunday")
    _, dom = _xml(monthly)                              # HKT computer: real "first Sunday" trigger
    assert dom.getElementsByTagName("ScheduleByMonthDayOfWeek") and _text(dom, "Week") == "1"
    # a computer at UTC+14: Sunday 20:00 HKT is Monday 02:00 local -> weekly Monday; the bot checks the HKT date
    _, dom = _xml(weekly, offset=14.0)
    assert dom.getElementsByTagName("Monday") and not dom.getElementsByTagName("Sunday")
    _, dom = _xml(monthly, offset=14.0)
    assert dom.getElementsByTagName("ScheduleByWeek") and dom.getElementsByTagName("Monday")
    assert not dom.getElementsByTagName("ScheduleByMonthDayOfWeek")


def test_task_xml_dashboard_at_logon_without_time_limit(cfg):
    spec = next(s for s in winsched.plan(cfg) if s.name == "dashboard")
    _, dom = _xml(spec)
    assert dom.getElementsByTagName("LogonTrigger")
    assert _text(dom, "ExecutionTimeLimit") == "PT0S"
    assert _text(dom, "Arguments") == "-m perpbot dashboard --no-browser"


def test_schedule_install_dry_run_writes_utf16_xml(cfg, tmp_path):
    res = winsched.install(cfg, tmp_path, tmp_path / "tasks", dry_run=True, offset_hours=8.0,
                           now_local=datetime(2026, 10, 5, 10, 0))
    assert len(res) == len(winsched.plan(cfg)) and all(r["ok"] is None for r in res)
    raw = (tmp_path / "tasks" / "decide_0830.xml").read_bytes()
    assert raw[:2] in (b"\xff\xfe", b"\xfe\xff")        # BOM: schtasks /XML needs UTF-16
    assert all(r["task"].startswith("\\btcperp\\") for r in res)


def test_cli_schedule_show_and_dry_run(tmp_root, capsys):
    make_env(tmp_root)
    paths = Paths(tmp_root)
    assert main(["schedule", "show"], paths=paths, clock=FixedClock(hkt(2026, 10, 5, 10, 0))) == 0
    out = capsys.readouterr().out
    assert "decide_0830" in out and "HKT" in out
    assert main(["schedule", "install", "--dry-run"], paths=paths, clock=FixedClock(hkt(2026, 10, 5, 10, 0))) == 0
    assert (tmp_root / "data" / "tasks" / "manage_0030.xml").exists()
    assert not paths.db_file.exists()                    # no lock, no database for schedule commands


# ------------------------------------------------------------------ notifications
def test_notifier_is_off_outside_windows(cfg):
    if notify.os.name != "nt":
        assert notify.make_notifier(cfg) is None
        assert notify.windows_toast("t", "b") is None


def test_notifier_on_windows_unless_tests_disable_it(cfg, monkeypatch):
    monkeypatch.setattr(notify, "os", SimpleNamespace(name="nt", environ={}))
    assert notify.make_notifier(cfg) is not None
    monkeypatch.setattr(notify, "os", SimpleNamespace(name="nt", environ={"BTCPERP_NO_TOAST": "1"}))
    assert notify.make_notifier(cfg) is None


def test_windows_toast_passes_text_by_environment_and_does_not_wait(monkeypatch):
    calls = []

    class FakePopen:
        returncode = 0

        def __init__(self, args, **kw):
            calls.append((args, kw))

        def communicate(self, timeout=None):
            calls.append(("waited", timeout))
            return b"", b""

    monkeypatch.setattr(notify, "os", SimpleNamespace(name="nt", environ={"PATH": "x"}))
    monkeypatch.setattr(notify.subprocess, "Popen", FakePopen)
    evil = "'); Remove-Item C:\\ -Recurse; ('"
    n = notify.ToastNotifier()
    assert n("title", evil) is True
    args, kw = calls[0]
    assert len(calls) == 1                                # started, not waited for (review v1.2.0 item 6)
    assert evil not in " ".join(args)                     # user text never becomes script text
    assert kw["env"]["BTCPERP_TOAST_BODY"] == evil and kw["env"]["BTCPERP_TOAST_TITLE"] == "btcperp: title"
    n.wait(5)
    assert calls[-1][0] == "waited" and n.procs == []
    monkeypatch.setattr(notify.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("no powershell")))
    assert notify.windows_toast("t", "b") is None         # failures never raise
    assert n("t", "b") is False


def test_engine_alert_calls_notifier_and_survives_its_failure(world):
    w = world(hkt(2026, 10, 5, 12, 30))
    got = []
    eng = Engine(cfg=w.cfg, calendar=w.calendar, store=w.store, exchange=w.ex, binance=w.bn, telegram=w.tg,
                 clock=w.clock, secrets=w.secrets, sleep=lambda s: None, notifier=lambda k, t: got.append((k, t)))
    eng.alert("trade opened", "LONG 0.1 BTC")
    assert got == [("trade opened", "LONG 0.1 BTC")]
    eng.alert("trade opened", "again", dedupe_key="k1")
    eng.alert("trade opened", "again", dedupe_key="k1")   # deduped: stored and notified once
    assert len(got) == 2

    def boom(k, t):
        raise RuntimeError("toast broken")

    eng.notifier = boom
    eng.alert("x", "y")                                   # must not raise
    assert w.store.count("alerts") == 3


def test_cli_passes_notifier_to_errors(tmp_root):
    make_env(tmp_root)
    cfg_path = tmp_root / "config" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    data["lock"]["wait_seconds"] = 0.3
    cfg_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    clock = FixedClock(hkt(2026, 10, 5, 12, 30))
    ex = MockExchange(clock=clock)
    ex.raise_on["get_instruments"] = RuntimeError("exchange exploded")
    got = []
    f = Factories(exchange=lambda c, s: ex, binance=lambda c: FakeBinance(clock.now().date()),
                  telegram=lambda c, s: FakeTelegram(), notifier=lambda c: (lambda k, t: got.append((k, t))),
                  sleep=lambda s: None)
    assert main(["manage"], paths=Paths(tmp_root), clock=clock, factories=f) == 1
    assert any(k == "ERROR in manage" and "exchange exploded" in t for k, t in got)


# ------------------------------------------------------------------ snapshot (read-only)
def _cli(tmp_root):
    make_env(tmp_root)
    clock = FixedClock(hkt(2026, 10, 5, 12, 30))
    ex = MockExchange(clock=clock)
    ex.proxy_info = type(ex.proxy_info)("0xOWNER", "0xP", None)
    tg = FakeTelegram()
    f = Factories(exchange=lambda c, s: ex, binance=lambda c: FakeBinance(clock.now().date()),
                  telegram=lambda c, s: tg, notifier=lambda c: None, sleep=lambda s: None)
    return Paths(tmp_root), clock, ex, f


def test_snapshot_is_read_only(tmp_root):
    paths, clock, ex, f = _cli(tmp_root)
    ok_leverage(ex)
    ex.pos_size = 0.05
    ex.pos_entry = 100_000.0
    assert main(["snapshot"], paths=paths, clock=clock, factories=f) == 0
    assert not [c for c in ex.calls if c[0] in TRADE_CALLS]
    s = Store(paths.db_file, clock, "x", "x")
    try:
        snap = s.latest("dash_snapshots")
        assert snap and snap["data"]["position"]["size"] == pytest.approx(0.05)
        assert snap["data"]["mark"] > 0 and "sl" in snap["data"]
        for table in ("trades", "state_log", "orders", "intents", "equity_log"):
            assert s.count(table) == 0, table
    finally:
        s.close()


# ------------------------------------------------------------------ dashboard
def test_build_summary_on_empty_database(cfg, tmp_path):
    from perpbot.calendar_events import parse_calendar

    cal = parse_calendar({"calendar_version": "t", "timezone": "America/New_York",
                          "coverage_end": {"FOMC": "2030-12-31", "CPI": "2030-12-31", "NFP": "2030-12-31"},
                          "events": []})
    clock = FixedClock(hkt(2026, 10, 5, 12, 30))
    s = Store(tmp_path / "d.db", clock, cfg.config_version, "t")
    d = build_summary(s, cfg, cal, clock.now())
    json.dumps(d, default=str)
    assert d["state"]["display"] == "flat" and d["position"] is None and d["trades"] == []
    assert d["equity"] is None and d["equity_series"] == [] and d["unread"] == 0


def test_build_summary_after_trading(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    ok_leverage(w.ex)
    w.bn.signal(w.clock.now().date(), "strong_long")
    w.decide()
    assert w.pos() > 0
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    w.ex.set_mark(106_500.0)                              # through TP
    w.at(hkt(2026, 10, 5, 16, 30)).manage()
    d = build_summary(w.store, w.cfg, w.calendar, w.clock.now())
    json.dumps(d, default=str)
    assert d["decision"]["score"]["score"] > 0 and d["decision"]["action"]
    assert len(d["equity_series"]) >= 2 and d["equity"]["equity"] > 0
    assert d["runs"] is not None and d["unread"] >= 1   # the trade alerts
    if d["trades"]:
        t = d["trades"][0]
        assert t["direction"] == 1 and t["net_pnl"] is not None


class _Server:
    def __init__(self, state):
        self.httpd = DashServer(("127.0.0.1", 0), make_handler(state, 1))
        self.port = self.httpd.server_address[1]
        self.httpd.RequestHandlerClass = make_handler(state, self.port)
        self.t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.t.start()

    def req(self, method, path, host=None, token=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", host or f"127.0.0.1:{self.port}")
        if token:
            c.putheader("X-Token", token)
        c.putheader("Content-Length", "0")
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def dash(world, tmp_path):
    w = world(hkt(2026, 10, 5, 12, 30))
    w.engine().alert("test alert", "hello")
    paths = SimpleNamespace(db_file=w.store.path, root=tmp_path)
    state = DashboardState(paths, w.cfg, w.calendar, w.clock)
    srv = _Server(state)
    yield srv, state, w
    srv.close()


def test_dashboard_page_and_summary(dash):
    srv, state, w = dash
    code, body = srv.req("GET", "/")
    assert code == 200 and state.token.encode() in body and b"__TOKEN__" not in body
    code, body = srv.req("GET", "/api/summary")
    d = json.loads(body)
    assert code == 200 and d["unread"] == 1 and d["alerts"][0]["text"].startswith("[btcperp] test alert")
    code, _ = srv.req("GET", "/nope")
    assert code == 404


def test_dashboard_rejects_foreign_host_and_missing_token(dash):
    srv, state, w = dash
    assert srv.req("GET", "/", host="evil.example")[0] == 403           # DNS rebinding
    assert srv.req("GET", "/api/summary", host=f"attacker.com:{srv.port}")[0] == 403
    assert srv.req("POST", "/api/alerts/read")[0] == 403                # no token
    assert srv.req("POST", "/api/alerts/read", token="wrong")[0] == 403
    assert w.store.count("alert_deliveries") == 0


def test_dashboard_mark_read_and_refresh(dash, monkeypatch):
    srv, state, w = dash
    assert srv.req("POST", "/api/alerts/read", token=state.token)[0] == 200
    assert w.store.count("alert_deliveries") == 1
    spawned = []

    class FakePopen:
        def __init__(self, args, **kw):
            spawned.append(args)

        def poll(self):
            return None

    import perpbot.dashboard as dmod

    monkeypatch.setattr(dmod.subprocess, "Popen", FakePopen)
    code, body = srv.req("POST", "/api/refresh", token=state.token)
    assert code == 200 and json.loads(body)["result"] == "started"
    assert spawned and spawned[0][-3:] == ["-m", "perpbot", "snapshot"]
    assert json.loads(srv.req("POST", "/api/refresh", token=state.token)[1])["result"] == "busy"


def test_dashboard_refresh_throttled(dash, monkeypatch):
    srv, state, w = dash
    import perpbot.dashboard as dmod

    class Done:
        def __init__(self, *a, **k):
            pass

        def poll(self):
            return 0

    monkeypatch.setattr(dmod.subprocess, "Popen", Done)
    assert state.refresh() == "started"
    assert state.refresh() == "too soon"


def test_dashboard_serve_when_port_busy(dash, capsys):
    srv, state, w = dash
    paths = SimpleNamespace(db_file=w.store.path, root=Path("."))
    assert serve(paths, w.cfg, w.calendar, w.clock, port=srv.port, open_browser=False) == 0
    assert "already running" in capsys.readouterr().out


def test_config_has_dashboard_and_notifications(cfg):
    assert 1024 < int(cfg.dashboard.port) < 65536
    assert cfg.notifications.windows_toast is True
    assert timedelta(seconds=float(cfg.dashboard.refresh_min_seconds)) >= timedelta(seconds=30)


def test_snapshot_failure_is_quiet(tmp_root):
    paths, clock, ex, f = _cli(tmp_root)
    got = []
    f.notifier = lambda c: (lambda k, t: got.append(k))
    ex.raise_on["get_instruments"] = RuntimeError("exchange down")
    assert main(["snapshot"], paths=paths, clock=clock, factories=f) == 1
    s = Store(paths.db_file, clock, "x", "x")
    try:
        assert s.count("alerts") == 0 and got == []
        assert s.latest("runs", "command='snapshot' AND event='end'")["status"] == "error"
        cfg = load_config(paths.config_file)
        d = build_summary(s, cfg, load_calendar(tmp_root / cfg.gates.calendar_file), clock.now())
        assert "exchange down" in d["snapshot_error"]           # shown in the header ...
        assert not any(e["command"] == "snapshot" for e in d["runs"]["errors"])   # ... not in the error list
    finally:
        s.close()


def test_snapshot_skips_when_bot_busy(tmp_root):
    from perpbot.lockfile import FileLock

    paths, clock, ex, f = _cli(tmp_root)
    paths.ensure()
    got = []
    f.notifier = lambda c: (lambda k, t: got.append(k))
    held = FileLock(paths.lock_file)
    held.acquire(0)
    import perpbot.cli as cli_mod

    old = cli_mod.SNAPSHOT_LOCK_WAIT_SECONDS
    cli_mod.SNAPSHOT_LOCK_WAIT_SECONDS = 0.2
    try:
        assert main(["snapshot"], paths=paths, clock=clock, factories=f) == 4
    finally:
        cli_mod.SNAPSHOT_LOCK_WAIT_SECONDS = old
        held.release()
    assert got == []


def test_suite_never_touches_real_scheduled_tasks(cfg, tmp_path, monkeypatch):
    """The selftest runs on the owner's Windows PC: even there it must not call schtasks or read the real marker."""
    import os

    assert os.environ.get("BTCPERP_NO_SCHTASKS") == "1"
    monkeypatch.setattr(winsched, "os", SimpleNamespace(name="nt", environ=os.environ))
    called = []
    monkeypatch.setattr(winsched, "_run", lambda args: called.append(args) or (0, ""))
    res = winsched.install(cfg, tmp_path, tmp_path / "tasks", dry_run=False)
    assert called == [] and all(r["ok"] is None for r in res)
    assert winsched.registered_root() is None and winsched.remove(cfg)[0]["ok"] is None
