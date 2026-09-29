"""Daily / weekly / monthly reports, CSV exports and backups."""

from __future__ import annotations

import json
import shutil
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from perpbot.paths import Paths
from perpbot.schedule_audit import audit
from perpbot.timeutil import UTC, day_start_ms, fmt_hkt, hkt_date, to_ms, utc_day


def _fmt(x: Any, nd: int = 2) -> str:
    if x is None:
        return "-"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


# ---------------------------------------------------------------- stats helpers
def trade_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(trades)
    if not n:
        return {"trades": 0}
    net = [float(t.get("net_pnl") or 0) for t in trades]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    risk = sum(float(t.get("initial_risk_usd") or 0) for t in trades)
    rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    return {
        "trades": n, "wins": len(wins), "win_rate_pct": 100.0 * len(wins) / n, "net_pnl": sum(net),
        "gross_pnl": sum(float(t.get("gross_pnl") or 0) for t in trades),
        "fees": sum(float(t.get("fees_total") or 0) for t in trades),
        "funding": sum(float(t.get("funding") or 0) for t in trades),
        "avg_r": (sum(rs) / len(rs)) if rs else None, "total_r": sum(rs) if rs else None,
        "size_weighted_expectancy_r": (sum(net) / risk) if risk else None,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None,
        "avg_holding_hours": sum(float(t.get("holding_hours") or 0) for t in trades) / n,
    }


def group_by(trades: list[dict[str, Any]], key_fn: Any) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        keys = key_fn(t)
        for k in (keys if isinstance(keys, list) else [keys]):
            groups[str(k)].append(t)
    return {k: trade_stats(v) for k, v in sorted(groups.items())}


def max_drawdown(equities: Iterable[float]) -> float:
    peak, mdd = None, 0.0
    for e in equities:
        peak = e if peak is None else max(peak, e)
        if peak and peak > 0:
            mdd = max(mdd, (peak - e) / peak * 100.0)
    return mdd


def version_key(t: dict[str, Any]) -> str:
    return f"code {t.get('code_version_open', '?')} / config {t.get('config_version_open', '?')}"


# ---------------------------------------------------------------- reports
class Reporter:
    def __init__(self, engine: Any, paths: Paths) -> None:
        self.e = engine
        self.store = engine.store
        self.cfg = engine.cfg
        self.paths = paths

    def _equity_rows(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return self.store.query("SELECT ts_ms, equity, peak, drawdown_pct, data FROM equity_log WHERE ts_ms >= ? AND ts_ms < ? "
                                "ORDER BY id", [start_ms, end_ms])

    # ------------------------------------------------------------ daily
    def daily(self) -> str:
        now = self.e.now()
        day = utc_day(now)
        dec = self.store.latest("decisions", "utc_day >= ? AND utc_day < ? AND score IS NOT NULL",
                                [day.isoformat(), (day + timedelta(days=1)).isoformat()]) or \
            self.store.latest("decisions", "score IS NOT NULL")
        lines = [f"BTC-PERP daily report - {hkt_date(now).isoformat()} (HKT)"]
        if dec:
            d = dec["data"]
            sc = d.get("score", {})
            lines.append(f"Decision {dec['utc_day']}: score {_fmt(sc.get('score'))} "
                         f"(trend {_fmt(sc.get('trend_component'))}, breakout {_fmt(sc.get('breakout_component'))}, "
                         f"CLV {_fmt(sc.get('clv_component'))}; CLV={_fmt(sc.get('clv'), 3)}) "
                         f"tier {_fmt(sc.get('tier_fraction'))}")
            gates = d.get("gates", {})
            gl = []
            for name, g in gates.items():
                flag = "TRIGGERED" if g.get("triggered") else "ok"
                det = g.get("detail", {})
                extra = det.get("regime") or det.get("trend") or (
                    f"pct {_fmt(det.get('percentile'))}" if "percentile" in det else "") or (
                    ",".join(e["type"] for e in det.get("events", [])) if det.get("events") else "")
                gl.append(f"{name}: {flag}{(' (' + extra + ')') if extra else ''}")
            lines.append("Gates: " + "; ".join(gl))
            lines.append(f"Decision: {dec['action']} - {dec['reason']}")
        else:
            lines.append("No decision recorded yet.")
        lines.append(self.e.status_text())
        warns = self.e.calendar.coverage_warnings(day, int(self.cfg.gates.calendar_coverage_warn_days))
        for w in warns:
            lines.append("CALENDAR: " + w)
        nxt = self.e.calendar.upcoming(now, 14)
        if nxt:
            lines.append("Next events: " + ", ".join(f"{e.type} {fmt_hkt(e.release_utc)}" for e in nxt[:5]))
        misses = [a for a in audit(self.store, self.cfg, now - timedelta(days=1), now) if a["status"] != "ok"]
        if misses:
            lines.append("Missed/late runs (24h): " + ", ".join(f"{a['command']} {a['slot_hkt']} {a['status']}" for a in misses))
        text = "\n".join(lines)
        prev = day - timedelta(days=1)
        out = self.paths.exports_dir / "daily" / prev.isoformat()
        self.store.export_csv(out, day_start_ms(prev), day_start_ms(day))
        self.e.tg.send(text)
        return text + f"\nCSV export: {out}"

    # ------------------------------------------------------------ weekly
    def weekly(self) -> str:
        now = self.e.now()
        days = int(self.cfg.reports.weekly_days)
        start = now - timedelta(days=days)
        trades = [t for t in self.e.rec.closed_trades() if int(t["closed_ts_ms"]) >= to_ms(start)]
        st = trade_stats(trades)
        eq = self._equity_rows(to_ms(start), to_ms(now) + 1)
        mdd = max_drawdown([r["equity"] for r in eq])
        lines = [f"BTC-PERP weekly report {fmt_hkt(start)[:10]} - {fmt_hkt(now)[:10]}",
                 f"Trades: {st.get('trades', 0)} | win rate {_fmt(st.get('win_rate_pct'))}% | net PnL {_fmt(st.get('net_pnl'))}",
                 f"Fees {_fmt(st.get('fees'))} | funding {_fmt(st.get('funding'))} | max drawdown {mdd:.2f}%"]
        for t in trades:
            lines.append(f" - {t.get('entry_utc_day')} {'LONG' if t['direction'] > 0 else 'SHORT'} {t.get('exit_reason')} "
                         f"net {_fmt(t.get('net_pnl'))} ({_fmt(t.get('r_multiple'))} R)")
        text = "\n".join(lines)
        stamp = now.strftime("%Y%m%d")
        tmp = self.paths.exports_dir / "weekly" / f"csv_{stamp}"
        self.store.export_csv(tmp)
        zpath = self.paths.exports_dir / "weekly" / f"btcperp_logs_{stamp}.zip"
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(tmp.glob("*.csv")):
                z.write(p, arcname=p.name)
        shutil.rmtree(tmp, ignore_errors=True)
        self.e.tg.send(text)
        self.e.tg.send_document(zpath, caption=f"btcperp logs (all tables, CSV) {stamp}")
        return text + f"\nZIP: {zpath}"

    # ------------------------------------------------------------ monthly
    def monthly(self, month: str | None = None) -> str:
        now = self.e.now()
        if month:
            y, m = (int(x) for x in month.split("-"))
        else:
            first_this = date(now.year, now.month, 1)
            prev = first_this - timedelta(days=1)
            y, m = prev.year, prev.month
        start_d = date(y, m, 1)
        end_d = date(y + (m == 12), (m % 12) + 1, 1)
        start_ms, end_ms = day_start_ms(start_d), day_start_ms(end_d)
        trades = [t for t in self.e.rec.closed_trades() if start_ms <= int(t["closed_ts_ms"]) < end_ms]
        report: dict[str, Any] = {"month": f"{y:04d}-{m:02d}", "generated_utc": now.isoformat(), "by_version": {}}
        versions = sorted({version_key(t) for t in trades}) or ["(no closed trades)"]
        sl_r = float(self.cfg.exits.sl_atr_multiple)
        tp_r = float(self.cfg.exits.tp_atr_multiple) / sl_r
        for v in versions:
            vt = [t for t in trades if version_key(t) == v]
            mae = [t["mae_r"] for t in vt if t.get("mae_r") is not None]
            mfe = [t["mfe_r"] for t in vt if t.get("mfe_r") is not None]
            slip = [t["slippage_bps_vs_decision_mark"] for t in vt if t.get("slippage_bps_vs_decision_mark") is not None]
            lat = [t["decision_to_fill_s"] for t in vt if t.get("decision_to_fill_s") is not None]
            mk = [x for t in vt for x in (t.get("maker_taker") or [])]
            report["by_version"][v] = {
                "overall": trade_stats(vt),
                "by_score_tier": group_by(vt, lambda t: t.get("tier_fraction")),
                "by_gate": group_by(vt, lambda t: list(t.get("gates_triggered") or ["none"])),
                "by_exit_reason": group_by(vt, lambda t: t.get("exit_reason")),
                "long_vs_short": group_by(vt, lambda t: "long" if t["direction"] > 0 else "short"),
                "mae_mfe_vs_sl_tp": {
                    "sl_distance_r": 1.0, "tp_distance_r": tp_r,
                    "avg_mae_r": (sum(mae) / len(mae)) if mae else None, "avg_mfe_r": (sum(mfe) / len(mfe)) if mfe else None,
                    "share_mae_ge_0_5r": (sum(1 for x in mae if x >= 0.5) / len(mae)) if mae else None,
                    "share_mfe_reached_tp": (sum(1 for x in mfe if x >= tp_r) / len(mfe)) if mfe else None,
                    "share_mfe_ge_1r_but_lost": (sum(1 for t in vt if (t.get("mfe_r") or 0) >= 1 and (t.get("net_pnl") or 0) < 0)
                                                / len(vt)) if vt else None,
                },
                "slippage_fill_quality": {
                    "avg_slippage_bps_vs_decision_mark": (sum(slip) / len(slip)) if slip else None,
                    "max_slippage_bps": max(slip) if slip else None,
                    "avg_decision_to_fill_s": (sum(lat) / len(lat)) if lat else None,
                    "maker_fills": mk.count("maker"), "taker_fills": mk.count("taker"),
                },
                "funding_vs_holding": {
                    "total_funding": sum(float(t.get("funding") or 0) for t in vt),
                    "total_holding_hours": sum(float(t.get("holding_hours") or 0) for t in vt),
                    "funding_per_holding_day": (sum(float(t.get("funding") or 0) for t in vt)
                                                / (sum(float(t.get("holding_hours") or 0) for t in vt) / 24.0))
                    if sum(float(t.get("holding_hours") or 0) for t in vt) else None,
                    "by_holding_bucket": group_by(vt, lambda t: "<1d" if (t.get("holding_hours") or 0) < 24 else
                                                  "1-3d" if (t.get("holding_hours") or 0) < 72 else ">3d"),
                },
            }
        # FOK / entry fill quality from intent events
        ev = self.store.query("SELECT step, status, config_version, code_version FROM intent_events WHERE ts_ms >= ? AND ts_ms < ?",
                              [start_ms, end_ms])
        report["entry_attempts"] = {
            "attempts": sum(1 for e in ev if e["step"] == "entry_attempt" and e["status"] == "started"),
            "unfilled": sum(1 for e in ev if e["step"] == "entry_attempt" and e["status"] == "unfilled"),
            "entries_failed": sum(1 for e in ev if e["step"] == "entry" and e["status"] == "failed"),
            "entries_missed": sum(1 for e in ev if e["step"] == "entry" and e["status"] == "missed"),
        }
        # shadow vs live
        live_r = sum(float(t.get("r_multiple") or 0) for t in trades)
        shadow = {}
        for vname in ("live_rules", "v2_breakeven", "flat_allowed", "ungated"):
            snap = self.store.latest("shadow_log", "kind='variant_snapshot' AND variant=?", [vname])
            if snap:
                ts = [t for t in snap["data"].get("trades", []) if start_ms <= int(t.get("exit_ts_ms") or 0) < end_ms]
                shadow[vname] = {"trades": len(ts), "total_r": sum(float(t.get("r") or 0) for t in ts),
                                 "wins": sum(1 for t in ts if (t.get("r") or 0) > 0),
                                 "code_version": snap["code_version"], "config_version": snap["config_version"]}
        gates = self.store.query("SELECT data, code_version, config_version FROM shadow_log WHERE kind='gate_trade'")
        gm = [g for g in gates if start_d.isoformat() <= g["data"]["utc_day"] < end_d.isoformat()]
        by_gate: dict[str, dict[str, float]] = defaultdict(lambda: {"days": 0, "r_ungated": 0.0, "r_live_equivalent": 0.0})
        for g in gm:
            for name in g["data"].get("gates") or ["unknown"]:
                by_gate[name]["days"] += 1
                by_gate[name]["r_ungated"] += float(g["data"].get("r_ungated") or 0)
                by_gate[name]["r_live_equivalent"] += float(g["data"].get("r_live_equivalent") or 0)
        report["shadow_vs_live"] = {"live_total_r": live_r, "variants": shadow, "gate_shadow_by_gate": dict(by_gate)}
        # runs
        aud = audit(self.store, self.cfg, datetime(y, m, 1, tzinfo=UTC), datetime(end_d.year, end_d.month, 1, tzinfo=UTC))
        errs = self.store.query("SELECT command, error, ts_utc, code_version FROM runs WHERE event='end' AND status='error' "
                                "AND ts_ms >= ? AND ts_ms < ?", [start_ms, end_ms])
        report["runs"] = {"slots": len(aud), "missed": [a for a in aud if a["status"] == "missed"],
                          "late": [a for a in aud if a["status"] == "late"], "errors": errs}
        report["decision_days"] = self._decision_days(start_d, end_d, utc_day(now), f"{y:04d}-{m:02d}")
        report["cumulative_all_vs_complete_months"] = self._all_vs_complete(end_d, utc_day(now))
        basis = sorted(abs(float(r["data"]["basis_bps"])) for r in self.store.query(
            "SELECT data FROM market_snapshots WHERE ts_ms >= ? AND ts_ms < ?", [start_ms, end_ms])
            if isinstance(r["data"], dict) and r["data"].get("basis_bps") is not None)
        report["basis_bps"] = {"samples": len(basis), "abs_p50": basis[len(basis) // 2] if basis else None,
                               "abs_p99": basis[min(len(basis) - 1, int(0.99 * (len(basis) - 1)))] if basis else None,
                               "note": "Polymarket mark vs Binance spot (review v1.3.0 R3)"}
        eq = self._equity_rows(start_ms, end_ms)
        report["equity"] = {"start": eq[0]["equity"] if eq else None, "end": eq[-1]["equity"] if eq else None,
                            "max_drawdown_pct": max_drawdown([r["equity"] for r in eq])}
        report["calendar_warnings"] = self.e.calendar.coverage_warnings(utc_day(now), int(self.cfg.gates.calendar_coverage_warn_days))
        report["calendar_check_reminder"] = "Monthly reminder: check config/calendar.yaml against the Fed and BLS schedules."
        out_dir = self.paths.reports_dir / "monthly"
        out_dir.mkdir(parents=True, exist_ok=True)
        jpath = out_dir / f"monthly_{report['month']}.json"
        jpath.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        mpath = out_dir / f"monthly_{report['month']}.md"
        mpath.write_text(self._monthly_md(report), encoding="utf-8")
        self.e.tg.send(f"Monthly report {report['month']} ready ({len(trades)} trades). "
                       f"{report['calendar_check_reminder']}")
        self.e.tg.send_document(mpath, caption=f"btcperp monthly report {report['month']}")
        return f"monthly report: {mpath}\njson: {jpath}"

    def _all_vs_complete(self, end_d: date, today: date) -> dict[str, Any]:
        """Review v1.3.0 V17: any parameter proposal shows all months and complete months only."""
        first = self.store.query("SELECT MIN(utc_day) AS d FROM decisions")
        if not first or not first[0]["d"]:
            return {"all_months": {"trades": 0}, "complete_months_only": {"trades": 0}, "incomplete_months": []}
        d0 = date.fromisoformat(first[0]["d"][:10])
        months, cur = [], date(d0.year, d0.month, 1)
        while cur < end_d:
            nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
            months.append((cur, nxt))
            cur = nxt
        incomplete = [f"{a.year:04d}-{a.month:02d}" for a, b in months
                      if self._decision_days(a, b, today, f"{a.year:04d}-{a.month:02d}", alert=False)["incomplete_month"]]
        trades = [t for t in self.e.rec.closed_trades() if int(t["closed_ts_ms"]) < day_start_ms(end_d)]

        def month_of(t: dict[str, Any]) -> str:
            return datetime.fromtimestamp(int(t["closed_ts_ms"]) / 1000, tz=UTC).strftime("%Y-%m")

        return {"all_months": trade_stats(trades),
                "complete_months_only": trade_stats([t for t in trades if month_of(t) not in incomplete]),
                "incomplete_months": incomplete}

    def _decision_days(self, start_d: date, end_d: date, today: date, month: str, alert: bool = True) -> dict[str, Any]:
        """Review v1.2.0 item 17: UTC days without an on-time decision (late close-only decisions count as missed).
        rolling_4h: 4h periods without an on-time decision; the month is incomplete above limit x 6 periods."""
        first = self.store.query("SELECT MIN(utc_day) AS d FROM decisions")
        first_d = date.fromisoformat(first[0]["d"][:10]) if first and first[0]["d"] else today
        lo, hi = max(start_d, first_d), min(end_d, today)
        days = [lo + timedelta(days=i) for i in range(max(0, (hi - lo).days))]
        rows = self.store.query("SELECT utc_day, data FROM decisions WHERE score IS NOT NULL AND utc_day >= ? AND utc_day < ?",
                                [start_d.isoformat(), end_d.isoformat()])
        on_time = {r["utc_day"] for r in rows if isinstance(r["data"], dict) and not (r["data"].get("plan") or {}).get("late")}
        limit = int(self.cfg.reports.max_missed_decision_days)
        if str(self.cfg.strategy.cadence) == "rolling_4h":
            keys = [f"{d.isoformat()}T{h:02d}:00" for d in days for h in range(0, 24, 4)]
            missed = [k for k in keys if k not in on_time]
            incomplete = len(missed) > limit * 6
            what = f"{len(missed)} four-hour periods"
        else:
            missed = [d.isoformat() for d in days if d.isoformat() not in on_time]
            incomplete = len(missed) > limit
            what = f"{len(missed)} days"
        if incomplete and alert:
            shown = ", ".join(missed[:20]) + (" ..." if len(missed) > 20 else "")
            self.e.alert("incomplete month", f"{month}: no on-time decision in {what} ({shown}); "
                         f"the month is marked incomplete - do not judge the strategy on it",
                         dedupe_key=f"incomplete_month:{month}")
        return {"days_counted": len(days), "missed_days": missed, "limit": limit, "incomplete_month": incomplete,
                "cadence": str(self.cfg.strategy.cadence)}

    def _monthly_md(self, r: dict[str, Any]) -> str:
        L = [f"# BTC-PERP monthly report {r['month']}", "",
             "Each version is reported separately; statistics never mix versions.", ""]
        for v, s in r["by_version"].items():
            L += [f"## {v}", "", "### Overall", "```", json.dumps(s["overall"], indent=2, default=str), "```"]
            for sec in ("by_score_tier", "by_gate", "by_exit_reason", "long_vs_short", "mae_mfe_vs_sl_tp",
                        "slippage_fill_quality", "funding_vs_holding"):
                L += [f"### {sec}", "```", json.dumps(s[sec], indent=2, default=str), "```"]
        if r.get("decision_days", {}).get("incomplete_month"):
            L = L[:3] + ["**INCOMPLETE MONTH: no on-time decision on "
                         f"{len(r['decision_days']['missed_days'])} days - do not judge the strategy on this month.**",
                         ""] + L[3:]
        for sec in ("decision_days", "cumulative_all_vs_complete_months", "basis_bps", "entry_attempts", "shadow_vs_live",
                    "equity", "runs", "calendar_warnings"):
            L += [f"## {sec}", "```", json.dumps(r[sec], indent=2, default=str), "```"]
        L += ["", r["calendar_check_reminder"]]
        return "\n".join(L) + "\n"


def backup(store: Any, paths: Paths, now: datetime, keep_daily: int = 14) -> Path:
    dest = paths.backups_dir / f"btcperp_{now.strftime('%Y%m%d_%H%M%S')}.sqlite3"
    store.backup_to(dest)
    for f in ("config/config.yaml", "config/calendar.yaml", "VERSION"):
        src = paths.root / f
        if src.exists():
            shutil.copy2(src, paths.backups_dir / f"{dest.stem}_{Path(f).name}")
    # retention: keep the newest `keep_daily` backups plus the first backup of every month
    dbs = sorted(paths.backups_dir.glob("btcperp_*.sqlite3"))
    monthly_keep = {}
    for p in dbs:
        monthly_keep.setdefault(p.name[8:14], p)
    for p in dbs[:-keep_daily]:
        if p not in monthly_keep.values():
            p.unlink(missing_ok=True)
            for side in paths.backups_dir.glob(f"{p.stem}_*"):
                side.unlink(missing_ok=True)
    return dest
