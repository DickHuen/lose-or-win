"""v1.4.0: fixes from the committee review of v1.3.0 (item ids from REVIEW_v1.3.0.md)."""

import json
from datetime import date

import pytest

from perpbot.cli import Factories, main
from perpbot.config import config_from_dict
from perpbot.engine import EngineError
from perpbot.exchange.mock import MockExchange
from perpbot.paths import Paths
from perpbot.records import Records
from perpbot.storage import Store
from perpbot.timeutil import FixedClock

from conftest import FakeBinance, FakeTelegram, hkt, make_env, ok_leverage

D1, D2 = date(2026, 10, 5), date(2026, 10, 6)


def enter_long(w):
    ok_leverage(w.ex)
    w.bn.signal(D1, "strong_long")
    w.at(hkt(2026, 10, 5, 8, 30)).decide()
    assert w.pos() > 0


def add_closed(w, uid, pnl, risk=100.0):
    rec = Records(w.store)
    rec.record_trade("open", uid, 1, {"trade_uid": uid, "direction": 1, "qty": 0.01, "entry_price": 100000.0,
                                      "initial_risk_usd": risk, "equity_at_entry": 10_000.0, "live": True})
    rec.record_trade("close", uid, 1, {"net_pnl": pnl, "exit_reason": "SL" if pnl < 0 else "TP"})


# ---------------------------------------------------------------- S9 live review line
def test_s9_live_review_pauses_below_backtest_line_once_per_new_trade(world):
    w = world(hkt(2026, 10, 5, 12, 30), risk__live_review_expectancy_floor_r=-0.2, risk__live_review_min_trades=5,
              risk__live_review_window_trades=5)
    ok_leverage(w.ex)
    for i, pnl in enumerate((-100, -100, 20, 10, -50)):
        add_closed(w, f"t{i}", pnl)
    w.manage()
    assert "live_review" in w.state()["pause_reasons"] and w.tg.has("live review")
    w.engine().cmd_resume()
    assert not w.state()["paused"]
    w.at(hkt(2026, 10, 5, 16, 30)).manage()                     # no new trade: no re-trigger
    assert not w.state()["paused"]
    add_closed(w, "t9", -100)
    w.at(hkt(2026, 10, 5, 20, 30)).manage()                     # a new trade, still below: pauses again
    assert "live_review" in w.state()["pause_reasons"]


def test_s9_line_not_set_means_no_check(world):
    w = world(hkt(2026, 10, 5, 12, 30), risk__live_review_min_trades=5, risk__live_review_window_trades=5)
    ok_leverage(w.ex)
    assert w.cfg.risk.live_review_expectancy_floor_r is None
    for i in range(5):
        add_closed(w, f"t{i}", -100)
    w.manage()
    assert "live_review" not in w.state()["pause_reasons"]


# ---------------------------------------------------------------- F1 permanent floor, F2 binding
def test_f1_permanent_floor_stops_for_good_and_needs_a_dated_config(world, cfg_dict):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.ex.cash -= 5_600                                           # below 50% of everything ever funded
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    st = w.state()
    assert w.pos() == 0 and "permanent_floor" in st["pause_reasons"] and w.tg.has("KILL SWITCH: permanent floor")
    assert w.engine().cmd_resume(reset_peak=True).startswith("equity_floor, permanent_floor")
    cfg_dict["config_version"] = "9.9.9-test"
    cfg_dict["risk"]["permanent_floor_reset_for"] = "2026-10-05"
    w.cfg = config_from_dict(cfg_dict)
    w.store.config_version = "9.9.9-test"
    msg = w.engine().cmd_resume(reset_peak=True)
    assert "permanent_floor" not in w.state()["pause_reasons"] and msg == "equity_floor still active"
    alert = w.store.latest("alerts", "kind LIKE 'resume%'")
    assert "Cumulative result since the first funding" in alert["text"]
    # single use: a second trigger with the same dated config is not cleared
    assert w.store.count("equity_log", "data LIKE ?", ['%"perm_reset_for": "2026-10-05"%']) == 1


def test_f1_new_config_may_not_lower_the_permanent_floor(world, cfg_dict):
    w = world(hkt(2026, 10, 5, 12, 30))
    ok_leverage(w.ex)
    w.manage()
    cfg_dict["risk"]["permanent_floor_pct_of_cumulative_funded"] = 40
    w.cfg = config_from_dict(cfg_dict)
    with pytest.raises(EngineError, match="lowers"):
        w.at(hkt(2026, 10, 5, 16, 30)).manage()
    assert w.tg.has("lowers the permanent floor")
    with pytest.raises(EngineError):
        w.engine().cmd_resume()


def test_f1_cumulative_funded_follows_deposits_and_is_never_rebased(world):
    from perpbot.exchange.base import Flow

    w = world(hkt(2026, 10, 5, 12, 30))
    ok_leverage(w.ex)
    w.manage()
    assert w.store.latest("equity_log")["data"]["cum_funded"] == pytest.approx(10_000)
    w.ex.flows.append(Flow("dep1", "deposit", 1_000.0, "confirmed", w.ex._now_ms()))
    w.ex.cash += 1_000
    w.at(hkt(2026, 10, 5, 16, 30)).manage()
    assert w.store.latest("equity_log")["data"]["cum_funded"] == pytest.approx(11_000)


# ---------------------------------------------------------------- V4 heartbeat
def _cli(tmp_root, pings):
    make_env(tmp_root)
    with (tmp_root / ".env").open("a", encoding="utf-8") as fh:
        fh.write("HEALTHCHECK_DECIDE_URL=https://hc-ping.com/decide-uuid-1234\n"
                 "HEALTHCHECK_MANAGE_URL=https://hc-ping.com/manage-uuid-5678\n")
    clock = FixedClock(hkt(2026, 10, 5, 12, 30))
    ex = MockExchange(clock=clock)
    ex.proxy_info = type(ex.proxy_info)("0xOWNER", "0xP", None)
    ok_leverage(ex)
    f = Factories(exchange=lambda c, s: ex, binance=lambda c: FakeBinance(clock.now().date()),
                  telegram=lambda c, s: FakeTelegram(), notifier=lambda c: None, sleep=lambda s: None,
                  heartbeat=lambda url, kind: pings.append((url, kind)), registered_root=lambda: None)
    return Paths(tmp_root), clock, ex, f


def test_v4_critical_alert_keeps_failing_until_read_and_hard_stop_while_active(tmp_root):
    pings: list = []
    paths, clock, ex, f = _cli(tmp_root, pings)
    assert main(["manage"], paths=paths, clock=clock, factories=f) == 0
    assert pings[-2:] == [("https://hc-ping.com/manage-uuid-5678", "start"), ("https://hc-ping.com/manage-uuid-5678", "success")]
    s = Store(paths.db_file, clock, "x", "x")
    s.insert("alerts", kind="SL re-place FAILED", dedupe_key=None, sent=0, text="[btcperp] SL re-place FAILED: x")
    s.close()
    assert main(["manage"], paths=paths, clock=clock, factories=f) == 0
    assert pings[-1][1] == "fail"
    s = Store(paths.db_file, clock, "x", "x")
    for a in s.query("SELECT id FROM alerts"):
        s.insert_ignore("alert_deliveries", alert_id=a["id"])     # the owner marks them read in the dashboard
    s.close()
    assert main(["manage"], paths=paths, clock=clock, factories=f) == 0
    assert pings[-1][1] == "success"
    s = Store(paths.db_file, clock, "x", "x")
    Records(s).set_state(add_reason="kill_drawdown", note="test")
    for a in s.query("SELECT id FROM alerts"):
        s.insert_ignore("alert_deliveries", alert_id=a["id"])
    s.close()
    assert main(["manage"], paths=paths, clock=clock, factories=f) == 0
    assert pings[-1][1] == "fail"                                  # every run while the kill switch is active
    assert main(["pause"], paths=paths, clock=clock, factories=f) == 0
    assert pings[-1][1] == "fail" and len(pings) == 8               # pause: no ping at all


def test_v4_decide_uses_its_own_check(tmp_root):
    pings: list = []
    paths, clock, ex, f = _cli(tmp_root, pings)
    clock.set(hkt(2026, 10, 5, 8, 30))
    main(["decide"], paths=paths, clock=clock, factories=f)
    assert {u for u, _ in pings} == {"https://hc-ping.com/decide-uuid-1234"}
    assert pings[0][1] == "start"


# ---------------------------------------------------------------- L1 late decision = on-time decision
def test_l1_late_decision_matches_on_time_even_with_a_later_funding_spike(tmp_path, cfg_dict):
    from conftest import World

    def run(late: bool):
        w = World(tmp_path / ("late" if late else "ontime"), hkt(2026, 10, 5, 8, 30), cfg_dict)
        enter_long(w)
        w.bn.signal(D2, "strong_short")
        if late:
            spike_ms = int(hkt(2026, 10, 6, 18, 0, 0).timestamp() * 1000)          # 10:00 UTC, after the cutoff
            w.bn.fund[spike_ms] = 0.01
            w.at(hkt(2026, 10, 6, 16, 30)).manage()
        else:
            w.at(hkt(2026, 10, 6, 8, 30)).decide()
        return w.store.latest("decisions", "utc_day='2026-10-06' AND score IS NOT NULL")["data"]

    on, late = run(False), run(True)
    assert late["plan"]["late"] is True and on["plan"]["late"] is False
    assert late["score"] == on["score"]
    assert late["plan"]["funding_percentile"] == on["plan"]["funding_percentile"]
    assert late["plan"]["gates_triggered"] == on["plan"]["gates_triggered"]
    assert late["plan"]["close_reason"] == on["plan"]["close_reason"] == "flip"
    assert on["plan"]["enter_direction"] == -1 and late["plan"]["enter_direction"] == 0


# ---------------------------------------------------------------- V16, V17, R3, fees
def test_v16_wrong_folder_message_shows_the_right_kill_path(tmp_root, tmp_path, capsys):
    make_env(tmp_root)
    other = tmp_path / "C_btcperp"
    f = Factories(registered_root=lambda: other)
    assert main(["kill"], paths=Paths(tmp_root), clock=FixedClock(hkt(2026, 10, 5, 12, 0)), factories=f) == 3
    assert "Kill_Close_Position.bat" in capsys.readouterr().err


def test_v17_monthly_report_all_vs_complete_months_and_r3_basis(world, tmp_path):
    from perpbot.reports import Reporter

    w = world(hkt(2026, 10, 1, 8, 30))
    ok_leverage(w.ex)
    for day in (1, 2, 6, 7):
        w.bn.signal(date(2026, 10, day), "flat")
        w.at(hkt(2026, 10, day, 8, 30)).decide()
    add_closed(w, "x1", 50)
    w.at(hkt(2026, 11, 2, 12, 0))
    paths = Paths(tmp_path / "root")
    paths.ensure()
    Reporter(w.engine(), paths).monthly("2026-10")
    data = json.loads((paths.reports_dir / "monthly" / "monthly_2026-10.json").read_text())
    c = data["cumulative_all_vs_complete_months"]
    assert c["incomplete_months"] == ["2026-10"] and c["all_months"]["trades"] == 1
    assert c["complete_months_only"]["trades"] == 0
    assert data["basis_bps"]["samples"] >= 1 and data["basis_bps"]["abs_p99"] is not None


def test_smoketest_records_real_fee_and_basis(world, tmp_path):
    from perpbot.smoketest import run_smoketest

    w = world(hkt(2026, 10, 5, 3, 0))
    w.ex.proxy_info = type(w.ex.proxy_info)("0xOWNER", "0xP", None)
    paths = Paths(tmp_path / "root")
    paths.ensure()
    ok, res = run_smoketest(w.engine(), paths, allow_trading=False)
    by = {r["step"]: r for r in res}
    assert by["fees"]["ok"] and by["fees"]["detail"]["taker_fee_rate"] == w.ex.fee_rate
    assert by["basis"]["ok"] and by["basis"]["detail"]["basis_bps"] is not None


# ---------------------------------------------------------------- U1, U2, U3 upgrade safety
def _install_mod(root):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("btc_install_v130", Path(__file__).resolve().parent.parent / "install.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ROOT = root
    return mod


def _old_install(root):
    (root / "perpbot").mkdir(parents=True)
    (root / "perpbot" / "old.py").write_text("OLD = 1\n")
    (root / "config").mkdir()
    (root / "config" / "config.yaml").write_text("old: 1\n")
    (root / "VERSION").write_text("1.3.0\n")
    (root / "requirements.txt").write_text("\n")
    (root / ".env").write_text("SECRET=keep\n")
    (root / "data").mkdir()
    (root / "data" / "btcperp.sqlite3").write_bytes(b"db")


def _zip(path, version, extra=None):
    import zipfile

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("btcperp/VERSION", version + "\n")
        z.writestr("btcperp/MANIFEST.txt", "VERSION\nperpbot/new.py\n")
        z.writestr("btcperp/install.py", "# new\n")
        z.writestr("btcperp/perpbot/new.py", "NEW = 1\n")
        z.writestr("btcperp/NEWDOC.md", "new\n")
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return path


def test_u1_unreadable_task_scheduler_changes_nothing(tmp_path, monkeypatch):
    from types import SimpleNamespace

    root = tmp_path / "btcperp"
    _old_install(root)
    mod = _install_mod(root)
    monkeypatch.setattr(mod, "os", SimpleNamespace(name="nt", environ={}, replace=__import__("os").replace))
    monkeypatch.setattr(mod, "_schtasks", lambda *a: (1, "ERROR: access denied"))
    with pytest.raises(mod.TaskQueryError):
        mod.existing_tasks()
    monkeypatch.setattr("sys.argv", ["install.py", "--from-zip", str(_zip(tmp_path / "n.zip", "1.4.0"))])
    assert mod.main() == 1
    assert (root / "VERSION").read_text() == "1.3.0\n" and not (root / "perpbot" / "new.py").exists()


def test_u3_older_or_equal_zip_is_refused(tmp_path, monkeypatch, capsys):
    root = tmp_path / "btcperp"
    _old_install(root)
    mod = _install_mod(root)
    for v in ("1.2.0", "1.3.0"):
        monkeypatch.setattr("sys.argv", ["install.py", "--from-zip", str(_zip(tmp_path / f"z{v}.zip", v))])
        assert mod.main() == 1
    out = capsys.readouterr().out
    assert "not newer" in out and "SHA-256" in out
    assert (root / "VERSION").read_text() == "1.3.0\n"
    assert mod.version_tuple("1.10.0") > mod.version_tuple("1.9.9")


def test_u2_failed_upgrade_restores_old_version_and_tasks(tmp_path, monkeypatch, capsys):
    import subprocess as sp

    root = tmp_path / "btcperp"
    _old_install(root)
    mod = _install_mod(root)
    calls = {"enable": [], "pip": 0}
    monkeypatch.setattr(mod, "existing_tasks", lambda: ["\\btcperp\\decide_0830"])
    monkeypatch.setattr(mod, "set_tasks", lambda names, enable: calls["enable"].append(enable))
    monkeypatch.setattr(mod, "_schtasks", lambda *a: (0, ""))

    def fake_check_call(args, **kw):
        calls["pip"] += 1
        return 0

    monkeypatch.setattr(mod.subprocess, "check_call", fake_check_call)
    for failure in ("pip", "tests"):
        def steps(members, failure=failure):
            mod.extract(members)                                   # the new files are written ...
            if failure == "pip":
                raise sp.CalledProcessError(1, ["pip"])            # ... then pip fails
            return root / "venv" / "python", 1                     # ... or the tests fail

        monkeypatch.setattr(mod, "_install_steps", steps)
        newdir = {"btcperp/offline_sign/offline_sign.html": "<p>new</p>"}
        monkeypatch.setattr("sys.argv", ["install.py", "--from-zip",
                                         str(_zip(tmp_path / f"{failure}.zip", "1.4.0", extra=newdir))])
        assert mod.main() == 1
        out = capsys.readouterr().out
        assert "restored" in out and "runs as before" in out
        assert not (root / "offline_sign").exists()                  # a folder only the new version had
        assert (root / "VERSION").read_text() == "1.3.0\n"
        assert (root / "perpbot" / "old.py").exists() and not (root / "perpbot" / "new.py").exists()
        assert not (root / "NEWDOC.md").exists() and (root / ".env").read_text() == "SECRET=keep\n"
        assert (root / "data" / "btcperp.sqlite3").read_bytes() == b"db"
        assert calls["enable"][-1] is True                          # tasks re-enabled
    assert len(list((root / "data").glob("upgrade_backup_1.3.0_*"))) >= 1


def test_u2_failed_restore_keeps_tasks_disabled_and_shows_position(tmp_path, monkeypatch, capsys):
    import sqlite3

    root = tmp_path / "btcperp"
    _old_install(root)
    (root / "data" / "btcperp.sqlite3").unlink()
    con = sqlite3.connect(str(root / "data" / "btcperp.sqlite3"))
    con.execute("CREATE TABLE dash_snapshots (id INTEGER PRIMARY KEY, ts_hkt TEXT, data TEXT)")
    con.execute("CREATE TABLE manage_log (id INTEGER PRIMARY KEY, ts_hkt TEXT, data TEXT)")
    con.execute("INSERT INTO dash_snapshots (ts_hkt, data) VALUES (?, ?)",
                ("2026-10-05 12:30:00 HKT", json.dumps({"position": {"size": 0.05, "entry_price": 100000}, "sl": [97000],
                                                        "tp": [106000], "mark": 99000})))
    con.commit()
    con.close()
    mod = _install_mod(root)
    enabled = []
    monkeypatch.setattr(mod, "existing_tasks", lambda: ["\\btcperp\\decide_0830"])
    monkeypatch.setattr(mod, "set_tasks", lambda names, enable: enabled.append(enable))
    monkeypatch.setattr(mod, "_schtasks", lambda *a: (0, ""))
    monkeypatch.setattr(mod, "_install_steps", lambda members: (root / "py", 1))

    def broken_restore(src, members):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "restore_install", broken_restore)
    monkeypatch.setattr("sys.argv", ["install.py", "--from-zip", str(_zip(tmp_path / "x.zip", "1.4.0"))])
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "RESTORE FAILED" in out and "STAY DISABLED" in out and "size 0.05" in out and "stop-loss [97000]" in out
    assert enabled == [False]                                        # disabled, never re-enabled


def test_u2_restore_test_mode_installs_then_puts_the_old_version_back(tmp_path, monkeypatch, capsys):
    """Go-live check B3: Upgrade.bat TEST-RESTORE proves the automatic restore on the real PC."""
    import subprocess as sp

    root = tmp_path / "btcperp"
    _old_install(root)
    mod = _install_mod(root)
    enabled, pip_calls = [], []
    monkeypatch.setattr(mod, "existing_tasks", lambda: ["\\btcperp\\decide_0830"])
    monkeypatch.setattr(mod, "set_tasks", lambda names, enable: enabled.append(enable))
    monkeypatch.setattr(mod, "_schtasks", lambda *a: (0, ""))
    seen = {}

    def steps(members):
        mod.extract(members)
        seen["new_installed"] = (root / "perpbot" / "new.py").exists()
        return root / "venv" / "python", 0                          # the new version passes its tests

    def pip_offline(args, **kw):                                      # no internet: only --no-index works
        pip_calls.append(list(args))
        if "--no-index" not in args:
            raise sp.CalledProcessError(1, args)
        return 0

    monkeypatch.setattr(mod, "_install_steps", steps)
    monkeypatch.setattr(mod.subprocess, "check_call", pip_offline)
    monkeypatch.setattr("sys.argv", ["install.py", "--from-zip", str(_zip(tmp_path / "same.zip", "1.3.0")),
                                     "--test-restore"])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert seen["new_installed"] is True and "RESTORE TEST PASSED" in out
    assert (root / "VERSION").read_text() == "1.3.0\n" and not (root / "perpbot" / "new.py").exists()
    assert (root / ".env").read_text() == "SECRET=keep\n" and enabled == [False, True]
    assert any("--no-index" in c for c in pip_calls)
    # an older zip, or no --from-zip, is refused before anything changes
    monkeypatch.setattr("sys.argv", ["install.py", "--from-zip", str(_zip(tmp_path / "old.zip", "1.2.0")),
                                     "--test-restore"])
    assert mod.main() == 1
    monkeypatch.setattr("sys.argv", ["install.py", "--test-restore"])
    assert mod.main() == 1
    assert "needs --from-zip" in capsys.readouterr().out
