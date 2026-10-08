"""v2.3.0 fair comparison of the intraday rules with and without the forecast (owner 2026-10-08: "使用原有
intraday_bt.py 框架，確保新舊策略比較公平 ... A. v2.2.0 原版 B. + Forecast Engine C. + Forecast + Dynamic Exit D. v2.0.0 /
v2.1.0 / v2.2.0 歷史設定 ... 必須計入交易成本 ... 時間順序 Out-of-Sample / Walk-Forward ... 唔可以只挑選賺錢時段").

Every variant runs on the same candles, the same costs, the same sizing and the same start equity with
intraday_bt.Sim (the live functions). The forecast is the same function the live bot logs (finished samples only).
No value was tuned on the compared period: the dynamic-exit and warning values were fixed before the first run (see
CHANGELOG 2.3.0). Every chronological segment is reported.

Variants: A = live rules (v2.2.0 values); B = A + forecast entry filter; C = B + dynamic exit; C0 = A + dynamic exit
(its effect alone); E = C + early-reversal entries (the owner: "高位轉頭做空，低位反彈做多"). Profiles D: the v2.0.0 and
v2.1.0 values with the live rules (variant A).
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Sequence

from perpbot import intraday_bt as ibt
from perpbot.config import config_from_dict

COMPARE_SCENARIOS = {"live_like": (4.0, 1.0), "base": (4.0, 5.0), "stress": (8.0, 15.0)}

# historical value sets (the v2.0.0 / v2.1.0 shipped `intraday` values that later versions changed)
PROFILES: dict[str, dict[str, Any]] = {
    "v2.2.0": {},
    "v2.1.0": dict(leg_min_atr=1.0, retrace_min=0.236, retrace_max=0.786, pullback_max_bars=12, reversal_window_hours=12,
                   retest_zone_atr=0.5, reclaim_atr=0.5, range_enabled=True, range_min_atr=2.0, range_edge_atr=0.35,
                   sl_min_atr1h=0.75, sl_min_pct=0.3, max_cost_r=0.35, room_recent_bars=8, cooldown_bars=1,
                   max_entries_per_day=12, max_entries_per_leg=2, tp1_r=1.0, trigger_clv_min=0.0,
                   trigger_beyond="high_low", min_room_r=1.0, range_latest_edge=False, max_hold_hours=12,
                   no_progress_hours=6),
    "v2.0.0": dict(leg_min_atr=1.5, retrace_min=0.382, pullback_max_bars=8, reversal_window_hours=6, retest_zone_atr=0.25,
                   reclaim_atr=0.25, range_enabled=False, sl_min_atr1h=1.0, sl_min_pct=0.5, max_cost_r=0.20,
                   room_recent_bars=16, cooldown_bars=2, max_entries_per_day=6, max_entries_per_leg=1,
                   retrace_max=0.786, tp1_r=1.0, trigger_clv_min=0.0, trigger_beyond="high_low", range_min_atr=2.0,
                   range_edge_atr=0.35, min_room_r=1.0, range_latest_edge=False, max_hold_hours=12,
                   no_progress_hours=6),
}


def profile_cfg(cfg: Any, name: str) -> Any:
    d = copy.deepcopy(cfg.to_dict())
    d["intraday"].update(PROFILES[name])
    return config_from_dict(d)


def _sim(cfg: Any, data: ibt.Data, scenario: str, sizing: str, equity: float, variant: str, base: str,
         pre: dict[int, Any]) -> ibt.Sim:
    return ibt.Sim(cfg, data, scenario, sizing, equity, variant=ibt.VARIANTS[variant], base=base, forecasts=pre)


def compare(cfg: Any, data: ibt.Data, start_ms: int, end_ms: int, *, base: str = "15m", equity: float = 100.0,
            variants: Sequence[str] = ("A", "B", "C", "C0", "E"), scenarios: Sequence[str] = ("live_like", "base", "stress"),
            sizings: Sequence[str] = ("owner", "risk3", "risk1"), segments: int = 3,
            progress: Any = None) -> dict[str, Any]:
    pre = ibt.precompute_forecasts(cfg, data, start_ms, end_ms, base)
    days = (end_ms - start_ms) / ibt.DAY
    out: dict[str, Any] = {"period": [start_ms, end_ms], "days": days, "base": base, "equity": equity,
                           "approximations": list(ibt.APPROXIMATIONS) + ([
                               "PROXY: the 15-minute rules run on 1h candles (pullback windows, triggers and stops count "
                               "1h candles) because 15m history is not available for this period; compare variants with "
                               "each other, not with live results."] if base == "1h" else []),
                           "runs": {}, "segments": {}, "profiles": {}}
    for v in variants:
        for sc in scenarios:
            for sz in sizings:
                res = _sim(cfg, data, sc, sz, equity, v, base, pre).run(start_ms, end_ms)
                st = ibt.stats(res)
                st["trades_per_day"] = st["trades"] / days if days else 0.0
                out["runs"][f"{v}/{sc}/{sz}"] = st
                if progress:
                    progress(f"{v}/{sc}/{sz}: {st['trades']} trades {st['net_return_pct']:+.1f}%")
    cuts = [start_ms + (end_ms - start_ms) * k // segments for k in range(segments + 1)]
    for k in range(segments):
        a, b = cuts[k], cuts[k + 1]
        for v in variants:
            for sz in ("owner", "risk1"):
                if sz not in sizings:
                    continue
                st = ibt.stats(_sim(cfg, data, "base", sz, equity, v, base, pre).run(a, b))
                out["segments"][f"{k + 1}/{v}/{sz}"] = {"from": a, "to": b, **st}
    for name in PROFILES:
        if name == "v2.2.0":
            continue
        pc = profile_cfg(cfg, name)
        for sc in scenarios:
            st = ibt.stats(_sim(pc, data, sc, "owner", equity, "A", base, pre).run(start_ms, end_ms))
            st["trades_per_day"] = st["trades"] / days if days else 0.0
            out["profiles"][f"{name}/{sc}/owner"] = st
    return out


def _d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _f(x: Any, fmt: str) -> str:
    if x is None:
        return "-"
    if x == float("inf"):
        return "inf"
    return format(x, fmt)


ROW_HEAD = ("| run | net % | max DD % | PF | win % | avg win USD | avg loss USD | avg R | trades/day | long/short | "
            "BTC move captured USD | captured >=500/1000/2000 (available) | avg give-back USD |")
ROW_SEP = "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"


def row(name: str, s: dict[str, Any]) -> str:
    cap, av = s.get("captured") or {}, s.get("available") or {}
    caps = "/".join(str(cap.get(k, 0)) for k in ("500", "1000", "2000"))
    avs = "/".join(str(av.get(k, 0)) for k in ("500", "1000", "2000"))
    return (f"| {name} | {s['net_return_pct']:+.1f} | {s['max_drawdown_pct']:.1f} | {_f(s.get('profit_factor'), '.2f')} | "
            f"{_f(s.get('win_rate_pct'), '.0f')} | {_f(s.get('avg_win_usd'), '+.2f')} | {_f(s.get('avg_loss_usd'), '+.2f')} | "
            f"{_f(s.get('avg_net_r'), '+.3f')} | {_f(s.get('trades_per_day'), '.2f')} | {s.get('longs', 0)}/{s.get('shorts', 0)} | "
            f"{_f(s.get('avg_captured_usd'), '+.0f')} | {caps} ({avs}) | {_f(s.get('avg_giveback_usd'), '.0f')} |")


def report_md(rep: dict[str, Any], title: str) -> str:
    a, b = rep["period"]
    L = [f"# {title}", "", f"{_d(a)} -> {_d(b)} ({rep['days']:.0f} days), decisions on {rep['base']} candles, start "
         f"{rep['equity']:g} USD each run (compounding). Costs per side: live_like fee 4 + slippage 1 bp, base 4 + 5, "
         "stress 8 + 15. Sizing: owner = config map x3-x20; risk3 = 3% of equity lost at the stop (<= x10); risk1 = 1% "
         "(<= x5, the conservative plan).", "", "**Approximations:**", ""]
    L += [f"- {x}" for x in rep["approximations"]]
    L += ["", "Variants: A = live rules; B = A + forecast entry filter; C = B + dynamic exit; C0 = A + dynamic exit; "
          "E = C + early-reversal entries. 'captured' = BTC price move from entry to the average exit in the trade "
          "direction; 'available' = trades whose best move reached the level; give-back = best move - captured.", ""]
    runs = rep["runs"]
    for sc in sorted({k.split("/")[1] for k in runs}, key=lambda x: list(COMPARE_SCENARIOS).index(x)):
        L += [f"## Cost {sc}", "", ROW_HEAD, ROW_SEP]
        L += [row(k, v) for k, v in runs.items() if k.split("/")[1] == sc]
        L.append("")
    L += ["## Walk-forward: every chronological segment (base cost)", "", ROW_HEAD, ROW_SEP]
    for k, v in rep["segments"].items():
        L.append(row(f"seg {k} ({_d(v['from'])}..{_d(v['to'])})", v))
    L += ["", "## Long / short and market state (base cost, owner sizing)", "",
          "| run | group | trades | win % | PF | net USD | avg R |", "|---|---|---:|---:|---:|---:|---:|"]
    for k, v in runs.items():
        if k.split("/")[1:] != ["base", "owner"]:
            continue
        for sec in ("by_side", "by_regime", "by_setup"):
            for g, x in (v.get(sec) or {}).items():
                L.append(f"| {k} | {sec[3:]} {g} | {x['trades']} | {x['win_rate_pct']:.0f} | {_f(x['profit_factor'], '.2f')} | "
                         f"{x['net']:+.2f} | {x['avg_net_r']:+.3f} |")
    L += ["", "## Exits (base cost, owner sizing)", "", "| run | exit | trades | net USD | avg R |", "|---|---|---:|---:|---:|"]
    for k, v in runs.items():
        if k.split("/")[1:] != ["base", "owner"]:
            continue
        for g, x in (v.get("by_exit") or {}).items():
            L.append(f"| {k} | {g} | {x['trades']} | {x['net']:+.2f} | {x['avg_net_r']:+.3f} |")
    L += ["", "## D: older value sets with the live rules (owner sizing)", "", ROW_HEAD, ROW_SEP]
    L += [row(k, v) for k, v in rep["profiles"].items()]
    L += [row(k, v) for k, v in runs.items() if k.startswith("A/") and k.endswith("/owner")]
    L += ["", "Reading it: judge net %, drawdown and profit factor after costs, across ALL segments and cost levels. "
          "A better result in one segment or at zero cost is not evidence. More trades or a higher win rate are not "
          "goals by themselves.", ""]
    return "\n".join(L)


def to_json(rep: dict[str, Any]) -> str:
    return json.dumps(rep, indent=1, default=str)
