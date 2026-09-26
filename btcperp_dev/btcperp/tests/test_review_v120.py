"""v1.3.0: fixes from the committee review of v1.2.0 (item numbers from REVIEW_v1.2.0.md)."""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from perpbot.cli import EXIT_CONFIG, EXIT_CONFIRM, Factories, main, smoketest_gate
from perpbot.engine import Engine, NeedsConfirmation
from perpbot.exchange.mock import MockExchange
from perpbot.paths import Paths
from perpbot.records import Records
from perpbot.risk import losing_streak
from perpbot.storage import Store
from perpbot.timeutil import FixedClock

from conftest import FakeBinance, FakeTelegram, hkt, make_env, ok_leverage

D1, D2 = date(2026, 10, 5), date(2026, 10, 6)


def active(w, kind):
    return [o for o in w.ex.orders.values() if o.tpsl_kind == kind and o.status in ("armed", "untriggered")]


def enter_long(w):
    ok_leverage(w.ex)
    w.bn.signal(D1, "strong_long")
    w.at(hkt(2026, 10, 5, 8, 30)).decide()
    assert w.pos() > 0
    return Records(w.store).open_trade()


# ---------------------------------------------------------------- item 2: late decision, close rules only
def test_item2_late_decide_closes_on_reverse_signal_but_never_enters(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.bn.signal(D2, "strong_short")
    n_fok = len(w.fok_calls())
    w.at(hkt(2026, 10, 6, 10, 0)).decide()               # after the 08:30-09:30 window, no intent today
    assert w.pos() == 0                                    # the flip's close half ran
    assert len(w.fok_calls()) == n_fok                     # no short opened
    intent = Records(w.store).intent("2026-10-06")
    assert intent["late"] is True and intent["action"] == "close" and intent["close_reason"] == "flip"
    assert any("late decision" in b for b in intent["entry_blocked"])
    assert w.tg.has("late decision") and w.tg.has("missed")


def test_item2_late_manage_evaluates_close_rules_when_decide_never_ran(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.bn.signal(D2, "strong_short")
    n_fok = len(w.fok_calls())
    w.at(hkt(2026, 10, 6, 12, 30)).manage()               # both decides missed (PC asleep); first run is manage
    assert w.pos() == 0 and len(w.fok_calls()) == n_fok
    assert Records(w.store).closed_trades()[0]["exit_reason"] == "flip"


def test_item2_late_decision_uses_data_closed_at_midnight_and_window_events(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.bn.signal(D2, "weak_short")                          # weak opposite: hold
    w.at(hkt(2026, 10, 6, 16, 30)).manage()
    assert w.pos() > 0
    dec = w.store.latest("decisions", "utc_day = '2026-10-06' AND score IS NOT NULL")
    assert dec["data"]["plan"]["late"] is True
    assert dec["data"]["score"]["candle_open_ms"] == int(hkt(2026, 10, 5, 8, 0, 0).timestamp() * 1000)


def test_item2_late_manage_when_flat_does_nothing(world):
    w = world(hkt(2026, 10, 6, 12, 30))
    ok_leverage(w.ex)
    w.manage()
    assert w.store.count("decisions") == 0 and w.pos() == 0


# ---------------------------------------------------------------- item 3: D11 tie rule
def test_item3_tie_neither_ends_nor_extends_streak():
    eq = 10_000.0
    newest_first = [{"net_pnl": -300, "equity_at_entry": eq}, {"net_pnl": -5, "equity_at_entry": eq},
                    {"net_pnl": -300, "equity_at_entry": eq}, {"net_pnl": 200, "equity_at_entry": eq}]
    st = losing_streak(newest_first, 8, tie_pct=0.1)
    assert st.losing_trades == 2 and st.streak_loss == pytest.approx(600)
    assert losing_streak(newest_first, 8, tie_pct=0.0).losing_trades == 3          # old rule
    tie_win = [{"net_pnl": -300, "equity_at_entry": eq}, {"net_pnl": 9, "equity_at_entry": eq},
               {"net_pnl": -300, "equity_at_entry": eq}]
    assert losing_streak(tie_win, 8, tie_pct=0.1).losing_trades == 2                # a tiny win does not end it


def test_item3_config_tie_band(cfg):
    assert cfg.risk.losing_streak_tie_pct == 0.1


# ---------------------------------------------------------------- item 6: notifications after the run
def test_item6_notifications_wait_until_after_sl_is_placed(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    sl = active(w, "sl")[0]
    sl.status = "cancelled"                                # SL disappeared on the exchange
    notified: list[str] = []
    placed_at_notify_count: list[int] = []
    orig = w.ex.place_position_tpsl

    def spy(**kw):
        placed_at_notify_count.append(len(notified))
        return orig(**kw)

    w.ex.place_position_tpsl = spy
    eng = Engine(cfg=w.cfg, calendar=w.calendar, store=w.store, exchange=w.ex, binance=w.bn, telegram=w.tg,
                 clock=w.clock, secrets=w.secrets, sleep=lambda s: None,
                 notifier=lambda k, t: notified.append(k), defer_notifications=True)
    w.at(hkt(2026, 10, 5, 12, 30))
    eng.cmd_manage()
    assert placed_at_notify_count == [0]                   # SL re-placed before any notification was sent
    assert notified == [] and len(active(w, "sl")) == 1
    assert eng.flush_notifications() >= 2
    assert "SL missing" in notified and "SL re-placed" in notified


def test_item6_toast_flood_is_capped(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    notified: list[str] = []
    eng = Engine(cfg=w.cfg, calendar=w.calendar, store=w.store, exchange=w.ex, binance=w.bn, telegram=w.tg,
                 clock=w.clock, secrets=w.secrets, sleep=lambda s: None,
                 notifier=lambda k, t: notified.append(k), defer_notifications=True)
    for i in range(8):
        eng.alert(f"a{i}", "x")
    assert eng.flush_notifications() == 8
    assert notified == ["a0", "a1", "a2", "a3", "a4", "more alerts"]
    assert w.store.count("alerts") == 8                    # every alert is still stored (dashboard)


# ---------------------------------------------------------------- item 7: unpause / resume / floor baseline
def _drawdown_kill(w):
    enter_long(w)
    w.ex.cash -= 1_800
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert "kill_drawdown" in w.state()["pause_reasons"]


def test_item7_unpause_only_removes_manual_pause(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    _drawdown_kill(w)
    w.engine().cmd_pause()
    peak_before = w.store.latest("equity_log")["peak"]
    msg = w.engine().cmd_unpause()
    reasons = w.state()["pause_reasons"]
    assert reasons == ["kill_drawdown"] and "STILL PAUSED" in msg
    assert w.store.latest("equity_log")["peak"] == peak_before


def test_item7_resume_needs_reset_peak_for_kill_switch(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    _drawdown_kill(w)
    with pytest.raises(NeedsConfirmation):
        w.engine().cmd_resume()
    assert "kill_drawdown" in w.state()["pause_reasons"]
    assert w.engine().cmd_resume(reset_peak=True) == "resumed"
    assert not w.state()["paused"] and w.store.latest("equity_log")["data"]["peak_reset"] is True


def test_item7_resume_from_manual_pause_keeps_peak_and_streak(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.engine().cmd_pause()
    n_eq = w.store.count("equity_log")
    resume_before = Records(w.store).last_resume_ms()
    assert w.engine().cmd_resume() == "resumed"
    assert not w.state()["paused"]
    assert w.store.count("equity_log") == n_eq                  # no peak reset row
    assert Records(w.store).last_resume_ms() == resume_before    # losing-streak count not restarted


def test_item7_floor_reset_baseline_must_be_positive(cfg_dict):
    from perpbot.config import ConfigError, config_from_dict

    cfg_dict["risk"]["equity_floor_reset_baseline_usd"] = -5
    with pytest.raises(ConfigError):
        config_from_dict(cfg_dict)


# ---------------------------------------------------------------- item 9: calendar fail-safe
def test_item9_expired_calendar_blocks_new_positions_but_not_closes(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    from perpbot.calendar_events import parse_calendar

    w.calendar = parse_calendar({"calendar_version": "t", "timezone": "America/New_York",
                                 "coverage_end": {"FOMC": "2030-12-31", "CPI": "2026-10-05", "NFP": "2030-12-31"},
                                 "events": []})
    w.bn.signal(D2, "strong_short")
    n_fok = len(w.fok_calls())
    w.at(hkt(2026, 10, 6, 8, 30)).decide()
    assert w.pos() == 0 and len(w.fok_calls()) == n_fok        # flip -> close only
    dec = w.store.latest("decisions", "score IS NOT NULL")
    assert any("calendar coverage ended for CPI" in b for b in dec["data"]["plan"]["entry_blocked"])
    assert w.tg.has("calendar expired")


def test_item9_expired_types(cfg):
    from perpbot.calendar_events import parse_calendar

    cal = parse_calendar({"calendar_version": "t", "timezone": "America/New_York",
                          "coverage_end": {"FOMC": "2027-12-31", "CPI": "2026-12-31", "NFP": "2026-12-31"}, "events": []})
    assert cal.expired_types(date(2026, 12, 31)) == []
    assert cal.expired_types(date(2027, 1, 8)) == ["CPI", "NFP"]


# ---------------------------------------------------------------- item 10: one FOK attempt, smoketest records status
def test_item10_entry_attempts_default_one(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    assert w.cfg.exits.entry_attempts == 1
    ok_leverage(w.ex)
    w.bn.signal(D1, "strong_long")
    w.ex.fok_outcomes.extend([False, False])
    w.decide()
    w.at(hkt(2026, 10, 5, 8, 50)).decide()
    assert w.pos() == 0 and len(w.fok_calls()) == 1


def test_item10_smoketest_records_unfilled_fok_status(world, tmp_path):
    from perpbot.smoketest import run_smoketest

    w = world(hkt(2026, 10, 5, 3, 0))
    w.ex.proxy_info = type(w.ex.proxy_info)("0xOWNER", "0xP", None)
    paths = Paths(tmp_path / "root")
    paths.ensure()
    ok, res = run_smoketest(w.engine(), paths)
    step = {r["step"]: r for r in res}["fok_unfilled_status"]
    assert step["ok"] is True and step["detail"]["raw_status"] == "fok_unfilled"
    data = json.loads(next(paths.smoketest_dir.glob("smoketest_*.json")).read_text())
    assert data["allow_trading"] is True and data["code_version"] and "proxy_address" in data


# ---------------------------------------------------------------- item 15: clock skew
def test_item15_clock_skew_blocks_new_positions(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    ok_leverage(w.ex)
    w.bn.signal(D1, "strong_long")
    w.ex.server_skew_ms = 60_000
    w.decide()
    assert w.pos() == 0 and w.fok_calls() == []
    assert w.tg.has("clock skew")
    dec = w.store.latest("decisions", "score IS NOT NULL")
    assert any("differs from the exchange by +60.0s" in b for b in dec["data"]["plan"]["entry_blocked"])


def test_item15_unreadable_server_time_blocks(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    ok_leverage(w.ex)
    w.bn.signal(D1, "strong_long")
    w.ex.server_time_fails = True
    w.decide()
    assert w.pos() == 0


def test_item15_small_skew_is_fine(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    ok_leverage(w.ex)
    w.bn.signal(D1, "strong_long")
    w.ex.server_skew_ms = -5_000
    w.decide()
    assert w.pos() > 0


# ---------------------------------------------------------------- CLI: items 5, 7, 16, 21
def _cli(tmp_root, **fac):
    make_env(tmp_root)
    clock = FixedClock(hkt(2026, 10, 5, 12, 30))
    ex = MockExchange(clock=clock)
    ex.proxy_info = type(ex.proxy_info)("0xOWNER", "0xP", None)
    ok_leverage(ex)
    tg = FakeTelegram()
    f = Factories(exchange=lambda c, s: ex, binance=lambda c: FakeBinance(clock.now().date()),
                  telegram=lambda c, s: tg, notifier=lambda c: None, sleep=lambda s: None, **fac)
    return Paths(tmp_root), clock, ex, f


def test_item5_heartbeat_after_decide_and_manage_only(tmp_root):
    pings = []
    paths, clock, ex, f = _cli(tmp_root, heartbeat=lambda url, ok: pings.append((url, ok)))
    with (tmp_root / ".env").open("a", encoding="utf-8") as fh:
        fh.write("HEALTHCHECK_PING_URL=https://hc-ping.com/11111111-2222-3333-4444-555555555555\n")
    assert main(["manage"], paths=paths, clock=clock, factories=f) == 0
    assert main(["pause"], paths=paths, clock=clock, factories=f) == 0
    ex.raise_on["get_instruments"] = RuntimeError("down")
    assert main(["manage"], paths=paths, clock=clock, factories=f) == 1
    assert [ok for _, ok in pings] == [True, False]         # pause: no ping; failed manage: /fail
    log_text = "".join(p.read_text() for p in (tmp_root / "logs").glob("*.log"))
    assert "11111111-2222-3333-4444-555555555555" not in log_text


def test_item5_heartbeat_url_must_be_https(tmp_root):
    paths, clock, ex, f = _cli(tmp_root)
    with (tmp_root / ".env").open("a", encoding="utf-8") as fh:
        fh.write("HEALTHCHECK_PING_URL=http://example.com/x\n")
    assert main(["pause"], paths=paths, clock=clock, factories=f) == EXIT_CONFIG


def test_heartbeat_never_raises():
    from perpbot.cli import send_heartbeat

    assert send_heartbeat("", True) is False
    assert send_heartbeat("https://127.0.0.1:1/nothing", True, timeout=0.5) is False


def test_item7_cli_resume_exit_code_needs_confirmation(tmp_root, capsys):
    paths, clock, ex, f = _cli(tmp_root)
    s = Store(paths.db_file, clock, "x", "x")
    Records(s).set_state(add_reason="kill_drawdown", note="test")
    s.close()
    assert main(["resume"], paths=paths, clock=clock, factories=f) == EXIT_CONFIRM
    assert "RESET-PEAK" in capsys.readouterr().out
    assert main(["reasons"], paths=paths, clock=clock, factories=f) == 0
    assert "kill_drawdown" in capsys.readouterr().out
    assert main(["resume", "--reset-peak"], paths=paths, clock=clock, factories=f) == 0
    assert main(["unpause"], paths=paths, clock=clock, factories=f) == 0


def test_item16_wrong_folder_is_refused(tmp_root, tmp_path, capsys):
    other = tmp_path / "C_btcperp"
    paths, clock, ex, f = _cli(tmp_root, registered_root=lambda: other)
    assert main(["kill"], paths=paths, clock=clock, factories=f) == EXIT_CONFIG
    assert "WRONG FOLDER" in capsys.readouterr().err
    assert not [c for c in ex.calls if c[0] == "place_order"]
    f.registered_root = lambda: tmp_root
    assert main(["pause"], paths=paths, clock=clock, factories=f) == 0


def test_item21_schedule_install_needs_passing_full_smoketest(tmp_root, capsys):
    from perpbot import code_version
    from perpbot.envsecrets import load_secrets

    paths, clock, ex, f = _cli(tmp_root, registered_root=lambda: None)
    assert main(["schedule", "install"], paths=paths, clock=clock, factories=f) == EXIT_CONFIG
    assert "no full smoketest" in capsys.readouterr().out
    sec = load_secrets(paths.env_file)
    paths.ensure()

    def write(name, **data):
        (paths.smoketest_dir / name).write_text(json.dumps(data), encoding="utf-8")

    write("smoketest_20261001000000.json", ok=True, allow_trading=True, code_version=code_version(),
          proxy_address=sec.proxy_address)
    write("smoketest_20261002000000.json", ok=True, allow_trading=False, code_version=code_version(),
          proxy_address=sec.proxy_address)                    # a later read-only run does not count
    assert smoketest_gate(paths, sec)[0] is True
    assert main(["schedule", "install"], paths=paths, clock=clock, factories=f) == 0
    write("smoketest_20261003000000.json", ok=True, allow_trading=True, code_version="0.9.9",
          proxy_address=sec.proxy_address)
    assert smoketest_gate(paths, sec)[0] is False
    write("smoketest_20261004000000.json", ok=True, allow_trading=True, code_version=code_version(),
          proxy_address="0x0000000000000000000000000000000000000001")
    assert "different proxy key" in smoketest_gate(paths, sec)[1]
    write("smoketest_20261005000000.json", ok=False, allow_trading=True, code_version=code_version(),
          proxy_address=sec.proxy_address)
    assert "did not pass" in smoketest_gate(paths, sec)[1]


def test_item8_schedule_upgrade_only_for_tasks_already_here(tmp_root, tmp_path):
    paths, clock, ex, f = _cli(tmp_root, registered_root=lambda: None)
    assert main(["schedule", "install", "--upgrade"], paths=paths, clock=clock, factories=f) == EXIT_CONFIG
    f.registered_root = lambda: tmp_root
    assert main(["schedule", "install", "--upgrade"], paths=paths, clock=clock, factories=f) == 0


def test_item20_dashboard_csp_header(world, tmp_path):
    import http.client
    import threading
    from types import SimpleNamespace

    from perpbot.dashboard import DashboardState, DashServer, make_handler

    w = world(hkt(2026, 10, 5, 12, 30))
    state = DashboardState(SimpleNamespace(db_file=w.store.path, root=tmp_path), w.cfg, w.calendar, w.clock)
    httpd = DashServer(("127.0.0.1", 0), make_handler(state, 1))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(state, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", "/")
        r = c.getresponse()
        r.read()
        csp = r.getheader("Content-Security-Policy") or ""
        assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_expired_proxy_hint_mentions_env_not_grok(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.secrets.proxy_expires_at = w.clock.now() + timedelta(days=2)
    w.ex.proxy_info = type(w.ex.proxy_info)("0xOWNER", "0xPROXY", None)
    w.engine().check_key_expiry()
    assert w.tg.has("put it in .env") and not w.tg.has("grok")


def test_windows_bat_files_exist_for_new_commands():
    root = Path(__file__).resolve().parent.parent / "windows"
    for name in ("Unpause.bat", "Upgrade.bat", "Resume.bat", "Backtest.bat", "Proxy_Key.bat"):
        assert (root / name).exists(), name


# ---------------------------------------------------------------- item 17: missed decision days
def test_item17_monthly_report_marks_incomplete_month(world, tmp_path):
    from perpbot.reports import Reporter

    w = world(hkt(2026, 10, 1, 8, 30))
    ok_leverage(w.ex)
    for day in (1, 2, 6, 7):                               # decided on 4 days; 3-5 missed
        w.bn.signal(date(2026, 10, day), "flat")
        w.at(hkt(2026, 10, day, 8, 30)).decide()
    w.at(hkt(2026, 10, 8, 12, 0))
    paths = Paths(tmp_path / "root")
    paths.ensure()
    rep = Reporter(w.engine(), paths)
    rep.monthly("2026-10")
    data = json.loads((paths.reports_dir / "monthly" / "monthly_2026-10.json").read_text())
    dd = data["decision_days"]
    assert dd["missed_days"] == ["2026-10-03", "2026-10-04", "2026-10-05"] and dd["incomplete_month"] is True
    assert "INCOMPLETE MONTH" in (paths.reports_dir / "monthly" / "monthly_2026-10.md").read_text()
    assert w.tg.has("incomplete month")


# ---------------------------------------------------------------- item 8: upgrade zip handling
def _install_module(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("btc_install", Path(__file__).resolve().parent.parent / "install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = tmp_path
    return mod


def _zip(path, entries):
    import zipfile

    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


def test_item8_upgrade_zip_is_validated_and_extracted(tmp_path):
    mod = _install_module(tmp_path / "root")
    (tmp_path / "root").mkdir()
    good = {"btcperp/VERSION": "9.9.9\n", "btcperp/MANIFEST.txt": "VERSION\n", "btcperp/install.py": "#",
            "btcperp/perpbot/x.py": "X = 1\n"}
    version, members = mod.read_zip(_zip(tmp_path / "ok.zip", good))
    assert version == "9.9.9"
    (tmp_path / "root" / ".env").write_text("SECRET=1\n")
    mod.extract(members)
    assert (tmp_path / "root" / "perpbot" / "x.py").read_text() == "X = 1\n"
    assert (tmp_path / "root" / ".env").read_text() == "SECRET=1\n"             # untouched
    for bad in ({**good, "btcperp/data/btcperp.sqlite3": "x"}, {**good, "btcperp/.env": "K=1"},
                {k: v for k, v in good.items() if k != "btcperp/VERSION"}, {**good, "other/evil.py": "x"},
                {**good, "btcperp/../evil.py": "x"}):
        with pytest.raises(SystemExit):
            mod.read_zip(_zip(tmp_path / "bad.zip", bad))


def test_item8_existing_tasks_parsing(monkeypatch, tmp_path):
    mod = _install_module(tmp_path)
    monkeypatch.setattr(mod, "os", type("O", (), {"name": "nt", "environ": {}})())
    out = ('"\\btcperp\\decide_0830","2026/10/06 8:30:00","Ready"\n'
           '"\\Microsoft\\Other","N/A","Ready"\n"\\btcperp\\dashboard","N/A","Running"\n')
    monkeypatch.setattr(mod, "_schtasks", lambda *a: (0, out))
    assert mod.existing_tasks() == ["\\btcperp\\decide_0830", "\\btcperp\\dashboard"]
