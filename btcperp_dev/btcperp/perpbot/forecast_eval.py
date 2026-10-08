"""v2.3.0 walk-forward check of the forecast engine (owner 2026-10-08: "預測準確率及概率校準 ... 使用時間順序
Out-of-Sample / Walk-Forward 測試 ... 唔可以只挑選賺錢時段展示結果").

Every hour in the period, in time order: the forecast is made from the samples that had finished by then
(forecast.Incremental - the same numbers the live bot would have shown), and only later compared with what happened.
Nothing is fitted, so every hour is out of sample. All periods are reported (quarters), not a chosen one.

Scores:
- ranges: share of actual prices inside the 10-90% band (80% if calibrated) and the band width, per horizon
- first passage (+-passage_atr x ATR1h within passage_hours): Brier score of P(up first) against the running
  base rate (also past-only) -> skill > 0 means better than "always the usual frequency"; reliability bins
- reach: predicted vs observed share of hours followed by an up / down move of 500 / 1,000 / 2,000 USD
- early reversal warnings: after a top (bottom) warning level 1 / 2 / 3, how often the next passage_atr move was
  DOWN (UP) first and the average move after 4 / 12 hours, against hours near a 24h high (low) without a warning
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Sequence

from perpbot import forecast as fc
from perpbot import intraday as idy
from perpbot.indicators import Candle

H1 = idy.H1_MS
BINS = [(0.0, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 1.01)]


def _q(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def _actual(fr: fc.Frame, i: int) -> dict[str, Any] | None:
    """What happened after hour i in absolute terms (up = +1)."""
    o = fr.outcomes(i, 1)
    if o is None:
        return None
    a = float(fr.atr[i] or 0.0)
    return {"fwd": {hz: v * a for hz, v in o["fwd"].items()}, "passage": o["passage"], "up": o["fav"] * a,
            "down": o["adv"] * a, "fwd4h": fr.h1[i + 4].close - fr.h1[i].close,
            "fwd12h": fr.h1[i + 12].close - fr.h1[i].close}


def walk_forward(h1: Sequence[Candle], h4: Sequence[Candle], m15: Sequence[Candle] | None, ip: idy.Params,
                 fp: fc.FParams, start_ms: int, end_ms: int) -> dict[str, Any]:
    fr = fc.Frame(h1, h4, m15, ip, fp)
    inc = fc.Incremental(fr)
    rows: list[dict[str, Any]] = []
    base_up = base_n = 0                       # running past-only base rate of "up first"
    pending: list[tuple[int, int]] = []        # (resolve_ms, passage) waiting to enter the base rate
    for i in range(len(fr.h1)):
        t = fr.close_ms[i]
        if t < start_ms or t > end_ms:
            continue
        while pending and pending[0][0] <= t:
            _, ps = pending.pop(0)
            if ps in (1, -1):
                base_up += ps == 1
                base_n += 1
        f = inc.forecast(t)
        act = _actual(fr, i)
        if f is None or act is None:
            continue
        pending.append((t + fp.outcome_hours() * H1, act["passage"]))
        rows.append({"t": t, "f": f, "a": act, "base": (base_up / base_n) if base_n else 0.5})
    return score(rows, fp)


def score(rows: list[dict[str, Any]], fp: fc.FParams) -> dict[str, Any]:
    out: dict[str, Any] = {"hours": len(rows), "from": rows[0]["t"] if rows else None, "to": rows[-1]["t"] if rows else None}
    # ranges
    rng: dict[str, Any] = {}
    for hz in fp.horizons_min:
        k = str(hz)
        hit = n = 0
        width = 0.0
        dir_hit = dir_n = 0
        scaled = 0
        for r in rows:
            band = r["f"]["ranges"].get(k) or {}
            act = r["a"]["fwd"].get(hz)
            if "low" not in band or act is None:
                continue
            px = r["f"]["price"]
            n += 1
            hit += band["low"] <= px + act <= band["high"]
            width += band["high"] - band["low"]
            scaled += bool(band.get("scaled"))
            if abs(band["mid"] - px) > 1e-9 and act != 0:
                dir_n += 1
                dir_hit += (band["mid"] - px) * act > 0
        rng[k] = {"n": n, "coverage_10_90": hit / n if n else None, "avg_width_usd": width / n if n else None,
                  "mid_direction_hit": dir_hit / dir_n if dir_n else None, "scaled_share": scaled / n if n else None}
    out["ranges"] = rng
    # first passage
    bs = bs0 = 0.0
    m = 0
    rel = [{"bin": f"{a:.1f}-{min(b, 1.0):.1f}", "n": 0, "pred": 0.0, "obs": 0.0} for a, b in BINS]
    cont_hit = cont_n = 0
    for r in rows:
        p = r["f"].get("p_up_first")
        ps = r["a"]["passage"]
        if p is None or ps not in (1, -1):
            continue
        pn = (r["f"]["p_up_first"] or 0) + (r["f"]["p_down_first"] or 0)
        pu = p / pn if pn > 0 else 0.5                      # P(up first | one side hit)
        y = 1.0 if ps == 1 else 0.0
        bs += (pu - y) ** 2
        bs0 += (r["base"] - y) ** 2
        m += 1
        for j, (a, b) in enumerate(BINS):
            if a <= pu < b:
                rel[j]["n"] += 1
                rel[j]["pred"] += pu
                rel[j]["obs"] += y
        if abs(pu - 0.5) >= 0.1:
            cont_n += 1
            cont_hit += (pu > 0.5) == (y == 1.0)
    for x in rel:
        if x["n"]:
            x["pred"] /= x["n"]
            x["obs"] /= x["n"]
    out["passage"] = {"n": m, "brier": bs / m if m else None, "brier_base": bs0 / m if m else None,
                      "skill": (1 - bs / bs0) if m and bs0 else None, "reliability": rel,
                      "called_side_n": cont_n, "called_side_hit": cont_hit / cont_n if cont_n else None}
    # reach
    reach: dict[str, Any] = {}
    for side in ("up", "down"):
        for u in fp.usd_levels:
            k = str(int(u))
            pr = ob = 0.0
            n = 0
            for r in rows:
                p = (r["f"]["reach"][side] or {}).get(k)
                if p is None:
                    continue
                pr += p
                ob += r["a"][side] >= u
                n += 1
            reach[f"{side}_{k}"] = {"n": n, "pred": pr / n if n else None, "obs": ob / n if n else None}
    out["reach"] = reach
    # warnings
    warn: dict[str, Any] = {}
    for side, s in (("top", 1), ("bottom", -1)):
        groups: dict[str, list[dict[str, Any]]] = {"none_near": [], "1": [], "2": [], "3": [], "all": []}
        for r in rows:
            w = r["f"]["warnings"][side]
            lv = int(w["level"])
            near = r["f"]["near_high"] if s > 0 else r["f"]["near_low"]
            groups["all"].append(r)
            if lv:
                groups[str(lv)].append(r)
            elif near:
                groups["none_near"].append(r)
        res = {}
        for g, rs in groups.items():
            pas = [x["a"]["passage"] for x in rs if x["a"]["passage"] in (1, -1)]
            against = sum(1 for p in pas if p == -s)          # top: down first
            res[g] = {"n": len(rs), "p_reverse_first": against / len(pas) if pas else None,
                      "avg_4h_usd_with_reversal": (sum(-s * x["a"]["fwd4h"] for x in rs) / len(rs)) if rs else None,
                      "avg_12h_usd_with_reversal": (sum(-s * x["a"]["fwd12h"] for x in rs) / len(rs)) if rs else None,
                      "reversal_500": (sum(1 for x in rs if x["a"]["down" if s > 0 else "up"] >= 500) / len(rs)) if rs else None,
                      "reversal_1000": (sum(1 for x in rs if x["a"]["down" if s > 0 else "up"] >= 1000) / len(rs)) if rs else None,
                      "continue_500": (sum(1 for x in rs if x["a"]["up" if s > 0 else "down"] >= 500) / len(rs)) if rs else None}
        warn[side] = res
    out["warnings"] = warn
    # quarters (all of them)
    quarters: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        quarters.setdefault(_q(r["t"]), []).append(r)
    out["quarters"] = {}
    for q, rs in sorted(quarters.items()):
        sub = score_small(rs, fp)
        out["quarters"][q] = sub
    regimes: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        regimes.setdefault(r["f"]["regime"], []).append(r)
    out["regimes"] = {k: score_small(v, fp) for k, v in sorted(regimes.items())}
    return out


def score_small(rows: list[dict[str, Any]], fp: fc.FParams) -> dict[str, Any]:
    bs = bs0 = 0.0
    m = 0
    cov = n = 0
    tw = tw_rev = 0
    for r in rows:
        p = r["f"].get("p_up_first")
        ps = r["a"]["passage"]
        if p is not None and ps in (1, -1):
            pn = (r["f"]["p_up_first"] or 0) + (r["f"]["p_down_first"] or 0)
            pu = p / pn if pn > 0 else 0.5
            y = 1.0 if ps == 1 else 0.0
            bs += (pu - y) ** 2
            bs0 += (r["base"] - y) ** 2
            m += 1
        band = r["f"]["ranges"].get("60") or {}
        act = r["a"]["fwd"].get(60)
        if "low" in band and act is not None:
            n += 1
            cov += band["low"] <= r["f"]["price"] + act <= band["high"]
        w = r["f"]["warnings"]["top"]
        if int(w["level"]) >= 1 and ps in (1, -1):
            tw += 1
            tw_rev += ps == -1
    return {"hours": len(rows), "skill": (1 - bs / bs0) if m and bs0 else None,
            "coverage_60": cov / n if n else None, "top_warnings": tw,
            "top_warning_down_first": tw_rev / tw if tw else None}


def _pct(x: Any) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def _num(x: Any, fmt: str = ".0f") -> str:
    return "-" if x is None else format(x, fmt)


def _skill(x: Any) -> str:
    return "-" if x is None else f"{x * 100:+.2f}%"


def report_md(res: dict[str, Any], title: str) -> str:
    def d(ms: Any) -> str:
        return "-" if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    L = [f"# {title}", "", f"Hours checked: {res['hours']} ({d(res['from'])} -> {d(res['to'])}). Every forecast used only "
         "outcomes finished before it (walk-forward, nothing fitted).", "", "## Price ranges (10-90% band)", "",
         "| horizon | n | inside band (target 80%) | avg width USD | middle got the direction | scaled from 60 min |",
         "|---|---:|---:|---:|---:|---:|"]
    for hz, v in res["ranges"].items():
        L.append(f"| {hz} min | {v['n']} | {_pct(v['coverage_10_90'])} | "
                 f"{_num(v['avg_width_usd'])} | {_pct(v['mid_direction_hit'])} | "
                 f"{_pct(v['scaled_share'])} |")
    p = res["passage"]
    L += ["", "## Which way first (1 x ATR1h within 12 h)", "",
          f"Brier {_num(p['brier'], '.4f')} vs base rate {_num(p['brier_base'], '.4f')}"
          f" -> skill {_skill(p['skill'])} (0 = no better than the usual frequency). "
          f"When it leaned at least 60/40: right {_pct(p['called_side_hit'])} of {p['called_side_n']} hours.", "",
          "| P(up first) bin | n | predicted | happened |", "|---|---:|---:|---:|"]
    L += [f"| {x['bin']} | {x['n']} | {_pct(x['pred'] if x['n'] else None)} | {_pct(x['obs'] if x['n'] else None)} |"
          for x in p["reliability"]]
    L += ["", "## Reach 500 / 1,000 / 2,000 USD within 12 h", "", "| move | n | predicted | happened |", "|---|---:|---:|---:|"]
    L += [f"| {k} | {v['n']} | {_pct(v['pred'])} | {_pct(v['obs'])} |" for k, v in res["reach"].items()]
    for side in ("top", "bottom"):
        zh = "高位（頂）" if side == "top" else "低位（底）"
        L += ["", f"## Early reversal warning: {side} {zh}", "",
              "| group | hours | reversal move first | avg move 4 h (reversal side) | avg 12 h | reversal >= 500 | >= 1,000 | "
              "continued >= 500 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
        names = {"all": "all hours", "none_near": "near the extreme, no warning", "1": "level 1 warning",
                 "2": "level 2 preparation", "3": "level 3 confirmed"}
        for g in ("all", "none_near", "1", "2", "3"):
            v = res["warnings"][side][g]
            f4 = v["avg_4h_usd_with_reversal"]
            f12 = v["avg_12h_usd_with_reversal"]
            L.append(f"| {names[g]} | {v['n']} | {_pct(v['p_reverse_first'])} | {_num(f4, '+.0f')} | "
                     f"{_num(f12, '+.0f')} | {_pct(v['reversal_500'])} | {_pct(v['reversal_1000'])} | "
                     f"{_pct(v['continue_500'])} |")
    L += ["", "## Every quarter (no period left out)", "",
          "| quarter | hours | skill | 60-min band coverage | top warnings | of them down first |", "|---|---:|---:|---:|---:|---:|"]
    for q, v in res["quarters"].items():
        L.append(f"| {q} | {v['hours']} | {_skill(v['skill'])} | "
                 f"{_pct(v['coverage_60'])} | {v['top_warnings']} | {_pct(v['top_warning_down_first'])} |")
    L += ["", "## By market state", "", "| state | hours | skill | 60-min band coverage |", "|---|---:|---:|---:|"]
    for q, v in res["regimes"].items():
        L.append(f"| {q} | {v['hours']} | {_skill(v['skill'])} | {_pct(v['coverage_60'])} |")
    return "\n".join(L) + "\n"
