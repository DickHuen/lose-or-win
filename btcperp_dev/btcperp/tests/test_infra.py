"""Storage, lock, config, logging redaction, schedule audit, shadow, CLI commands."""

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from perpbot.cli import Factories, main
from perpbot.config import ConfigError, config_from_dict
from perpbot.exchange.mock import MockExchange
from perpbot.lockfile import FileLock, LockTimeout
from perpbot.logging_setup import setup_logging
from perpbot.paths import Paths
from perpbot.schedule_audit import audit, nearest_slot
from perpbot.shadow import SimState, SimTrade, _walk
from perpbot.storage import Store
from perpbot.timeutil import HOUR_MS, UTC, FixedClock, to_ms

from conftest import FakeBinance, FakeTelegram, hkt, make_env


def test_storage_is_append_only(tmp_path):
    s = Store(tmp_path / "x.db", FixedClock(datetime(2026, 10, 5, tzinfo=UTC)), "1.0.0", "1.0.0")
    s.insert("runs", command="decide", event="start", data={"a": 1})
    for sql in ("UPDATE runs SET command='x'", "DELETE FROM runs"):
        with pytest.raises(sqlite3.DatabaseError):
            s.conn.execute(sql)
    row = s.latest("runs")
    assert row["config_version"] == "1.0.0" and row["code_version"] == "1.0.0"
    assert row["ts_utc"].endswith("Z") and row["ts_hkt"].endswith("HKT")
    assert s.insert_ignore("fills", trade_id=1, data={}) and not s.insert_ignore("fills", trade_id=1, data={})


def test_file_lock_excludes_second_holder(tmp_path):
    a = FileLock(tmp_path / "l.lock")
    a.acquire(0)
    b = FileLock(tmp_path / "l.lock")
    with pytest.raises(LockTimeout):
        b.acquire(0.3, poll=0.1)
    a.release()
    b.acquire(0.3, poll=0.1)
    b.release()


def test_config_validation(cfg_dict):
    bad = dict(cfg_dict)
    bad["risk"] = dict(cfg_dict["risk"], leverage=0)
    with pytest.raises(ConfigError):
        config_from_dict(bad)
    missing = {k: v for k, v in cfg_dict.items() if k != "exits"}
    with pytest.raises(ConfigError):
        config_from_dict(missing)
    c = config_from_dict(cfg_dict)
    assert c.risk.leverage == 3 and c.config_version == cfg_dict["config_version"]


def test_strategy_code_has_no_hardcoded_strategy_numbers():
    """Strategy/risk code may only contain structural constants; every strategy number comes from config."""
    import io
    import tokenize

    root = Path(__file__).resolve().parent.parent / "perpbot"
    allowed = {0.0, 1.0, 2.0, 4.0, 100.0, 3_600_000.0, 1e-9, 1e-12}
    for name in ("strategy.py", "risk.py"):
        toks = tokenize.generate_tokens(io.StringIO((root / name).read_text()).readline)
        nums = {float(t.string.replace("_", "")) for t in toks if t.type == tokenize.NUMBER}
        assert nums <= allowed, f"{name}: unexpected numeric literals {sorted(nums - allowed)}"


def test_logging_redacts_secrets(tmp_path):
    clock = FixedClock(datetime(2026, 10, 5, tzinfo=UTC))
    secret = "0x" + "ab" * 32
    path, _ = setup_logging(tmp_path / "logs", clock, "INFO", ["super-secret-value", secret], stdout=False)
    log = logging.getLogger("t")
    log.info("key=%s secret=%s token=%s", secret, "super-secret-value", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij")
    try:
        raise ValueError(f"boom {secret}")
    except ValueError:
        log.exception("failure")
    for h in logging.getLogger().handlers:
        h.flush()
    text = path.read_text()
    assert "super-secret-value" not in text and "ab" * 32 not in text and "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in text
    assert "***" in text


def test_schedule_audit_and_lateness(cfg, tmp_path):
    clock = FixedClock(hkt(2026, 10, 5, 8, 33))
    s = Store(tmp_path / "a.db", clock, "1", "1")
    slot, late = nearest_slot(cfg, "decide", clock.now())
    assert slot == hkt(2026, 10, 5, 8, 30, 0) and late == pytest.approx(3 + 5 / 60, rel=0.01)
    s.insert("runs", command="decide", event="start")
    clock.set(hkt(2026, 10, 5, 9, 30))
    res = audit(s, cfg, hkt(2026, 10, 5, 8, 0, 0), hkt(2026, 10, 5, 9, 30, 0))
    by = {(a["command"], a["slot_hkt"]): a["status"] for a in res}
    assert by[("decide", "2026-10-05 08:30")] == "ok"
    assert by[("decide", "2026-10-05 08:50")] == "ok"      # same run is within tolerance of both slots
    assert by[("report_daily", "2026-10-05 08:45")] == "missed"


def test_shadow_v2_breakeven_move():
    t = SimTrade("v2", "2026-10-05", 1, 1.0, 100.0, 0, 2.0, 97.0, 106.0)
    st = SimState(trade=t)
    candles = [{"open_ms": HOUR_MS * 1, "open": 100, "high": 102.5, "low": 99.5, "close": 102},   # +1.25 ATR -> BE
               {"open_ms": HOUR_MS * 2, "open": 102, "high": 102.2, "low": 99.9, "close": 100}]   # touches BE
    _walk(st, candles, HOUR_MS * 10, True, 1.0)
    assert st.closed and st.closed[0].exit_reason == "BE" and st.closed[0].exit_price == 100.0
    t2 = SimTrade("base", "2026-10-05", 1, 1.0, 100.0, 0, 2.0, 97.0, 106.0)
    st2 = SimState(trade=t2)
    _walk(st2, candles, HOUR_MS * 10, False, 1.0)
    assert st2.trade is not None                       # without V2 the trade is still open


def test_shadow_update_runs(world):
    from perpbot.shadow import update_shadow

    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(w.clock.now().date(), "strong_long")
    w.bn.set_funding(w.clock.now().date(), 0.01)      # gate blocks the long -> gate shadow day
    w.decide()
    base = to_ms(w.clock.now())
    for i in range(24 * 6):
        o = (base // HOUR_MS + i) * HOUR_MS
        w.store.insert_ignore("pm_klines_1h", open_ms=o, open=100000, high=100000 + i * 20, low=99990, close=100000 + i * 20,
                              volume=1)
    w.clock.advance(days=6)
    update_shadow(w.engine())
    variants = {r["variant"] for r in w.store.query("SELECT variant FROM shadow_log WHERE kind='variant_snapshot'")}
    assert variants == {"live_rules", "v2_breakeven", "flat_allowed", "ungated"}
    g = w.store.query("SELECT data FROM shadow_log WHERE kind='gate_trade'")
    assert g and g[0]["data"]["blocked"] and g[0]["data"]["trade"]["exit_reason"] in ("TP", "time", "SL")


# ------------------------------------------------------------------ CLI
def _factories(ex_holder, tg):
    return Factories(exchange=lambda c, s: ex_holder["ex"], binance=lambda c: ex_holder["bn"],
                     telegram=lambda c, s: tg, sleep=lambda s: None)


def _setup_cli(tmp_root):
    make_env(tmp_root)
    cfg_path = tmp_root / "config" / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text())
    data["lock"]["wait_seconds"] = 0.3
    cfg_path.write_text(yaml.safe_dump(data))
    clock = FixedClock(hkt(2026, 10, 5, 12, 30))
    ex = MockExchange(clock=clock)
    ex.proxy_info = type(ex.proxy_info)("0xOWNER", "0xP", None)
    holder = {"ex": ex, "bn": FakeBinance(clock.now().date())}
    tg = FakeTelegram()
    return Paths(tmp_root), clock, holder, tg


def test_cli_pause_resume_status_kill(tmp_root, capsys):
    paths, clock, holder, tg = _setup_cli(tmp_root)
    f = _factories(holder, tg)
    assert main(["pause"], paths=paths, clock=clock, factories=f) == 0
    assert main(["status"], paths=paths, clock=clock, factories=f) == 0
    assert "state: paused" in capsys.readouterr().out
    assert main(["resume"], paths=paths, clock=clock, factories=f) == 0
    assert main(["kill"], paths=paths, clock=clock, factories=f) == 0
    assert main(["manage"], paths=paths, clock=clock, factories=f) == 0
    assert main(["backup"], paths=paths, clock=clock, factories=f) == 0
    assert list((tmp_root / "data" / "backups").glob("*.sqlite3"))
    assert main(["report", "daily"], paths=paths, clock=clock, factories=f) == 0
    s = Store(paths.db_file, clock, "x", "x")
    starts = {r["command"] for r in s.query("SELECT command FROM runs WHERE event='start'")}
    assert {"pause", "status", "resume", "kill", "manage", "backup", "report_daily"} <= starts
    ends = s.query("SELECT status FROM runs WHERE event='end'")
    assert all(r["status"] == "ok" for r in ends)
    s.close()
    raw = b"".join(p.read_bytes() for p in (tmp_root / "data").glob("btcperp.sqlite3*"))   # db + WAL
    for line in (tmp_root / ".env").read_text().splitlines():
        key, _, value = line.partition("=")
        if key in ("PM_PROXY_PRIVATE_KEY", "PM_PROXY_SECRET", "TELEGRAM_BOT_TOKEN"):
            assert value.encode() not in raw, key


def test_cli_error_exit_code_and_alert(tmp_root):
    paths, clock, holder, tg = _setup_cli(tmp_root)
    holder["ex"].raise_on["get_instruments"] = RuntimeError("exchange exploded")
    rc = main(["manage"], paths=paths, clock=clock, factories=_factories(holder, tg))
    assert rc == 1
    assert any("ERROR in manage" in m and "exchange exploded" in m for m in tg.sent)


def test_cli_lock_prevents_concurrent_runs(tmp_root):
    paths, clock, holder, tg = _setup_cli(tmp_root)
    paths.ensure()
    held = FileLock(paths.lock_file)
    held.acquire(0)
    try:
        rc = main(["manage"], paths=paths, clock=clock, factories=_factories(holder, tg))
    finally:
        held.release()
    assert rc == 4 and any("not run" in m for m in tg.sent)


def test_cli_config_error(tmp_root):
    paths, clock, holder, tg = _setup_cli(tmp_root)
    (tmp_root / "config" / "config.yaml").write_text("config_version: '1'\n")
    assert main(["status"], paths=paths, clock=clock, factories=_factories(holder, tg)) == 3


def test_cli_missing_secrets_fails_trading_commands(tmp_root):
    paths, clock, holder, tg = _setup_cli(tmp_root)
    (tmp_root / ".env").write_text("TELEGRAM_CHAT_ID=42\n")
    assert main(["manage"], paths=paths, clock=clock, factories=_factories(holder, tg)) == 1


def test_cli_smoketest_with_mock(tmp_root):
    paths, clock, holder, tg = _setup_cli(tmp_root)
    from perpbot.envsecrets import load_secrets

    holder["ex"].proxy_info = type(holder["ex"].proxy_info)(load_secrets(paths.env_file).wallet_address,
                                                            "0xP", to_ms(clock.now() + timedelta(days=30)))
    # resting order would fill at the mock's bid if marketable; offset keeps it resting
    rc = main(["smoketest"], paths=paths, clock=clock, factories=_factories(holder, tg))
    out = list((tmp_root / "data" / "smoketest").glob("smoketest_*.json"))
    assert out and rc == 0, out[0].read_text()[:3000] if out else "no smoketest output"
    import json as _json

    assert _json.loads(out[0].read_text())["ok"] is True


def test_monthly_only_first_sunday(tmp_root, capsys):
    paths, clock, holder, tg = _setup_cli(tmp_root)
    f = _factories(holder, tg)
    clock.set(hkt(2026, 10, 11, 20, 30))               # second Sunday
    assert main(["report", "monthly", "--only-first-sunday"], paths=paths, clock=clock, factories=f) == 0
    assert "skipped" in capsys.readouterr().out
    clock.set(hkt(2026, 10, 4, 20, 30))                # first Sunday of October 2026
    assert main(["report", "monthly", "--only-first-sunday"], paths=paths, clock=clock, factories=f) == 0
    assert (tmp_root / "data" / "reports" / "monthly" / "monthly_2026-09.md").exists()


def test_alerts_command_delivers_once(tmp_root, capsys):
    paths, clock, holder, tg = _setup_cli(tmp_root)
    f = _factories(holder, tg)
    holder["ex"].raise_on["get_instruments"] = RuntimeError("exchange exploded")
    assert main(["manage"], paths=paths, clock=clock, factories=f) == 1
    assert "NEW ALERTS FOR OWNER" in capsys.readouterr().out
    assert main(["pause"], paths=paths, clock=clock, factories=f) == 0
    capsys.readouterr()
    assert main(["alerts"], paths=paths, clock=clock, factories=f) == 0
    out = capsys.readouterr().out
    assert "PING OWNER" in out and "ERROR in manage" in out and "paused" in out
    assert "PM_PROXY" not in out and "test-secret-value-123" not in out
    assert main(["alerts"], paths=paths, clock=clock, factories=f) == 0
    assert "no new alerts" in capsys.readouterr().out


def test_telegram_disabled_by_config_even_with_token(cfg):
    from perpbot.telegram import Telegram

    tg = Telegram(cfg, "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij", "42")
    assert cfg.telegram.enabled is False and tg.enabled is False
    assert tg.send("x") is False and tg.get_updates(None) == []


def test_alerts_stored_when_telegram_disabled(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.tg.enabled = False
    w.engine().cmd_pause()
    rows = w.store.query("SELECT kind, sent FROM alerts")
    assert rows and rows[-1]["kind"] == "paused"
