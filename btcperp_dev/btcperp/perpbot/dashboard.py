"""Local dashboard: http://127.0.0.1:<port>  (only reachable from this computer).

Read-only view of the bot's database (state, position, equity, decision, trades, alerts, runs,
calendar, shadow results). The only actions are "refresh from exchange" (runs the read-only
`snapshot` command) and "mark alerts read". The dashboard NEVER gets trading controls (review
v1.2.0 item 20): pause / kill / resume stay in the .bat shortcuts, which ask for typed confirmation.
POST requests need a per-session token and the Host header must be localhost (blocks other
websites from driving the page).
"""

from __future__ import annotations

import json
import logging
import os
import secrets as _secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from perpbot import code_version
from perpbot.records import Records
from perpbot.reports import trade_stats
from perpbot.schedule_audit import audit
from perpbot.storage import Store
from perpbot.timeutil import Clock, fmt_hkt, from_ms, to_ms

log = logging.getLogger("perpbot.dashboard")

# The page is self-contained: inline script/style, data: favicon, fetches to itself only.
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def build_summary(store: Store, cfg: Any, calendar: Any, now: datetime) -> dict[str, Any]:
    rec = Records(store)
    st = rec.state()
    eq = store.latest("equity_log")
    snap = store.latest("dash_snapshots")
    man = store.latest("manage_log")
    dec = store.latest("decisions", "score IS NOT NULL")
    last_dec = store.latest("decisions")
    open_t = rec.open_trade()
    closed = rec.closed_trades()
    rows = store.query("SELECT ts_ms, equity, peak FROM equity_log WHERE equity IS NOT NULL AND equity > 0 ORDER BY id")
    step = max(1, len(rows) // 400)
    series = [[r["ts_ms"], r["equity"], r["peak"]] for r in rows[::step]]
    if rows and (not series or series[-1][0] != rows[-1]["ts_ms"]):
        series.append([rows[-1]["ts_ms"], rows[-1]["equity"], rows[-1]["peak"]])
    alerts = store.query(
        "SELECT a.id, a.ts_hkt, a.kind, a.text, CASE WHEN d.alert_id IS NULL THEN 0 ELSE 1 END AS read "
        "FROM alerts a LEFT JOIN alert_deliveries d ON d.alert_id = a.id ORDER BY a.id DESC LIMIT 60")
    unread = store.count("alerts", "id NOT IN (SELECT alert_id FROM alert_deliveries)")
    last_runs = {}
    for r in store.query("SELECT command, event, status, error, ts_hkt, ts_ms, duration_s FROM runs ORDER BY id DESC LIMIT 400"):
        key = r["command"]
        if key not in last_runs and r["event"] == "end":
            last_runs[key] = {"ts_hkt": r["ts_hkt"], "status": r["status"], "error": r["error"], "duration_s": r["duration_s"]}
    errors = store.query("SELECT command, error, ts_hkt FROM runs WHERE event='end' AND status='error' AND ts_ms >= ? "
                         "AND command != 'snapshot' ORDER BY id DESC LIMIT 20", [to_ms(now - timedelta(days=7))])
    snap_run = store.latest("runs", "command='snapshot' AND event='end'")
    snap_error = (f"{snap_run['ts_hkt']}: {snap_run['error']}"
                  if snap_run and snap_run["status"] == "error" and (not snap or snap_run["ts_ms"] > snap["ts_ms"]) else None)
    first = store.query("SELECT MIN(ts_ms) AS t FROM runs WHERE event='start' AND command IN ('decide','manage')")
    audit_rows: list[dict[str, Any]] = []
    if first and first[0]["t"]:
        start = max(now - timedelta(hours=48), from_ms(int(first[0]["t"])))
        if start < now:
            audit_rows = [a for a in audit(store, cfg, start, now - timedelta(minutes=float(cfg.schedule.missed_tolerance_minutes)))
                          if a["status"] != "ok"]
    shadow = {}
    for v in ("live_rules", "v2_breakeven", "flat_allowed", "ungated"):
        s = store.latest("shadow_log", "kind='variant_snapshot' AND variant=?", [v])
        if s:
            d = s["data"]
            shadow[v] = {"closed_trades": d.get("closed_trades"), "total_r": d.get("total_r"), "wins": d.get("wins"),
                         "open": bool(d.get("open_trade"))}
    upcoming = [{"type": e.type, "release_hkt": fmt_hkt(e.release_utc), "note": e.note} for e in calendar.upcoming(now, 30)]
    position = None
    if snap and (not man or snap["ts_ms"] >= man["ts_ms"]):
        sd = snap["data"]
        position = {"source": "snapshot", "ts_hkt": snap["ts_hkt"], "mark": sd.get("mark"), "position": sd.get("position"),
                    "sl": sd.get("sl"), "tp": sd.get("tp"), "equity": sd.get("equity")}
    elif man:
        md = man["data"]
        position = {"source": "manage", "ts_hkt": man["ts_hkt"], "mark": md.get("mark"), "position": md.get("position"),
                    "sl": [md.get("sl")] if md.get("sl") else [], "tp": [md.get("tp")] if md.get("tp") else [],
                    "equity": md.get("equity")}
    trades = [{
        "entry_day": t.get("entry_utc_day"), "direction": t.get("direction"), "qty": t.get("qty"),
        "entry_price": t.get("entry_price"), "exit_price": t.get("exit_price"), "exit_reason": t.get("exit_reason"),
        "net_pnl": t.get("net_pnl"), "r_multiple": t.get("r_multiple"), "holding_hours": t.get("holding_hours"),
        "fees": t.get("fees_total"), "funding": t.get("funding"), "exit_utc": t.get("exit_utc"),
        "version": f"{t.get('code_version_open')}/{t.get('config_version_open')}"} for t in closed[:60]]
    return {
        "now_hkt": fmt_hkt(now), "code_version": code_version(), "config_version": cfg.config_version,
        "state": {"display": rec.display_state(), "paused": st["paused"], "reasons": st["pause_reasons"]},
        "equity": ({"equity": eq["equity"], "wallet": eq["wallet"], "upnl": eq["upnl"], "peak": eq["peak"],
                    "drawdown_pct": eq["drawdown_pct"], "ts_hkt": eq["ts_hkt"],
                    "kill": (eq["data"] or {}).get("kill", {}) if isinstance(eq["data"], dict) else {},
                    "source": (eq["data"] or {}).get("source") if isinstance(eq["data"], dict) else None}
                   if eq else None),
        "limits": {"kill_drawdown_pct": cfg.risk.kill_drawdown_pct, "kill_losing_streak_pct": cfg.risk.kill_losing_streak_pct,
                   "equity_floor_pct": cfg.risk.equity_floor_pct_of_net_funded, "risk_per_trade_pct": cfg.risk.risk_per_trade_pct,
                   "notional_cap_pct": cfg.risk.notional_cap_pct_equity, "leverage": cfg.risk.leverage,
                   "permanent_floor_pct": cfg.risk.permanent_floor_pct_of_cumulative_funded},
        "position": position,
        "open_trade": ({k: open_t.get(k) for k in ("direction", "qty", "entry_price", "entry_utc_day", "sl_price", "tp_price",
                                                  "initial_risk_usd", "score", "effective_fraction", "entry_ts_ms")}
                       if open_t else None),
        "decision": ({"utc_day": dec["utc_day"], "ts_hkt": dec["ts_hkt"], "score": dec["data"].get("score"),
                      "gates": dec["data"].get("gates"), "plan": dec["data"].get("plan"), "action": dec["action"],
                      "reason": dec["reason"], "analysis": dec["data"].get("analysis")} if dec else None),
        "last_decision_row": ({"utc_day": last_dec["utc_day"], "action": last_dec["action"], "reason": last_dec["reason"],
                               "ts_hkt": last_dec["ts_hkt"]} if last_dec else None),
        "equity_series": series,
        "trades": trades,
        "stats": trade_stats(closed),
        "alerts": alerts, "unread": unread,
        "runs": {"last": last_runs, "errors": errors, "not_ok_48h": audit_rows[-40:]},
        "calendar": {"upcoming": upcoming, "warnings": calendar.coverage_warnings(now.date(), int(cfg.gates.calendar_coverage_warn_days))},
        "shadow": shadow,
        "snapshot_ts_hkt": snap["ts_hkt"] if snap else None,
        "snapshot_error": snap_error,
    }


class DashServer(ThreadingHTTPServer):
    """Loopback-only server. On Windows SO_REUSEADDR would let a second copy bind the same port,
    so the port is bound exclusively there instead."""
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
        super().server_bind()


class DashboardState:
    def __init__(self, paths: Any, cfg: Any, calendar: Any, clock: Clock) -> None:
        self.paths, self.cfg, self.calendar, self.clock = paths, cfg, calendar, clock
        self.token = _secrets.token_hex(16)
        self.proc: subprocess.Popen[bytes] | None = None
        self.last_refresh = 0.0
        self.lock = threading.Lock()

    def store(self) -> Store:
        return Store(self.paths.db_file, self.clock, self.cfg.config_version, code_version())

    def refresh(self) -> str:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                return "busy"
            if time.monotonic() - self.last_refresh < float(self.cfg.dashboard.refresh_min_seconds):
                return "too soon"
            self.last_refresh = time.monotonic()
            self.proc = subprocess.Popen([sys.executable, "-m", "perpbot", "snapshot"], cwd=str(self.paths.root),
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return "started"


def make_handler(state: DashboardState, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
            log.debug(fmt, *args)

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", CSP)                 # review v1.2.0 item 20
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            if self.path == "/" or self.path.startswith("/?"):
                html = PAGE.replace("__TOKEN__", state.token).replace("__AUTO__", str(int(state.cfg.dashboard.auto_refresh_seconds)))
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            if self.path == "/api/summary":
                s = state.store()
                try:
                    data = build_summary(s, state.cfg, state.calendar, state.clock.now())
                finally:
                    s.close()
                busy = state.proc is not None and state.proc.poll() is None
                data["refresh_busy"] = busy
                return self._send(200, json.dumps(data, default=str).encode("utf-8"), "application/json")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok() or self.headers.get("X-Token") != state.token:
                return self._send(403, b"forbidden", "text/plain")
            if self.path == "/api/refresh":
                return self._send(200, json.dumps({"result": state.refresh()}).encode(), "application/json")
            if self.path == "/api/alerts/read":
                s = state.store()
                try:
                    for a in s.query("SELECT id FROM alerts WHERE id NOT IN (SELECT alert_id FROM alert_deliveries)"):
                        s.insert_ignore("alert_deliveries", alert_id=a["id"])
                finally:
                    s.close()
                return self._send(200, b'{"result":"ok"}', "application/json")
            return self._send(404, b"not found", "text/plain")

    return Handler


def serve(paths: Any, cfg: Any, calendar: Any, clock: Clock, *, port: int, open_browser: bool) -> int:
    url = f"http://127.0.0.1:{port}/"
    state = DashboardState(paths, cfg, calendar, clock)
    try:
        httpd = DashServer(("127.0.0.1", port), make_handler(state, port))
    except OSError:
        print(f"dashboard already running (or port {port} busy): {url}")
        if open_browser:
            webbrowser.open(url)
        return 0
    print(f"btcperp dashboard: {url}  (close this window or press Ctrl+C to stop)")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


PAGE = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>btcperp dashboard</title><link rel="icon" href="data:,">
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#6b7385;--line:#e3e6ec;--up:#12805c;--down:#c2352b;--warn:#b7791f;--acc:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b23;--fg:#e6e9ef;--mut:#98a1b3;--line:#2a303c;--up:#3fbf8f;--down:#ef6b61;--warn:#e0a84a;--acc:#7aa0ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI","Microsoft JhengHei",sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:2}
h1{font-size:18px;margin:0 8px 0 0}.badge{padding:3px 10px;border-radius:999px;font-weight:600;font-size:13px;border:1px solid var(--line)}
.b-open{background:rgba(47,91,211,.12);color:var(--acc)}.b-flat{color:var(--mut)}.b-paused{background:rgba(194,53,43,.12);color:var(--down)}
.mut{color:var(--mut)}button{font:inherit;padding:6px 12px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--acc)}main{padding:16px 20px;display:grid;gap:14px;grid-template-columns:repeat(12,1fr)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;min-width:0}
.c3{grid-column:span 3}.c4{grid-column:span 4}.c6{grid-column:span 6}.c8{grid-column:span 8}.c12{grid-column:span 12}
@media (max-width:1000px){.c3,.c4,.c6,.c8{grid-column:span 12}}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin:0 0 8px}
.big{font-size:26px;font-weight:700}.up{color:var(--up)}.down{color:var(--down)}.warn{color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mut);font-weight:600}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin:4px 0 8px}.bar>i{display:block;height:100%}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px}.kv div:nth-child(odd){color:var(--mut)}
.alert{padding:6px 0;border-bottom:1px solid var(--line);font-size:13px;white-space:pre-wrap;word-break:break-word}.alert.unread{font-weight:600}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;margin:0 4px 4px 0;border:1px solid var(--line)}
.pill.on{background:rgba(183,121,31,.15);color:var(--warn);border-color:transparent}.scroll{max-height:360px;overflow:auto}
svg text{fill:var(--mut);font-size:11px}
pre.ana{white-space:pre-wrap;font-family:inherit;font-size:13px;line-height:1.5;margin:10px 0 0;padding:10px;border-radius:8px;background:var(--bg)}</style></head><body>
<header><h1>btcperp</h1><span id="state" class="badge">…</span><span id="reasons" class="mut"></span>
<span style="flex:1"></span><span class="down" id="snaperr"></span><span class="mut" id="upd"></span>
<button id="refresh">從交易所更新</button><button id="readall">標記警報已讀 (<span id="unread">0</span>)</button></header>
<main>
 <section class="card c3"><h2>權益 Equity</h2><div class="big" id="equity">–</div><div class="mut" id="eqsub"></div></section>
 <section class="card c3"><h2>回撤 Drawdown</h2><div class="big" id="dd">–</div><div class="bar"><i id="ddbar"></i></div><div class="mut" id="ddsub"></div></section>
 <section class="card c3"><h2>連虧 Losing streak</h2><div class="big" id="streak">–</div><div class="bar"><i id="stbar"></i></div><div class="mut" id="stsub"></div></section>
 <section class="card c3"><h2>本金底線 Equity floor</h2><div class="big" id="floor">–</div><div class="mut" id="floorsub"></div></section>
 <section class="card c6"><h2>倉位 Position</h2><div id="pos" class="kv"></div></section>
 <section class="card c6"><h2>最新決定 Decision</h2><div id="dec"></div></section>
 <section class="card c8"><h2>權益走勢 Equity curve</h2><div id="chart"></div></section>
 <section class="card c4"><h2>統計 Stats</h2><div id="stats" class="kv"></div></section>
 <section class="card c8"><h2>交易紀錄 Trades</h2><div class="scroll"><table id="trades"></table></div></section>
 <section class="card c4"><h2>警報 Alerts</h2><div class="scroll" id="alerts"></div></section>
 <section class="card c6"><h2>排程及錯誤 Runs</h2><div id="runs"></div></section>
 <section class="card c6"><h2>經濟事件 Calendar ・ 影子追蹤 Shadow</h2><div id="cal"></div><div id="shadow" style="margin-top:10px"></div></section>
</main>
<script>
const TOKEN="__TOKEN__", AUTO=__AUTO__*1000;
const $=id=>document.getElementById(id);
const f=(x,d=2)=>x===null||x===undefined||isNaN(x)?"–":Number(x).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const STATE={flat:"空倉 flat",open:"持倉 open",pending_entry:"入場中 pending entry",pending_exit:"平倉中 pending exit",paused:"暫停 paused"};
const stateLabel=s=>STATE[s]||(s.startsWith("paused")?"暫停 "+s:s);
const dir=d=>d>0?'<span class="up">多 LONG</span>':d<0?'<span class="down">空 SHORT</span>':'–';
function bar(el,pct,limit){const w=Math.max(0,Math.min(100,pct/limit*100));el.style.width=w+"%";el.style.background=w>80?"var(--down)":w>50?"var(--warn)":"var(--up)";}
async function post(p){return fetch(p,{method:"POST",headers:{"X-Token":TOKEN}}).then(r=>r.json()).catch(()=>({}))}
function chart(series){
 if(!series.length){$("chart").innerHTML='<div class="mut">未有資料</div>';return}
 const W=760,H=220,p=34,xs=series.map(s=>s[0]),ys=series.flatMap(s=>[s[1],s[2]||s[1]]);
 const x0=Math.min(...xs),x1=Math.max(...xs)||x0+1,y0=Math.min(...ys)*0.995,y1=Math.max(...ys)*1.005;
 const X=v=>p+(W-2*p)*((v-x0)/((x1-x0)||1)),Y=v=>H-p+(-(H-2*p))*((v-y0)/((y1-y0)||1));
 const line=(i,c,dash)=>'<polyline fill="none" stroke="'+c+'" stroke-width="'+(dash?1:2)+'" '+(dash?'stroke-dasharray="4 4"':'')+' points="'+series.map(s=>X(s[0]).toFixed(1)+","+Y(s[i]||s[1]).toFixed(1)).join(" ")+'"/>';
 const t=v=>new Date(v).toLocaleDateString();
 $("chart").innerHTML='<svg viewBox="0 0 '+W+' '+H+'" width="100%">'+
  '<text x="4" y="'+(Y(y1)+4)+'">'+f(y1,0)+'</text><text x="4" y="'+(Y(y0))+'">'+f(y0,0)+'</text>'+
  '<text x="'+p+'" y="'+(H-8)+'">'+t(x0)+'</text><text x="'+(W-p-70)+'" y="'+(H-8)+'">'+t(x1)+'</text>'+
  line(2,"var(--mut)",true)+line(1,"var(--acc)",false)+'</svg><div class="mut">實線 權益 ・ 虛線 高水位</div>';
}
function render(d){
 const st=d.state;$("state").textContent=stateLabel(st.display);$("state").className="badge "+(st.paused?"b-paused":st.display==="open"?"b-open":"b-flat");
 $("reasons").textContent=st.reasons.length?("暫停原因: "+st.reasons.join(", ")):"";
 $("upd").textContent="更新: "+d.now_hkt+(d.snapshot_ts_hkt?" ・ 交易所快照: "+d.snapshot_ts_hkt:"")+(d.refresh_busy?" ・ 更新中…":"")+" ・ v"+d.code_version+"/cfg "+d.config_version;
 $("snaperr").textContent=d.snapshot_error?("交易所讀取失敗 "+d.snapshot_error):"";
 $("unread").textContent=d.unread;
 const e=d.equity,L=d.limits,k=(e&&e.kill)||{};
 $("equity").textContent=e?f(e.equity):"–";$("eqsub").textContent=e?("錢包 "+f(e.wallet)+" ・ 未實現 "+f(e.upnl)+" ・ 高水位 "+f(e.peak)):"未有資料";
 $("dd").textContent=e?f(e.drawdown_pct)+"%":"–";if(e)bar($("ddbar"),e.drawdown_pct||0,L.kill_drawdown_pct);$("ddsub").textContent="殺停線 "+L.kill_drawdown_pct+"%";
 $("streak").textContent=f(k.losing_streak_pct||0)+"%";bar($("stbar"),k.losing_streak_pct||0,L.kill_losing_streak_pct);$("stsub").textContent=(k.losing_streak_trades||0)+" 筆連虧 ・ 停新倉線 "+L.kill_losing_streak_pct+"%";
 $("floor").textContent=k.equity_floor?f(k.equity_floor):"–";$("floorsub").textContent="淨投入本金 "+f(k.net_funded)+" 的 "+L.equity_floor_pct+"% ・ 跌穿即硬停"+(k.permanent_floor?" ・ 永久底線 "+f(k.permanent_floor)+"（累計投入 "+f(k.cum_funded)+" 的 "+L.permanent_floor_pct+"%）":"");
 const P=d.position,T=d.open_trade;let h="";
 if(P&&P.position){const p=P.position;h+="<div>方向</div><div>"+dir(p.size>0?1:-1)+"</div><div>數量</div><div>"+f(Math.abs(p.size),4)+" BTC</div>"+
  "<div>入場價</div><div>"+f(p.entry_price)+"</div><div>標記價</div><div>"+f(P.mark)+"</div>"+
  "<div>未實現盈虧</div><div class='"+(p.unrealized_pnl>=0?"up":"down")+"'>"+f(p.unrealized_pnl)+"</div>"+
  "<div>止損 SL</div><div>"+(P.sl&&P.sl.length?P.sl.map(x=>f(x)).join(", "):"<b class='down'>無！</b>")+"</div>"+
  "<div>止盈 TP</div><div>"+(P.tp&&P.tp.length?P.tp.map(x=>f(x)).join(", "):"–")+"</div>"+
  "<div>強平價</div><div>"+f(p.liquidation_price)+"</div><div>累計資金費</div><div>"+f(p.cumulative_funding,4)+"</div>";
  if(T)h+="<div>入場日 (UTC)</div><div>"+esc(T.entry_utc_day)+"</div><div>風險 (到止損)</div><div>"+f(T.initial_risk_usd)+"</div>";
  h+="<div>資料來源</div><div class='mut'>"+esc(P.source)+" "+esc(P.ts_hkt)+"</div>";}
 else h="<div>倉位</div><div>空倉 flat</div>"+(P?"<div>標記價</div><div>"+f(P.mark)+"</div><div>資料時間</div><div class='mut'>"+esc(P.source)+" "+esc(P.ts_hkt)+"</div>":"");
 $("pos").innerHTML=h;
 const D=d.decision;if(D&&D.score){const s=D.score,pl=D.plan||{},gs=D.gates||{};
  const comp=(n,v,m)=>"<div class='kv'><div>"+n+"</div><div>"+f(v)+"</div></div><div class='bar'><i style='width:"+Math.min(100,Math.abs(v)/m*100)+"%;background:"+(v>=0?"var(--up)":"var(--down)")+"'></i></div>";
  $("dec").innerHTML="<div class='kv'><div>決定時段 (UTC)</div><div>"+esc(D.utc_day)+" <span class='mut'>"+esc(D.ts_hkt)+"</span></div><div>分數</div><div class='big "+(s.score>0?"up":s.score<0?"down":"")+"'>"+f(s.score)+"</div>"+
   "<div>方向 / 注碼級別</div><div>"+dir(s.direction)+" ・ "+f(s.tier_fraction*100,0)+"%</div><div>行動</div><div><b>"+esc(D.action)+"</b></div></div>"+
   comp("趨勢 Trend",s.trend_component,50)+comp("突破 Breakout",s.breakout_component,25)+comp("收市位置 CLV",s.clv_component,25)+
   "<div>"+Object.entries(gs).map(([n,g])=>"<span class='pill "+(g.triggered?"on":"")+"'>"+esc(n)+(g.triggered?" ✓":"")+"</span>").join("")+"</div>"+
   "<div class='mut'>"+esc(D.reason)+"</div>"+(D.analysis?"<pre class='ana'>"+esc(D.analysis.join("\n"))+"</pre>":"");}
 else $("dec").innerHTML="<div class='mut'>未有決定紀錄"+(d.last_decision_row?(" ・ 最後: "+esc(d.last_decision_row.action)+" "+esc(d.last_decision_row.reason)):"")+"</div>";
 chart(d.equity_series);
 const S=d.stats;$("stats").innerHTML=S&&S.trades?("<div>交易數</div><div>"+S.trades+"</div><div>勝率</div><div>"+f(S.win_rate_pct,1)+"%</div><div>淨盈虧</div><div class='"+(S.net_pnl>=0?"up":"down")+"'>"+f(S.net_pnl)+"</div>"+
  "<div>總 R</div><div>"+f(S.total_r)+"</div><div>期望值 (R)</div><div>"+f(S.size_weighted_expectancy_r,3)+"</div><div>手續費</div><div>"+f(S.fees)+"</div><div>資金費</div><div>"+f(S.funding)+"</div><div>平均持倉 (小時)</div><div>"+f(S.avg_holding_hours,1)+"</div>")
  :"<div>交易數</div><div>0</div>";
 $("trades").innerHTML="<tr><th>入場日</th><th>方向</th><th class=num>入場</th><th class=num>出場</th><th>原因</th><th class=num>淨盈虧</th><th class=num>R</th><th class=num>小時</th></tr>"+
  d.trades.map(t=>"<tr><td>"+esc(t.entry_day)+"</td><td>"+dir(t.direction)+"</td><td class=num>"+f(t.entry_price)+"</td><td class=num>"+f(t.exit_price)+"</td><td>"+esc(t.exit_reason)+"</td><td class='num "+(t.net_pnl>=0?"up":"down")+"'>"+f(t.net_pnl)+"</td><td class=num>"+f(t.r_multiple)+"</td><td class=num>"+f(t.holding_hours,1)+"</td></tr>").join("");
 $("alerts").innerHTML=d.alerts.length?d.alerts.map(a=>"<div class='alert "+(a.read?"":"unread")+"'><span class='mut'>"+esc(a.ts_hkt)+"</span> "+esc(a.text.replace(/^\[btcperp\] /,"").replace(/\n\(\d{4}-\d\d-\d\d [\d:]+ HKT\)$/,""))+"</div>").join(""):"<div class='mut'>冇警報</div>";
 const R=d.runs;$("runs").innerHTML="<table><tr><th>指令</th><th>上次完成</th><th>結果</th></tr>"+Object.entries(R.last).map(([c,r])=>"<tr><td>"+esc(c)+"</td><td>"+esc(r.ts_hkt)+"</td><td class='"+(r.status==="ok"?"up":"down")+"'>"+esc(r.status)+"</td></tr>").join("")+"</table>"+
  (R.not_ok_48h.length?"<div style='margin-top:8px' class='warn'>漏跑/遲跑 (48 小時): "+R.not_ok_48h.map(a=>esc(a.command+" "+a.slot_hkt+" "+a.status)).join(", ")+"</div>":"")+
  (R.errors.length?"<div class='down' style='margin-top:6px'>錯誤 (7 日): "+R.errors.map(e=>esc(e.ts_hkt+" "+e.command+": "+(e.error||""))).join("<br>")+"</div>":"");
 const C=d.calendar;$("cal").innerHTML=(C.warnings.map(w=>"<div class='warn'>"+esc(w)+"</div>").join(""))+(C.upcoming.length?"<table>"+C.upcoming.map(e=>"<tr><td>"+esc(e.type)+"</td><td>"+esc(e.release_hkt)+"</td></tr>").join("")+"</table>":"<div class='mut'>30 日內冇事件</div>");
 const sh=d.shadow;$("shadow").innerHTML=Object.keys(sh).length?"<table><tr><th>影子版本</th><th class=num>交易</th><th class=num>勝</th><th class=num>總 R</th></tr>"+Object.entries(sh).map(([n,v])=>"<tr><td>"+esc(n)+"</td><td class=num>"+v.closed_trades+"</td><td class=num>"+v.wins+"</td><td class=num>"+f(v.total_r)+"</td></tr>").join("")+"</table>":"<div class='mut'>影子追蹤未有資料</div>";
}
async function load(){try{const d=await fetch("/api/summary").then(r=>r.json());render(d)}catch(e){$("upd").textContent="dashboard 讀取失敗: "+e}}
$("refresh").onclick=async()=>{const r=await post("/api/refresh");$("upd").textContent="交易所更新: "+(r.result||"?");setTimeout(load,8000)};
$("readall").onclick=async()=>{await post("/api/alerts/read");load()};
load();setInterval(load,30000);post("/api/refresh");setInterval(()=>post("/api/refresh"),AUTO);
</script></body></html>
"""
