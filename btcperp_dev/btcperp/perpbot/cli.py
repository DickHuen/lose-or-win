"""Command-line entry points (run by Windows Task Scheduler and the .bat shortcuts).

  decide | manage | report daily|weekly|monthly [--month YYYY-MM] | backup | status | reasons
  pause | unpause | kill | resume [--reset-peak] | alerts | selftest | smoketest [--no-trade] [--probe-withdrawal]
  flowwatch | version | snapshot (read-only exchange read for the dashboard)
  dashboard [--port N] [--no-browser] | schedule install|remove|list|show [--dry-run] [--no-dashboard] [--upgrade]
  proxykey new --owner 0x.. [--days N<=30] [--offline | --phone [--host IP]] | proxykey finish [--signature 0x..] | proxykey status
  backtest download | criteria | confirm | run   (offline from downloaded Binance data; see BACKTEST.md)
  preview [--equity USD]   (read-only: what the strategy would decide now; public data, no keys, no orders)

Every bot command: exclusive file lock, full logging, non-zero exit code on error.
Alerts (including errors) are stored and shown on the dashboard; Windows notifications (and Telegram,
if enabled) are sent after the run, once the lock is released, so they can never delay a trading action.
`decide` / `manage` ping the optional heartbeat URL (HEALTHCHECK_PING_URL in .env) after the run.
`dashboard` and `schedule` take no lock: they never touch the exchange.
Exit codes: 0 ok, 1 error, 3 config/secrets/wrong folder, 4 another command running, 5 selftest failed,
6 needs an extra confirmation (resume with an active kill switch).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from perpbot import code_version
from perpbot.calendar_events import CalendarError, load_calendar
from perpbot.config import ConfigError, load_config
from perpbot.envsecrets import SecretsError, load_secrets
from perpbot.lockfile import FileLock, LockTimeout
from perpbot.logging_setup import setup_logging, tail
from perpbot.notify import make_notifier
from perpbot.paths import Paths
from perpbot.schedule_audit import audit, nearest_slot
from perpbot.storage import Store
from perpbot.telegram import Telegram
from perpbot.timeutil import Clock, SystemClock, fmt_hkt

log = logging.getLogger("perpbot.cli")

EXIT_OK, EXIT_ERROR, EXIT_CONFIG, EXIT_LOCK, EXIT_SELFTEST, EXIT_CONFIRM = 0, 1, 3, 4, 5, 6
NEEDS_EXCHANGE = {"decide", "manage", "status", "kill", "smoketest", "resume", "flowwatch", "snapshot"}
NO_LOCK = {"dashboard", "schedule", "proxykey", "backtest", "preview"}
QUIET = {"snapshot"}              # dashboard convenience read: failures are logged, never alerted
SNAPSHOT_LOCK_WAIT_SECONDS = 5.0  # a snapshot never queues behind a real run
NEEDS_BINANCE = {"decide", "manage", "smoketest"}
TRADING = {"decide", "manage", "kill", "smoketest"}
HEARTBEAT = {"decide", "manage"}
# manual commands refused in a copy that is not the folder the scheduled tasks run in (review v1.2.0 item 16)
GUARDED = {"pause", "unpause", "kill", "resume", "reasons", "status", "alerts", "smoketest", "flowwatch", "snapshot",
           "dashboard", "proxykey", "backtest"}
NOTIFY_WAIT_SECONDS = 15.0


@dataclass
class Factories:
    exchange: Callable[[Any, Any], Any] | None = None
    binance: Callable[[Any], Any] | None = None
    telegram: Callable[[Any, Any], Any] | None = None
    notifier: Callable[[Any], Any] | None = None
    heartbeat: Callable[[str, bool], Any] | None = None
    registered_root: Callable[[], Path | None] | None = None
    sleep: Callable[[float], None] = time.sleep


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="btcperp", description="BTC-PERP bot for Polymarket Perps")
    sub = p.add_subparsers(dest="command", required=True)
    for c in ("decide", "manage", "backup", "status", "reasons", "pause", "unpause", "kill", "alerts", "selftest",
              "version", "snapshot"):
        sub.add_parser(c)
    rs = sub.add_parser("resume")
    rs.add_argument("--reset-peak", action="store_true",
                    help="confirm clearing an active kill switch: resets the drawdown peak and the losing-streak count")
    r = sub.add_parser("report")
    r.add_argument("kind", choices=["daily", "weekly", "monthly"])
    r.add_argument("--month", help="YYYY-MM for the monthly report (default: previous month)")
    r.add_argument("--only-first-sunday", action="store_true",
                   help="monthly: do nothing unless today (HKT) is the first Sunday of the month")
    s = sub.add_parser("smoketest")
    s.add_argument("--no-trade", action="store_true", help="read-only checks, no orders")
    s.add_argument("--probe-withdrawal", action="store_true",
                   help="also test (a): send a proxy-signed 1-base-unit withdrawal to your own wallet (expected rejection)")
    fw = sub.add_parser("flowwatch")
    fw.add_argument("--minutes", type=float, default=30.0, help="how long to watch (default 30)")
    fw.add_argument("--interval", type=float, default=15.0, help="seconds between reads (default 15)")
    d = sub.add_parser("dashboard", help="local web dashboard on http://127.0.0.1:<port>")
    d.add_argument("--port", type=int, default=None, help="port (default: dashboard.port in config)")
    d.add_argument("--no-browser", action="store_true", help="do not open the browser")
    sc = sub.add_parser("schedule", help="Windows Task Scheduler tasks for the bot")
    sc.add_argument("action", choices=["install", "remove", "list", "show"])
    sc.add_argument("--dry-run", action="store_true", help="write the task XML files but do not register them")
    sc.add_argument("--no-dashboard", action="store_true", help="do not start the dashboard at logon")
    sc.add_argument("--upgrade", action="store_true",
                    help="re-register tasks that already run from this folder (used by the upgrade)")
    pk = sub.add_parser("proxykey", help="create a proxy key; the main wallet only signs, elsewhere")
    pk.add_argument("action", choices=["new", "finish", "status"])
    pk.add_argument("--days", type=int, default=30, help="proxy key lifetime in days (1-30, default 30)")
    pk.add_argument("--owner", default=None, help="new: your MAIN wallet address (asked for if not given)")
    pk.add_argument("--label", default="btcperp")
    pk.add_argument("--offline", action="store_true", help="new: only write sign_fields.txt to sign on another computer")
    pk.add_argument("--phone", action="store_true",
                    help="new: serve the signing page on this PC's home Wi-Fi address for the phone's MetaMask app")
    pk.add_argument("--host", default=None, help="new --phone: this PC's home-network IP (default: detected)")
    pk.add_argument("--port", type=int, default=8766, help="new: local signing page port (default 8766)")
    pk.add_argument("--no-browser", action="store_true")
    pk.add_argument("--signature", default=None, help="finish: the signature (asked for if not given)")
    pv = sub.add_parser("preview", help="read-only: what the strategy would decide right now (no orders, no keys)")
    pv.add_argument("--equity", type=float, default=None, help="equity for the size example (default: last recorded)")
    bt = sub.add_parser("backtest", help="backtest on downloaded Binance history (never trades)")
    bt.add_argument("action", choices=["download", "criteria", "confirm", "run"])
    return p


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def _notify(notifier: Any, kind: str, text: str) -> None:
    if notifier is None:
        return
    try:
        notifier(kind, text)
    except Exception:  # noqa: BLE001
        log.debug("notification failed", exc_info=True)


def _wait_notifier(notifier: Any) -> None:
    wait = getattr(notifier, "wait", None)
    if callable(wait):
        try:
            wait(NOTIFY_WAIT_SECONDS)
        except Exception:  # noqa: BLE001
            log.debug("notification wait failed", exc_info=True)


def send_heartbeat(url: str, kind: str, timeout: float = 5.0) -> bool:
    """Dead-man's switch (review v1.2.0 item 5, v1.3.0 V4): a bare GET, no data. kind: start | success | fail
    (`<url>/start`, `<url>`, `<url>/fail`). Never raises; the URL itself is never logged."""
    if not url:
        return False
    try:
        import httpx

        r = httpx.get(url.rstrip("/") + {"start": "/start", "success": "", "fail": "/fail"}[kind], timeout=timeout)
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        log.warning("heartbeat ping failed (%s)", type(e).__name__)
        return False


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _other_install(paths: Paths, factories: Factories) -> Path | None:
    """The folder the scheduled tasks run in, if it is NOT this copy."""
    try:
        if factories.registered_root is not None:
            root = factories.registered_root()
        else:
            from perpbot import winsched

            root = winsched.registered_root()
    except Exception:  # noqa: BLE001
        log.debug("registered root lookup failed", exc_info=True)
        return None
    if root is None or _same_path(root, paths.root):
        return None
    return root


def smoketest_gate(paths: Paths, secrets: Any) -> tuple[bool, str]:
    """Review v1.2.0 item 21: going live needs a passing FULL smoketest of this code version with this proxy key."""
    files = sorted(paths.smoketest_dir.glob("smoketest_*.json"))
    for f in reversed(files):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not data.get("allow_trading"):
            continue
        if not data.get("ok"):
            return False, f"the latest full smoketest ({f.name}) did not pass"
        if data.get("code_version") != code_version():
            return False, (f"the latest full smoketest ({f.name}) ran on version {data.get('code_version')}, "
                           f"this is {code_version()}: run 2_Smoketest.bat again")
        if not secrets.proxy_address or (data.get("proxy_address") or "").lower() != secrets.proxy_address.lower():
            return False, (f"the latest full smoketest ({f.name}) used a different proxy key than .env: "
                           f"run 2_Smoketest.bat again")
        return True, f"smoketest {f.name} passed"
    return False, "no full smoketest has passed yet: run 2_Smoketest.bat (type YES) first"


def _expire_proxykey_request(paths: Paths) -> None:
    """Review v1.3.0 P4: every bot run deletes an unfinished proxy key request older than an hour."""
    try:
        from perpbot.proxykey import expire_pending

        expire_pending(paths)
    except Exception:  # noqa: BLE001
        log.warning("could not check the pending proxy key request", exc_info=True)


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
    _utf8_console()
    paths.ensure()
    if command == "version":
        print(code_version())
        return EXIT_OK
    _expire_proxykey_request(paths)

    # --- config / secrets
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
        return EXIT_CONFIG
    log_path, redactor = setup_logging(paths.logs_dir, clock, cfg.logging.level, secrets.secret_values())
    if command in GUARDED:
        other = _other_install(paths, factories)
        if other is not None:
            print(f"WRONG FOLDER: the scheduled bot runs in {other}, not in {paths.root}.\n"
                  f"Use the .bat files in {other}\\windows, for example "
                  f"{other}\\windows\\Kill_Close_Position.bat. (This copy has its own data and .env.)", file=sys.stderr)
            return EXIT_CONFIG
    if command in NO_LOCK:
        return _run_unlocked(command, args, paths, cfg, calendar, clock, secrets, factories)
    tg = (factories.telegram or (lambda c, s: Telegram(c, s.telegram_token, s.telegram_chat_id)))(cfg, secrets)
    notifier = (factories.notifier or make_notifier)(cfg)

    lock = FileLock(paths.lock_file)
    try:
        lock.acquire(SNAPSHOT_LOCK_WAIT_SECONDS if command in QUIET else float(cfg.lock.wait_seconds))
    except LockTimeout as e:
        print(f"NOT RUN: {command}: {e}")
        if command in QUIET:
            log.info("%s skipped: bot busy", command)
            return EXIT_LOCK
        log.error("%s", e)
        tg.send(f"[btcperp] {command} not run: {e}")
        _notify(notifier, f"{command} not run", str(e))
        _wait_notifier(notifier)
        return EXIT_LOCK

    store = Store(paths.db_file, clock, cfg.config_version, code_version())
    store.redact = redactor.redact                     # review D14: nothing secret reaches the database
    store.run_id = uuid.uuid4().hex
    started = clock.now()
    slot, lateness = nearest_slot(cfg, command, started)
    store.insert("runs", command=command, event="start", scheduled_for=fmt_hkt(slot) if slot else None,
                 status="running", lateness_min=lateness, data={"argv": argv or sys.argv[1:], "pid_run": store.run_id})
    log.info("=== %s start (code %s, config %s) scheduled %s lateness %s min", command, code_version(),
             cfg.config_version, fmt_hkt(slot) if slot else "-", f"{lateness:.1f}" if lateness is not None else "-")
    hb_url = secrets.heartbeat_url(command) if command in HEARTBEAT else ""
    hb = factories.heartbeat or send_heartbeat
    hb_start = None
    if hb_url:                                        # /start in the background: a slow ping never delays trading
        import threading

        hb_start = threading.Thread(target=hb, args=(hb_url, "start"), daemon=True)
        hb_start.start()
    exchange = binance = None
    engine: Any = None
    rc = EXIT_OK
    status, err = "ok", None
    cli_alerts: list[tuple[str, str, str]] = []
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
            from perpbot.engine import Engine, NeedsConfirmation

            engine = Engine(cfg=cfg, calendar=calendar, store=store, exchange=exchange, binance=binance, telegram=tg,
                            clock=clock, secrets=secrets, sleep=factories.sleep, notifier=notifier,
                            defer_notifications=True)
            if command in TRADING:
                _missed_run_alerts(engine, cfg, store, clock)
            try:
                result = _dispatch(command, args, engine, paths, cfg)
            except NeedsConfirmation as e:
                print(f"NEEDS CONFIRMATION: {e}")
                rc, status, result = EXIT_CONFIRM, "needs_confirmation", {}
            if command == "smoketest" and not result.get("ok", True):
                rc, status, err = EXIT_ERROR, "error", "smoketest failed"
    except Exception as e:  # noqa: BLE001
        rc, status, err = EXIT_ERROR, "error", redactor.redact(f"{type(e).__name__}: {e}")
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
        if status == "error" and command not in QUIET:
            text = redactor.redact(f"[btcperp] ERROR in {command}: {err}\nlog: {log_path}\n--- last log lines ---\n"
                                   f"{_redacted_tail(log_path, redactor)}")
            try:
                store.insert("alerts", kind="ERROR", dedupe_key=None, sent=0, text=text[:4000])
            except Exception:  # noqa: BLE001
                log.exception("could not store error alert")
            cli_alerts.append((f"ERROR in {command}", str(err)[:300], text))
        if command != "alerts":
            try:
                n = len(pending_alerts(store))
                if n:
                    print(f"NEW ALERTS FOR OWNER: {n} (see the dashboard, or run: python run.py alerts)")
            except Exception:  # noqa: BLE001
                log.debug("pending alert count failed", exc_info=True)
        hb_kind = "success"
        if hb_url:
            hb_kind = "fail" if status == "error" else _heartbeat_state(store)
        store.close()
        lock.release()
        # --- after the lock: nothing below can delay a trading run (review v1.2.0 item 6)
        if engine is not None:
            engine.flush_notifications()
        for kind, short, text in cli_alerts:
            try:
                tg.send(text)
            except Exception:  # noqa: BLE001
                log.debug("telegram send failed", exc_info=True)
            _notify(notifier, kind, short)
        if hb_url:
            if hb_start is not None:
                hb_start.join(timeout=10)                  # the end ping must never overtake /start
            hb(hb_url, hb_kind)
        _wait_notifier(notifier)
    return rc


def _heartbeat_state(store: Store) -> str:
    """Review v1.3.0 V4: `fail` while a critical alert is unread (not yet marked read in the dashboard or
    Alerts.bat) or a hard stop is active, so the phone keeps showing the problem."""
    from perpbot.engine import CRITICAL_ALERT_KINDS, HARD_STOP_REASONS
    from perpbot.records import Records

    try:
        kinds = sorted(CRITICAL_ALERT_KINDS)
        marks = ",".join("?" for _ in kinds)
        unread = store.count("alerts", f"kind IN ({marks}) AND id NOT IN (SELECT alert_id FROM alert_deliveries)", kinds)
        hard = [r for r in Records(store).state()["pause_reasons"] if r in HARD_STOP_REASONS]
        return "fail" if (unread or hard) else "success"
    except Exception:  # noqa: BLE001
        log.warning("heartbeat state failed", exc_info=True)
        return "fail"


def _run_unlocked(command: str, args: Any, paths: Paths, cfg: Any, calendar: Any, clock: Clock, secrets: Any,
                  factories: Factories) -> int:
    """dashboard / schedule: no lock, no exchange, no database writes by this process."""
    try:
        if command == "dashboard":
            from perpbot.dashboard import serve

            port = int(args.port or cfg.dashboard.port)
            return serve(paths, cfg, calendar, clock, port=port, open_browser=not args.no_browser)
        if command == "proxykey":
            return _run_proxykey(args, paths, cfg, secrets)
        if command == "preview":
            return _run_preview(args, paths, cfg, calendar, clock, factories)
        if command == "backtest":
            return _run_backtest(args, paths, cfg, calendar, clock, factories, secrets)
        from perpbot import winsched

        if args.action == "show":
            for line in winsched.summary_lines(cfg) + winsched.power_lines():
                print(line)
            return EXIT_OK
        if args.action == "install":
            if not args.dry_run:
                if args.upgrade:
                    reg = factories.registered_root() if factories.registered_root else winsched.registered_root()
                    if reg is None or not _same_path(reg, paths.root):
                        print("NOT INSTALLED: --upgrade only re-registers tasks that already run from this folder.")
                        return EXIT_CONFIG
                else:
                    ok, why = smoketest_gate(paths, secrets)
                    if not ok:
                        print(f"NOT INSTALLED: {why}")
                        return EXIT_CONFIG
                    print(f"go-live check: {why}")
            results = winsched.install(cfg, paths.root, paths.data_dir / "tasks", dry_run=bool(args.dry_run),
                                       with_dashboard=not args.no_dashboard)
        elif args.action == "remove":
            results = winsched.remove(cfg)
        else:
            results = winsched.listing(cfg)
        bad = [r for r in results if r["ok"] is False]
        for r in results:
            mark = {True: "OK  ", False: "FAIL", None: "--  "}[r["ok"]]
            print(f"{mark} {r['task']}  {r['output'].splitlines()[0] if r['output'] else ''}")
            if r["ok"] is False and args.action != "list":
                print(r["output"])
        if args.action in ("install", "list"):
            print()
            for line in winsched.summary_lines(cfg) + winsched.power_lines():
                print(line)
        return EXIT_ERROR if bad else EXIT_OK
    except Exception as e:  # noqa: BLE001
        log.error("command %s failed: %s\n%s", command, e, traceback.format_exc())
        print(f"ERROR: {command}: {e}", file=sys.stderr)
        return EXIT_ERROR


def _run_proxykey(args: Any, paths: Paths, cfg: Any, secrets: Any) -> int:
    from perpbot import proxykey as pkm

    def with_lock(fn: Callable[[], Any]) -> Any:
        """P7: the key is registered and .env written only while no bot command runs."""
        lock = FileLock(paths.lock_file)
        lock.acquire(float(cfg.lock.wait_seconds))
        try:
            return fn()
        finally:
            lock.release()

    try:
        if args.action == "status":
            for line in pkm.status_lines(paths, cfg, secrets):
                print(line)
            return EXIT_OK
        if args.action == "new":
            if args.offline and args.phone:
                print("FAILED: choose --offline or --phone, not both")
                return EXIT_ERROR
            host = "127.0.0.1"
            if args.phone:
                host = (args.host or pkm.lan_ip() or "").strip()
                if not host or not pkm.is_private_lan(host):
                    print("FAILED: could not find this PC's home Wi-Fi / LAN address (192.168.x.x, 10.x.x.x or "
                          "172.16-31.x.x). Connect this PC to the same Wi-Fi as the phone and try again.")
                    return EXIT_ERROR
            owner = (args.owner or "").strip()
            if not owner:
                hint = f" [{secrets.wallet_address}]" if secrets.wallet_address else ""
                owner = input(f"Your MAIN wallet address (0x...){hint}: ").strip() or secrets.wallet_address
            method = "phone" if args.phone else "offline" if args.offline else "browser"
            p = pkm.new_request(paths, cfg, days=int(args.days), label=str(args.label), owner=owner, method=method,
                                expected_owner=secrets.wallet_address)
            print(f"New proxy key made on this computer: {p.proxy}")
            print(f"  for main wallet {p.owner}, expires {pkm._hkt(p.exp_ms)} ({int(args.days)} days).")
            print("The proxy PRIVATE key stays on this computer (deleted after 1 hour if not finished).")
            if args.offline:
                print(f"\nCopy ONLY this file to the other computer (plain fields, nothing secret):\n"
                      f"  {paths.data_dir / 'proxykey' / 'sign_fields.txt'}\n"
                      f"There, use YOUR OWN copy of the release zip (check its SHA-256), not files from this PC:\n"
                      f"  python perpbot\\offline_sign.py sign_fields.txt      (or offline_sign\\offline_sign.html)\n"
                      f"Then come back within 1 hour: Proxy_Key.bat, option F.")
                return EXIT_OK
            if args.phone:
                token = pkm.phone_token()
                print("\nSIGN ON YOUR PHONE (same Wi-Fi as this PC), within 15 minutes:")
                print("  1. Open the MetaMask app, tap the Browser, and type this address exactly:")
                print(f"\n        http://{host}:{int(args.port)}/{token}/\n")
                print("  2. Tap the sign button. MetaMask must show CreateProxy / Polymarket and the proxy address above.")
                print("     If it shows Permit, Approve, a transfer or anything else: reject it and tell Claude.")
                print("  If Windows Firewall asks about Python: allow it on PRIVATE networks.")
                res = pkm.serve_signing(paths, cfg, p, port=int(args.port), open_browser=False, with_lock=with_lock,
                                        host=host, token=token)
            else:
                print("Your browser opens a signing page. Sign with the HARDWARE wallet connected to MetaMask/Rabby.")
                res = pkm.serve_signing(paths, cfg, p, port=int(args.port), open_browser=not args.no_browser,
                                        with_lock=with_lock)
        else:
            sig = args.signature or input("Paste the signature (0x...): ").strip()
            res = dict(pkm.finish(paths, cfg, sig, with_lock=with_lock), ok=True)
        if not res.get("ok"):
            print(f"FAILED: {res.get('error')}")
            return EXIT_ERROR
        print(f"DONE: proxy {res['proxy']} registered for wallet {res['owner']}, expires {res['expires_utc']}.")
        print(".env updated. Next: windows\\2_Smoketest.bat")
        return EXIT_OK
    except LockTimeout as e:
        print(f"FAILED: a bot command is running ({e}); .env was not changed. Try again in a minute.")
        return EXIT_ERROR
    except pkm.ProxyKeyError as e:
        print(f"FAILED: {e}")
        return EXIT_ERROR


def _run_preview(args: Any, paths: Paths, cfg: Any, calendar: Any, clock: Clock, factories: Factories) -> int:
    """v1.5.0 Preview.bat: the analysis for right now. Public Binance data only; reads the bot's database only to
    know an open position and the last equity; never writes, never trades."""
    from perpbot import analysis
    from perpbot.preview import preview_decision
    from perpbot.records import Records

    trade, equity, ramp = None, args.equity, int(cfg.risk.ramp_trades) > 0      # no trades yet: ramp applies
    if paths.db_file.exists():
        store = Store(paths.db_file, clock, cfg.config_version, code_version())
        try:
            rec = Records(store)
            trade = rec.open_trade()
            last = store.latest("equity_log", "equity IS NOT NULL AND equity > 0")
            if equity is None and last:
                equity = float(last["equity"])
            ramp = rec.live_trades_opened() < int(cfg.risk.ramp_trades)
        finally:
            store.close()
    bn = (factories.binance or _default_binance)(cfg)
    try:
        decision, nxt = preview_decision(cfg, calendar, clock.now(), bn, trade)
    except Exception as e:  # noqa: BLE001
        print(f"PREVIEW FAILED: {type(e).__name__}: {e}")
        return EXIT_ERROR
    finally:
        bn.close()
    for line in analysis.render(decision, cfg, equity=equity, ramp=ramp, next_hkt=nxt, position=trade,
                                title="預覽：如果而家決定"):
        print(line)
    print("（預覽只用公開數據，唔落單。實際決定仲會檢查暫停、時鐘、地區同交易所價格，所以有機會唔同。）")
    return EXIT_OK


def real_fee_rate(paths: Paths) -> float | None:
    """Taker fee rate recorded by the latest smoketest (review BT3: the backtest never uses less)."""
    for f in sorted(paths.smoketest_dir.glob("smoketest_*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for r in data.get("results", []):
            if r.get("step") == "fees" and isinstance(r.get("detail"), dict) and r["detail"].get("taker_fee_rate") is not None:
                return float(r["detail"]["taker_fee_rate"])
    return None


def _run_backtest(args: Any, paths: Paths, cfg: Any, calendar: Any, clock: Clock, factories: Factories,
                  secrets: Any) -> int:
    """Backtest (review B3 / R1): no lock (it never trades and must not delay a scheduled run)."""
    from perpbot import backtest as btm
    from perpbot.timeutil import to_ms

    crit = paths.root / cfg.backtest.criteria_file
    data_dir = paths.data_dir / "backtest"
    store = Store(paths.db_file, clock, cfg.config_version, code_version())
    real = real_fee_rate(paths)
    fee = max(float(cfg.shadow.fee_rate_estimate), real or 0.0)
    try:
        confirmed = store.latest("backtest_log", "event='criteria_confirmed'")
        if args.action == "criteria":
            print(crit.read_text(encoding="utf-8"))
            print(f"fee rate used: {fee} (config {cfg.shadow.fee_rate_estimate}, smoketest {real})")
            if confirmed:
                print(f"last confirmation: {confirmed['ts_hkt']}, manifest {confirmed['data']['manifest']['sha256'][:12]}, "
                      f"data to {confirmed['data']['manifest']['parts']['data_end_utc']}")
            else:
                print("NOT confirmed yet")
            return EXIT_OK
        if args.action == "download":
            from datetime import date as _date

            bn = (factories.binance or _default_binance)(cfg)
            pm = None
            try:
                pm = (factories.exchange or _default_exchange)(cfg, secrets)
            except Exception as e:  # noqa: BLE001 - Polymarket candles are optional (I7)
                log.warning("polymarket client unavailable: %s", e)
            try:
                counts = btm.download(bn, data_dir, _date.fromisoformat(str(cfg.backtest.data_start)), to_ms(clock.now()),
                                      pm=pm, cfg=cfg)
            except Exception as e:  # noqa: BLE001
                if pm is None or "polymarket" not in str(e).lower():
                    raise
                counts = {"polymarket_error": str(e)}
            finally:
                bn.close()
                if pm is not None:
                    pm.close()
            print(f"downloaded into {data_dir}: {counts}")
            store.insert("backtest_log", event="download", data=counts)
            return EXIT_OK
        if args.action == "confirm":
            import yaml

            ds, cal, end, dq = btm.prepare(cfg, paths.root, data_dir, calendar)
            man = btm.manifest(cfg, paths.root, dq, end, fee)
            ver = (yaml.safe_load(crit.read_text(encoding="utf-8")) or {}).get("criteria_version")
            n_conf = store.count("backtest_log", "event='criteria_confirmed'") + 1
            store.insert("backtest_log", event="criteria_confirmed", data={"criteria_version": ver, "manifest": man})
            print(f"CONFIRMED (confirmation #{n_conf}): criteria {ver}, data to {end.isoformat()} (fixed), fee {fee}, "
                  f"manifest {man['sha256'][:12]}. Nothing that decides the result can change under this confirmation.")
            return EXIT_OK
        if confirmed is None:
            print("NEEDS CONFIRMATION: the pass/fail criteria must be confirmed by the owner BEFORE the backtest runs "
                  "(after the committee has reviewed them). Read them (`backtest criteria`), then `backtest confirm`.")
            return EXIT_CONFIRM
        man0 = confirmed["data"]["manifest"]
        from datetime import date as _date

        end = _date.fromisoformat(man0["parts"]["data_end_utc"])
        ds, cal, end, dq = btm.prepare(cfg, paths.root, data_dir, calendar, end)
        man = btm.manifest(cfg, paths.root, dq, end, fee)
        if man["sha256"] != man0["sha256"]:
            print(f"NEEDS CONFIRMATION: changed since the confirmation: {', '.join(btm.manifest_diff(man0, man))}. "
                  f"A new confirmation is a new pre-registration: the committee must see why.")
            return EXIT_CONFIRM
        prior = [r for r in store.query("SELECT data FROM backtest_log WHERE event='run'")
                 if isinstance(r["data"], dict) and r["data"].get("manifest_sha256") == man["sha256"]]
        n_run = len(prior) + 1
        out = paths.data_dir / "backtest" / f"results_{clock.now().strftime('%Y%m%d_%H%M%S')}"
        rep = btm.run_backtest(cfg, paths.root, data_dir, out, calendar, fee, data_end=end, run_number=n_run,
                               manifest_sha=man["sha256"])
        rep_hist = {"confirmations_total": store.count("backtest_log", "event='criteria_confirmed'"),
                    "runs_total": store.count("backtest_log", "event='run'") + 1}
        store.insert("backtest_log", event="run", data={"verdict": rep["verdict"], "result_sha256": rep["result_sha256"],
                                                         "manifest_sha256": man["sha256"], "run_number": n_run,
                                                         "dir": str(out), **rep_hist})
        text = (out / "summary.md").read_text(encoding="utf-8")
        text = text.replace("(the committee uses run #1).",
                            f"(the committee uses run #1). History: {rep_hist['confirmations_total']} confirmation(s), "
                            f"{rep_hist['runs_total']} run(s) in total.")
        (out / "summary.md").write_text(text, encoding="utf-8")
        print(text)
        print(f"results: {out}\nSend summary.md and summary.json (never .env) for review.")
        return EXIT_OK
    except btm.BacktestError as e:
        print(f"BACKTEST ERROR: {e}")
        return EXIT_ERROR
    finally:
        store.close()


def _redacted_tail(path: Any, redactor: Any) -> str:
    return redactor.redact(tail(path, 25))[-3000:]


def pending_alerts(store: Store) -> list[dict[str, Any]]:
    return store.query("SELECT id, ts_hkt, kind, text FROM alerts WHERE id NOT IN "
                       "(SELECT alert_id FROM alert_deliveries) ORDER BY id")


def deliver_alerts(store: Store) -> list[str]:
    """Print unread alerts once and mark them read (the dashboard shows the same list)."""
    out = []
    for a in pending_alerts(store):
        line = f"PING OWNER #{a['id']} [{a['ts_hkt']}] {a['kind']}: {a['text']}"
        print(line)
        out.append(line)
        store.insert_ignore("alert_deliveries", alert_id=a["id"])
    if not out:
        print("no new alerts")
    return out


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
            first_decide = (str(cfg.strategy.cadence) == "daily" and a["command"] == "decide"
                            and a["slot_hkt"].endswith(cfg.schedule.decide_times_hkt[0]))
            engine.alert("missed run" if not first_decide else f"missed {cfg.schedule.decide_times_hkt[0]}",
                         f"scheduled {a['command']} at {a['slot_hkt']} HKT did not run",
                         dedupe_key=f"missed:{a['command']}:{a['slot_hkt']}")


def _dispatch(command: str, args: Any, engine: Any, paths: Paths, cfg: Any) -> dict[str, Any]:
    from perpbot.reports import Reporter, backup

    if command == "decide":
        out = engine.cmd_decide()
    elif command == "manage":
        out = engine.cmd_manage()
    elif command == "snapshot":
        out = engine.cmd_snapshot()
    elif command == "status":
        text = engine.status_text()
        print(text)
        return {"text": text}
    elif command == "reasons":
        text = engine.pause_reasons_text()
        print(text)
        return {"text": text}
    elif command == "pause":
        out = {"result": engine.cmd_pause()}
    elif command == "unpause":
        out = {"result": engine.cmd_unpause()}
    elif command == "kill":
        engine.instrument()
        out = {"result": engine.cmd_kill()}
    elif command == "resume":
        out = {"result": engine.cmd_resume(reset_peak=bool(args.reset_peak))}
    elif command == "alerts":
        return {"alerts": deliver_alerts(engine.store)}
    elif command == "flowwatch":
        from perpbot.smoketest import flowwatch

        path = flowwatch(engine, paths, minutes=float(args.minutes), interval=float(args.interval))
        print(f"flowwatch log: {path}")
        return {"path": str(path)}
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

        ok, results = run_smoketest(engine, paths, allow_trading=not args.no_trade,
                                    probe_withdrawal=bool(args.probe_withdrawal))
        text = summary_text(ok, results)
        print(text)
        print(json.dumps(results, indent=2, default=str)[:20000])
        return {"ok": ok}
    else:
        raise ValueError(f"unknown command {command}")
    print(json.dumps(out, indent=2, default=str))
    return out
