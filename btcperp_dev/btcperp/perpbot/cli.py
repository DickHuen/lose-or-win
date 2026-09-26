"""Command-line entry points (the only things Grok Bot runs).

  decide | manage | report daily|weekly|monthly [--month YYYY-MM] | backup | status
  pause | kill | resume | selftest | smoketest | version

Every command: exclusive file lock, full logging, non-zero exit code and a
Telegram alert on error.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from perpbot import code_version
from perpbot.calendar_events import CalendarError, load_calendar
from perpbot.config import ConfigError, load_config
from perpbot.envsecrets import SecretsError, load_secrets
from perpbot.lockfile import FileLock, LockTimeout
from perpbot.logging_setup import setup_logging, tail
from perpbot.paths import Paths
from perpbot.schedule_audit import audit, nearest_slot
from perpbot.storage import Store
from perpbot.telegram import Telegram
from perpbot.timeutil import Clock, SystemClock, fmt_hkt

log = logging.getLogger("perpbot.cli")

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_LOCK, EXIT_SELFTEST = 0, 1, 3, 4, 5
NEEDS_EXCHANGE = {"decide", "manage", "status", "kill", "smoketest", "resume"}
NEEDS_BINANCE = {"decide", "manage", "smoketest"}
TRADING = {"decide", "manage", "kill", "smoketest"}


@dataclass
class Factories:
    exchange: Callable[[Any, Any], Any] | None = None
    binance: Callable[[Any], Any] | None = None
    telegram: Callable[[Any, Any], Any] | None = None
    sleep: Callable[[float], None] = time.sleep


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="btcperp", description="BTC-PERP bot for Polymarket Perps")
    sub = p.add_subparsers(dest="command", required=True)
    for c in ("decide", "manage", "backup", "status", "pause", "kill", "resume", "selftest", "version"):
        sub.add_parser(c)
    r = sub.add_parser("report")
    r.add_argument("kind", choices=["daily", "weekly", "monthly"])
    r.add_argument("--month", help="YYYY-MM for the monthly report (default: previous month)")
    r.add_argument("--only-first-sunday", action="store_true",
                   help="monthly: do nothing unless today (HKT) is the first Sunday of the month")
    s = sub.add_parser("smoketest")
    s.add_argument("--no-trade", action="store_true", help="read-only checks, no orders")
    return p


def run_selftest(paths: Paths) -> int:
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(paths.root / "tests")]
    print("Running unit tests:", " ".join(cmd[2:]))
    rc = subprocess.call(cmd, cwd=str(paths.root))
    print("SELFTEST", "PASS" if rc == 0 else "FAIL")
    return EXIT_OK if rc == 0 else EXIT_SELFTEST


def main(argv: list[str] | None = None, *, paths: Paths | None = None, clock: Clock | None = None,
         factories: Factories | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = paths or Paths.default()
    clock = clock or SystemClock()
    factories = factories or Factories()
    command = args.command if args.command != "report" else f"report_{args.kind}"
    paths.ensure()
    if command == "version":
        print(code_version())
        return EXIT_OK

    # --- config / secrets (errors here still try to alert via Telegram)
    try:
        secrets = load_secrets(paths.env_file)
    except SecretsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        cfg = load_config(paths.config_file)
        calendar = load_calendar(paths.root / cfg.gates.calendar_file)
    except (ConfigError, CalendarError) as e:
        print(f"CONFIG ERROR: {e}", file=sys.stderr)
        _raw_alert(secrets, f"[btcperp] config error in {command}: {e}")
        return EXIT_CONFIG
    log_path, _ = setup_logging(paths.logs_dir, clock, cfg.logging.level, secrets.secret_values())
    tg = (factories.telegram or (lambda c, s: Telegram(c, s.telegram_token, s.telegram_chat_id)))(cfg, secrets)

    lock = FileLock(paths.lock_file)
    try:
        lock.acquire(float(cfg.lock.wait_seconds))
    except LockTimeout as e:
        log.error("%s", e)
        tg.send(f"[btcperp] {command} not run: {e}")
        return EXIT_LOCK

    store = Store(paths.db_file, clock, cfg.config_version, code_version())
    store.run_id = uuid.uuid4().hex
    started = clock.now()
    slot, lateness = nearest_slot(cfg, command, started)
    store.insert("runs", command=command, event="start", scheduled_for=fmt_hkt(slot) if slot else None,
                 status="running", lateness_min=lateness, data={"argv": argv or sys.argv[1:], "pid_run": store.run_id})
    log.info("=== %s start (code %s, config %s) scheduled %s lateness %s min", command, code_version(),
             cfg.config_version, fmt_hkt(slot) if slot else "-", f"{lateness:.1f}" if lateness is not None else "-")
    exchange = binance = None
    rc = EXIT_OK
    status, err = "ok", None
    try:
        if command == "selftest":
            rc = run_selftest(paths)
            status = "ok" if rc == 0 else "error"
            err = None if rc == 0 else "selftest failed"
        else:
            if command in NEEDS_EXCHANGE:
                secrets.require_trading()
                exchange = (factories.exchange or _default_exchange)(cfg, secrets)
            elif command == "report_daily" and secrets.proxy_private_key and secrets.proxy_address:
                exchange = (factories.exchange or _default_exchange)(cfg, secrets)
            if command in NEEDS_BINANCE:
                binance = (factories.binance or _default_binance)(cfg)
            from perpbot.engine import Engine

            engine = Engine(cfg=cfg, calendar=calendar, store=store, exchange=exchange, binance=binance, telegram=tg,
                            clock=clock, secrets=secrets, sleep=factories.sleep)
            if command in TRADING:
                _missed_run_alerts(engine, cfg, store, clock)
            result = _dispatch(command, args, engine, paths, cfg)
            if command == "smoketest" and not result.get("ok", True):
                rc, status, err = EXIT_ERROR, "error", "smoketest failed"
    except Exception as e:  # noqa: BLE001
        rc, status, err = EXIT_ERROR, "error", f"{type(e).__name__}: {e}"
        log.error("command %s failed: %s\n%s", command, err, traceback.format_exc())
    finally:
        dur = (clock.now() - started).total_seconds()
        try:
            retries = int(getattr(binance, "retries", 0) or 0)
            store.insert("runs", command=command, event="end", status=status, error=err, duration_s=dur,
                         data={"retries": retries})
        except Exception:  # noqa: BLE001
            log.exception("could not write run end")
        for c in (exchange, binance):
            try:
                if c is not None:
                    c.close()
            except Exception:  # noqa: BLE001
                log.debug("close failed", exc_info=True)
        log.info("=== %s end: %s (%.1fs)", command, status, dur)
        if status == "error":
            tg.send(f"[btcperp] ERROR in {command}: {err}\nlog: {log_path}\n--- last log lines ---\n{_redacted_tail(log_path)}")
        store.close()
        lock.release()
    return rc


def _redacted_tail(path: Any) -> str:
    t = tail(path, 25)
    from perpbot.logging_setup import RedactFilter

    return RedactFilter().redact(t)[-3000:]


def _raw_alert(secrets: Any, text: str) -> None:
    if not (secrets.telegram_token and secrets.telegram_chat_id):
        return
    try:
        import httpx

        httpx.post(f"https://api.telegram.org/bot{secrets.telegram_token}/sendMessage",
                   data={"chat_id": secrets.telegram_chat_id, "text": text}, timeout=15)
    except Exception:  # noqa: BLE001
        pass


def _default_exchange(cfg: Any, secrets: Any) -> Any:
    from perpbot.exchange.polymarket import PolymarketExchange

    return PolymarketExchange(cfg, secrets)


def _default_binance(cfg: Any) -> Any:
    from perpbot.datasources.binance import BinanceData

    return BinanceData(cfg)


def _missed_run_alerts(engine: Any, cfg: Any, store: Store, clock: Clock) -> None:
    from datetime import timedelta

    now = clock.now()
    first = store.query("SELECT MIN(ts_ms) AS t FROM runs WHERE event='start' AND command IN ('decide', 'manage')")
    if not first or first[0]["t"] is None:
        return
    from perpbot.timeutil import from_ms

    start = max(now - timedelta(hours=26), from_ms(int(first[0]["t"])))
    end = now - timedelta(minutes=float(cfg.schedule.missed_tolerance_minutes))
    if end <= start:
        return
    for a in audit(store, cfg, start, end):
        if a["status"] == "missed" and a["command"] in ("decide", "manage"):
            first_decide = a["command"] == "decide" and a["slot_hkt"].endswith(cfg.schedule.decide_times_hkt[0])
            engine.alert("missed run" if not first_decide else f"missed {cfg.schedule.decide_times_hkt[0]}",
                         f"scheduled {a['command']} at {a['slot_hkt']} HKT did not run",
                         dedupe_key=f"missed:{a['command']}:{a['slot_hkt']}")


def _dispatch(command: str, args: Any, engine: Any, paths: Paths, cfg: Any) -> dict[str, Any]:
    from perpbot.reports import Reporter, backup

    if command == "decide":
        out = engine.cmd_decide()
    elif command == "manage":
        out = engine.cmd_manage()
    elif command == "status":
        text = engine.status_text()
        print(text)
        return {"text": text}
    elif command == "pause":
        out = {"result": engine.cmd_pause()}
    elif command == "kill":
        engine.instrument()
        out = {"result": engine.cmd_kill()}
    elif command == "resume":
        out = {"result": engine.cmd_resume()}
    elif command == "backup":
        dest = backup(engine.store, paths, engine.now())
        out = {"backup": str(dest)}
    elif command.startswith("report_"):
        if command == "report_monthly" and getattr(args, "only_first_sunday", False):
            from perpbot.timeutil import hkt_date

            d = hkt_date(engine.now())
            if not (d.weekday() == 6 and d.day <= 7):
                print(f"{d.isoformat()} (HKT) is not the first Sunday of the month; monthly report skipped")
                return {"skipped": True}
        rep = Reporter(engine, paths)
        text = {"report_daily": rep.daily, "report_weekly": rep.weekly}.get(command)
        out = {"text": text() if text else rep.monthly(getattr(args, "month", None))}
        print(out["text"])
        return out
    elif command == "smoketest":
        from perpbot.smoketest import run_smoketest, summary_text

        ok, results = run_smoketest(engine, paths, allow_trading=not args.no_trade)
        text = summary_text(ok, results)
        print(text)
        print(json.dumps(results, indent=2, default=str)[:20000])
        engine.tg.send(text)
        return {"ok": ok}
    else:
        raise ValueError(f"unknown command {command}")
    print(json.dumps(out, indent=2, default=str))
    return out
