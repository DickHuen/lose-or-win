"""v2.3.0 forecast engine (owner 2026-10-08: "每15分鐘重新思考：仲有幾多波幅可以食？而家走會唔會太早？市場係真反轉
定只係普通回調？"; "判斷個個係高位，之後會點落就平倉賺錢，如果佢回落就買做空，判斷低位就放，再回彈就買做多").

Pure functions, no orders. Every probability is a FREQUENCY: what followed similar situations in the last
`calib_days` of Binance 1h candles, counting only outcomes that had finished by the forecast time (no future candle
is ever used). Nothing is fitted; the buckets and rules below are fixed, explainable and identical live and in the
backtest (the backtest adds the same samples one by one as they finish - `Incremental`).

A situation ("bucket") is described relative to the current move d (+1 up, -1 down; the 1h structure trend, else
the sign of the 6-hour move), so long and short are mirror images and share their samples:
  trend      1h swing structure trending (1) or not (0)
  momentum   6-hour move in the direction d, in ATR1h: <0 / 0-1 / 1-2 / >=2
  position   distance to the 24-hour extreme in the direction d, in ATR1h: <=0.5 / 0.5-1.5 / >1.5
  warning    early-reversal level against d (1h only): 0 / 1 / >=2
  volatility ATR1h(14) / average 1h true range (100 h): <0.8 / 0.8-1.25 / >1.25
A bucket with fewer than `min_samples` finished samples falls back to a coarser one (drop volatility, then
momentum, then position, then trend; at last all samples). The output says which level was used and how many
samples it had (confidence).

Outcomes per sample (hour close t, price c, ATR a, direction d):
  forward move after 15 / 30 / 60 / 120 minutes (15 / 30 from 15m candles where they exist), in ATR, times d
  first passage within passage_hours: +passage_atr x ATR in the direction d first (continuation), -passage_atr
    first (reversal), neither; both in the same 1h candle = unknown (not counted)
  largest move with d (favourable) and against d (adverse) within excursion_hours, in ATR

Early reversal warning (owner's list: divergence, slowing, failed tests, wick rejection, ATR expansion /
contraction, volume, swing structure, higher timeframes). For a top (mirrored for a bottom):
  level 1 WARNING      price within near_extreme_atr x ATR1h of a 24-hour high that ended a move of at least
                       run_min_atr x ATR1h, and at least warn_min_signs of the 7 signs below
  level 2 PREPARATION  the signs were there, the high is at most prep_hours old, price still within prep_max_atr
                       x ATR1h of it and it turned: a 1h close below the lows of the two 1h candles before (live
                       and backtest: or a 15m close below the lowest low of the four 15m candles before)
  level 3 CONFIRMED    the 1h structure broke (change of character, intraday.find_break) within
                       intraday.reversal_window_hours
  A warning is NOT an entry signal. Used by the dynamic exit (tighten / reduce / close) and, only when switched on,
  by the early-reversal entry (backtest variant).
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from perpbot import intraday as idy
from perpbot.indicators import Candle, atr_wilder, clv, true_ranges

H1 = idy.H1_MS
M15 = idy.M15_MS
LEVEL_ZH = {0: "冇", 1: "預警", 2: "準備轉勢", 3: "確認轉勢"}
SIGN_ZH = {"divergence": "動能背馳", "slowing": "速度減慢", "failed_tests": "多次試頂／底失敗", "wick": "影線拒絕",
           "atr": "波幅異常（擴張／收縮）", "volume": "成交量異常", "higher_tf": "大時間框架轉弱"}


@dataclass(frozen=True)
class FParams:
    enabled: bool
    calib_days: int
    min_samples: int
    horizons_min: tuple[int, ...]
    passage_hours: int
    passage_atr: float
    excursion_hours: int
    usd_levels: tuple[float, ...]
    extreme_hours: int
    near_extreme_atr: float
    run_min_atr: float
    prep_hours: int
    prep_max_atr: float
    warn_min_signs: int
    rsi_period: int
    vol_hours: int
    volume_hours: int
    entry_filter: bool

    @classmethod
    def from_cfg(cls, cfg: Any) -> "FParams":
        sec = cfg.forecast
        vals = {f: getattr(sec, f) for f in cls.__dataclass_fields__}
        vals["horizons_min"] = tuple(int(x) for x in vals["horizons_min"])
        vals["usd_levels"] = tuple(float(x) for x in vals["usd_levels"])
        return cls(**vals)

    def outcome_hours(self) -> int:
        return max(self.passage_hours, self.excursion_hours, max(self.horizons_min) // 60 + 1)

    def warmup_hours(self, ip: idy.Params) -> int:
        return max(ip.need_1h(), self.vol_hours + 2, self.volume_hours + 13, self.extreme_hours + 8,
                   self.rsi_period * 3 + 2) + 2


# ---------------------------------------------------------------- indicators over a whole 1h series
def rsi_wilder(closes: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period
    out[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        ag = (ag * (period - 1) + gains[i - 1]) / period
        al = (al * (period - 1) + losses[i - 1]) / period
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def _rolling_mean(xs: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


@dataclass
class Warning:
    side: int                       # +1 top (against longs), -1 bottom (against shorts)
    level: int = 0
    signs: list[str] = field(default_factory=list)
    extreme: float | None = None
    extreme_ms: int | None = None
    dist_atr: float | None = None
    run_atr: float | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"side": "top" if self.side > 0 else "bottom", "level": self.level, "level_zh": LEVEL_ZH[self.level],
                "signs": list(self.signs), "extreme": self.extreme, "extreme_ms": self.extreme_ms,
                "dist_atr": None if self.dist_atr is None else round(self.dist_atr, 3),
                "run_atr": None if self.run_atr is None else round(self.run_atr, 3), "note": self.note}


class Frame:
    """One 1h series (oldest first, closed candles) with its indicators, the 4h series and optional 15m series.
    Everything at hour index i uses candles 0..i only."""

    def __init__(self, h1: Sequence[Candle], h4: Sequence[Candle], m15: Sequence[Candle] | None, ip: idy.Params,
                 fp: FParams) -> None:
        self.h1, self.h4, self.m15 = list(h1), list(h4), list(m15 or [])
        self.ip, self.fp = ip, fp
        self.close_ms = [c.open_ms + H1 for c in self.h1]
        self.atr = atr_wilder(self.h1, ip.atr_period)
        self.rsi = rsi_wilder([c.close for c in self.h1], fp.rsi_period)
        self.tr_mean = _rolling_mean(true_ranges(self.h1), fp.vol_hours)
        self.vol_mean = _rolling_mean([c.volume for c in self.h1], fp.volume_hours)
        self.h4_close = [c.open_ms + 4 * H1 for c in self.h4]
        self.m15_open = [c.open_ms for c in self.m15]
        self._ctx: dict[int, int] = {}

    # ------------------------------------------------------------ lookups
    def index_at(self, t_ms: int) -> int:
        """Newest 1h candle closed at or before t_ms (-1 when none)."""
        return bisect.bisect_right(self.close_ms, t_ms) - 1

    def ok(self, i: int) -> bool:
        return i >= self.fp.warmup_hours(self.ip) and self.atr[i] is not None and self.tr_mean[i] is not None

    def m15_upto(self, t_ms: int, n: int) -> list[Candle]:
        j = bisect.bisect_right(self.m15_open, t_ms - M15)
        return self.m15[max(0, j - n):j]

    def ctx_bias(self, t_ms: int) -> int:
        j = bisect.bisect_right(self.h4_close, t_ms)
        if j in self._ctx:
            return self._ctx[j]
        n = self.ip.need_4h() + 4
        win = self.h4[max(0, j - n):j]
        b = int(idy.context(win, self.ip).get("bias") or 0) if len(win) >= self.ip.need_4h() else 0
        self._ctx[j] = b
        return b

    def structure(self, i: int) -> idy.Structure:
        n = self.ip.need_1h()
        return idy.structure(self.h1[max(0, i + 1 - n):i + 1], self.ip)

    # ------------------------------------------------------------ early reversal warning
    def warning(self, i: int, side: int, st: idy.Structure, bias: int, m15: Sequence[Candle] | None = None,
                t_ms: int | None = None, price: float | None = None) -> Warning:
        fp, h = self.fp, self.h1
        w = Warning(side)
        a = float(self.atr[i] or 0.0)
        t = t_ms if t_ms is not None else self.close_ms[i]
        px = h[i].close if price is None else price
        if st.break_dir == -side and st.break_close_ms is not None and st.break_level is not None and a > 0 and \
                t - st.break_close_ms <= self.ip.reversal_window_hours * H1 and \
                (px - st.break_level) * side <= self.ip.reclaim_atr * a:          # not reclaimed (as intraday.reversal)
            w.level, w.note = 3, f"1h structure broke {'down' if side > 0 else 'up'} at {st.break_level}"
            w.extreme = st.break_level
        if a <= 0:
            return w
        n = fp.extreme_hours
        lo = max(0, i - n + 1)
        win = range(lo, i + 1)
        ext_j = max(win, key=lambda j: h[j].high) if side > 0 else min(win, key=lambda j: h[j].low)
        ext = h[ext_j].high if side > 0 else h[ext_j].low
        before = range(lo, ext_j + 1)
        start = min(h[j].low for j in before) if side > 0 else max(h[j].high for j in before)
        w.extreme, w.extreme_ms = (ext, h[ext_j].open_ms) if w.level < 3 else (w.extreme, w.extreme_ms)
        w.run_atr = (ext - start) * side / a
        w.dist_atr = (ext - px) * side / a
        if w.run_atr < fp.run_min_atr:
            if not w.note:
                w.note = f"no move into an extreme (run {w.run_atr:.2f} ATR1h < {fp.run_min_atr})"
            return w
        signs: list[str] = []
        rec, early = range(max(lo, i - 5), i + 1), range(lo, max(lo, i - 5))
        hi = (lambda j: h[j].high) if side > 0 else (lambda j: -h[j].low)
        if len(early) >= 3:
            r_rec = [self.rsi[j] for j in rec if self.rsi[j] is not None]
            r_early = [self.rsi[j] for j in early if self.rsi[j] is not None]
            if r_rec and r_early and max(hi(j) for j in rec) > max(hi(j) for j in early):
                if (side > 0 and max(r_rec) < max(r_early) - 2.0) or (side < 0 and min(r_rec) > min(r_early) + 2.0):
                    signs.append("divergence")
        if i >= 6:
            m1 = (h[i].close - h[i - 3].close) * side
            m2 = (h[i - 3].close - h[i - 6].close) * side
            if m2 > 0 and m1 < 0.5 * m2:
                signs.append("slowing")
        tests = 0
        for j in range(max(lo, i - 7), i + 1):
            c = h[j]
            near = (c.high >= ext - 0.25 * a) if side > 0 else (c.low <= ext + 0.25 * a)
            if near and clv(c.high, c.low, c.close) * side < 0:
                tests += 1
        if tests >= 2:
            signs.append("failed_tests")
        last = h[i]
        rng = last.high - last.low
        wick = (last.high - max(last.open, last.close)) if side > 0 else (min(last.open, last.close) - last.low)
        if rng >= 0.5 * a and wick >= 0.5 * rng:
            signs.append("wick")
        ratio = a / float(self.tr_mean[i]) if self.tr_mean[i] else 1.0
        if ratio >= 1.5 or ratio <= 0.7:
            signs.append("atr")
        vm = self.vol_mean[i]
        if vm and vm > 0:
            climax = max(h[j].volume for j in range(max(0, i - 2), i + 1)) >= 2.0 * vm and \
                clv(last.high, last.low, last.close) * side < 0.5
            v_now = sum(h[j].volume for j in range(max(0, i - 5), i + 1))
            v_before = sum(h[j].volume for j in range(max(0, i - 11), max(0, i - 5)))
            fading = i >= 12 and v_before > 0 and v_now < 0.7 * v_before and max(hi(j) for j in rec) >= hi(ext_j)
            if climax or fading:
                signs.append("volume")
        if bias != side or st.efficiency * side < 0.2:
            signs.append("higher_tf")
        w.signs = signs
        enough = len(signs) >= fp.warn_min_signs
        if w.level < 1 and enough and w.dist_atr <= fp.near_extreme_atr:
            w.level, w.note = 1, f"near the 24h {'high' if side > 0 else 'low'} {ext} with {len(signs)} signs"
        if w.level < 2 and enough and i - ext_j < fp.prep_hours and w.dist_atr <= fp.prep_max_atr:
            turned = i >= 2 and (h[i].close - (min(h[i - 1].low, h[i - 2].low) if side > 0
                                               else max(h[i - 1].high, h[i - 2].high))) * side < 0
            if not turned and m15 is not None and len(m15) >= 5:
                prev = m15[-5:-1]
                ref = min(c.low for c in prev) if side > 0 else max(c.high for c in prev)
                turned = (m15[-1].close - ref) * side < 0 and (m15[-1].open_ms >= h[ext_j].open_ms)
            if turned:
                w.level, w.note = 2, (f"turned from the 24h {'high' if side > 0 else 'low'} {ext}: "
                                      f"{', '.join(signs)}")
        return w

    # ------------------------------------------------------------ features of an hour (bucket key)
    def features(self, i: int, *, m15: Sequence[Candle] | None = None, t_ms: int | None = None,
                 price: float | None = None) -> dict[str, Any]:
        h, fp = self.h1, self.fp
        a = float(self.atr[i] or 0.0)
        t = self.close_ms[i] if t_ms is None else t_ms
        st = self.structure(i)
        bias = self.ctx_bias(t)
        c = h[i].close
        mom = (c - h[i - 6].close) / a if a > 0 and i >= 6 else 0.0
        d = st.trend if st.trend else (1 if mom >= 0 else -1)
        lo = max(0, i - fp.extreme_hours + 1)
        hi24 = max(x.high for x in h[lo:i + 1])
        lo24 = min(x.low for x in h[lo:i + 1])
        pos = ((hi24 - c) if d > 0 else (c - lo24)) / a if a > 0 else 0.0
        ratio = a / float(self.tr_mean[i]) if self.tr_mean[i] else 1.0
        # bucket warnings: 1h only (the samples have no 15m), at the hour close
        wk = {s: self.warning(i, s, st, bias) for s in (1, -1)}
        against = wk[d].level
        key = (1 if st.trend else 0,
               0 if mom * d < 0 else 1 if mom * d < 1 else 2 if mom * d < 2 else 3,
               0 if pos <= 0.5 else 1 if pos <= 1.5 else 2,
               min(against, 2),
               0 if ratio < 0.8 else 1 if ratio <= 1.25 else 2)
        out = {"i": i, "t_ms": t, "d": d, "key": key, "atr": a, "close": c, "structure": st, "bias": bias,
               "mom_atr": mom, "pos_atr": pos, "vol_ratio": ratio, "hi24": hi24, "lo24": lo24, "warn_key": wk}
        if m15 is not None or price is not None or t_ms is not None:
            px = price if price is not None else c
            out["warnings"] = {s: self.warning(i, s, st, bias, m15=m15, t_ms=t, price=px) for s in (1, -1)}
        return out

    # ------------------------------------------------------------ outcomes of an hour (future of the sample)
    def outcomes(self, i: int, d: int) -> dict[str, Any] | None:
        fp, h = self.fp, self.h1
        need = fp.outcome_hours()
        if i + need >= len(h):
            return None
        a = float(self.atr[i] or 0.0)
        if a <= 0:
            return None
        c = h[i].close
        t = self.close_ms[i]
        # contiguous candles only (a gap would shift the clock)
        if self.close_ms[i + need] != t + need * H1:
            return None
        out: dict[str, Any] = {"fwd": {}, "passage": None, "fav": 0.0, "adv": 0.0, "resolve_ms": t + need * H1}
        for hz in fp.horizons_min:
            if hz % 60 == 0:
                out["fwd"][hz] = (h[i + hz // 60].close - c) * d / a
            else:
                at = t + hz * 60_000 - M15             # the 15m candle closing hz minutes after t
                j = bisect.bisect_left(self.m15_open, at)
                if j < len(self.m15) and self.m15[j].open_ms == at:
                    out["fwd"][hz] = (self.m15[j].close - c) * d / a
        thr = fp.passage_atr * a
        for k in range(1, fp.passage_hours + 1):
            b = h[i + k]
            fav = (b.high - c) if d > 0 else (c - b.low)
            adv = (c - b.low) if d > 0 else (b.high - c)
            up, dn = fav >= thr, adv >= thr
            if up and dn:
                out["passage"] = "unknown"
                break
            if up or dn:
                out["passage"] = 1 if up else -1
                break
        if out["passage"] is None:
            out["passage"] = 0
        fav_m = adv_m = 0.0
        for k in range(1, fp.excursion_hours + 1):
            b = h[i + k]
            fav_m = max(fav_m, ((b.high - c) if d > 0 else (c - b.low)) / a)
            adv_m = max(adv_m, ((c - b.low) if d > 0 else (b.high - c)) / a)
        out["fav"], out["adv"] = fav_m, adv_m
        return out


# ---------------------------------------------------------------- empirical tables
LEVELS = ((0, 1, 2, 3, 4), (0, 1, 2, 3), (0, 2, 3), (0, 3), (3,), ())


def subkey(key: tuple[int, ...], lv: int) -> tuple[int, ...]:
    return tuple(key[k] for k in LEVELS[lv])


class Bucket:
    def __init__(self) -> None:
        self.fwd: dict[int, list[float]] = {}
        self.fav: list[float] = []
        self.adv: list[float] = []
        self.passage = {1: 0, -1: 0, 0: 0}

    @property
    def n(self) -> int:
        return len(self.fav)

    def add(self, o: dict[str, Any], sign: int) -> None:
        for hz, v in o["fwd"].items():
            lst = self.fwd.setdefault(hz, [])
            if sign > 0:
                bisect.insort(lst, v)
            else:
                del lst[bisect.bisect_left(lst, v)]
        for lst, v in ((self.fav, o["fav"]), (self.adv, o["adv"])):
            if sign > 0:
                bisect.insort(lst, v)
            else:
                del lst[bisect.bisect_left(lst, v)]
        if o["passage"] in (1, -1, 0):
            self.passage[o["passage"]] += sign


def quantile(sorted_xs: Sequence[float], q: float) -> float | None:
    if not sorted_xs:
        return None
    pos = q * (len(sorted_xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def share_at_least(sorted_xs: Sequence[float], x: float) -> float | None:
    if not sorted_xs:
        return None
    return (len(sorted_xs) - bisect.bisect_left(sorted_xs, x)) / len(sorted_xs)


class Model:
    """Finished samples per bucket at every fallback level."""

    def __init__(self, fp: FParams) -> None:
        self.fp = fp
        self.tables: list[dict[tuple[int, ...], Bucket]] = [{} for _ in LEVELS]
        self.count = 0

    def update(self, key: tuple[int, ...], o: dict[str, Any], sign: int) -> None:
        for lv in range(len(LEVELS)):
            b = self.tables[lv].setdefault(subkey(key, lv), Bucket())
            b.add(o, sign)
        self.count += sign

    def pick(self, key: tuple[int, ...], size: Any) -> tuple[int, Bucket | None]:
        for lv in range(len(LEVELS)):
            b = self.tables[lv].get(subkey(key, lv))
            if b is not None and size(b) >= self.fp.min_samples:
                return lv, b
        b = self.tables[-1].get(())
        return len(LEVELS) - 1, b

    def forecast(self, feat: dict[str, Any], price: float) -> dict[str, Any]:
        fp = self.fp
        d, a, key = int(feat["d"]), float(feat["atr"]), feat["key"]
        st: idy.Structure = feat["structure"]
        out: dict[str, Any] = {"t_ms": feat["t_ms"], "price": price, "atr1h": a, "direction": d,
                               "trend": st.trend, "efficiency": round(st.efficiency, 4), "bias_4h": feat["bias"],
                               "key": list(key), "samples_total": self.count}
        out["regime"] = ("volatile" if feat["vol_ratio"] >= 1.5 else "trend_up" if st.trend > 0 else
                         "trend_down" if st.trend < 0 else "range")
        ranges: dict[str, Any] = {}
        for hz in fp.horizons_min:
            lv, b = self.pick(key, lambda bb, hz=hz: len(bb.fwd.get(hz, [])))
            lst = b.fwd.get(hz, []) if b else []
            scaled = False
            if len(lst) < fp.min_samples:      # no 15m history: scale the 60-minute spread by sqrt(time)
                lv, b60 = self.pick(key, lambda bb: len(bb.fwd.get(60, [])))
                lst = [x * math.sqrt(hz / 60.0) for x in (b60.fwd.get(60, []) if b60 else [])]
                scaled = True
            qs = [quantile(lst, q) for q in (0.1, 0.5, 0.9)]
            if None in qs:
                ranges[str(hz)] = {"n": len(lst), "level": lv, "scaled": scaled}
                continue
            pts = sorted(price + d * q * a for q in qs)     # type: ignore[operator]
            ranges[str(hz)] = {"low": pts[0], "mid": price + d * qs[1] * a, "high": pts[2],  # type: ignore[operator]
                               "n": len(lst), "level": lv, "scaled": scaled}
        out["ranges"] = ranges
        lv, b = self.pick(key, lambda bb: bb.passage[1] + bb.passage[-1] + bb.passage[0])
        n = (b.passage[1] + b.passage[-1] + b.passage[0]) if b else 0
        pc = b.passage[1] / n if n else None
        pr = b.passage[-1] / n if n else None
        out.update({"p_continuation": pc, "p_reversal": pr, "p_neither": (1 - pc - pr) if n else None,  # type: ignore[operator]
                    "passage_n": n, "passage_level": lv,
                    "p_up_first": pc if d > 0 else pr, "p_down_first": pr if d > 0 else pc})
        half = 1.96 * math.sqrt(pc * (1 - pc) / n) if n and pc is not None else 1.0
        out["confidence"] = ("high" if n >= 150 and half <= 0.08 and lv <= 2 else "medium" if n >= 60 else "low")
        out["confidence_halfwidth"] = round(half, 4)
        lv2, b2 = self.pick(key, lambda bb: bb.n)
        up_l, dn_l = ((b2.fav, b2.adv) if d > 0 else (b2.adv, b2.fav)) if b2 else ([], [])
        mu, md = quantile(up_l, 0.5), quantile(dn_l, 0.5)
        out.update({"excursion_level": lv2, "excursion_n": len(up_l),
                    "up_room_usd": None if mu is None else mu * a, "down_room_usd": None if md is None else md * a,
                    "bull_target": None if mu is None else price + mu * a,
                    "bear_target": None if md is None else price - md * a})
        out["reach"] = {"up": {str(int(u)): share_at_least(up_l, u / a) for u in fp.usd_levels},
                        "down": {str(int(u)): share_at_least(dn_l, u / a) for u in fp.usd_levels}}
        out["invalidation"] = (st.lows[-1].price if (d > 0 and st.lows) else st.highs[-1].price if (d < 0 and st.highs)
                               else (feat["lo24"] if d > 0 else feat["hi24"]))
        above = [s.price for s in st.highs if s.price > price]
        below = [s.price for s in st.lows if s.price < price]
        out["resistance"] = min(above) if above else None
        out["support"] = max(below) if below else None
        ws = feat.get("warnings") or feat["warn_key"]
        out["warnings"] = {"top": ws[1].to_dict(), "bottom": ws[-1].to_dict()}
        out["near_high"] = (feat["hi24"] - price) <= fp.near_extreme_atr * a
        out["near_low"] = (price - feat["lo24"]) <= fp.near_extreme_atr * a
        return out


# ---------------------------------------------------------------- building (live) and incremental (backtest)
def _sample_ok(fr: Frame, i: int) -> bool:
    return fr.ok(i)


class Incremental:
    """The backtest's model: finished samples are added as time passes and dropped after calib_days. At any time t
    it holds exactly the samples `build` would use at t (tested). Times must not go backwards."""

    def __init__(self, frame: Frame) -> None:
        self.fr = frame
        self.model = Model(frame.fp)
        self.samples: list[tuple[int, int, tuple[int, ...], dict[str, Any]]] = []   # (t, resolve, key, outcome)
        self._next = 0          # next hour index to turn into a sample
        self._added = 0         # samples[:_added] have finished (added, or too old when they finished)
        self._oldest = 0        # samples[:_oldest] are out of the window
        self._in: set[int] = set()
        self._t = -1

    def _prepare(self, upto_i: int) -> None:
        fr = self.fr
        while self._next <= upto_i:
            i = self._next
            self._next += 1
            if not _sample_ok(fr, i):
                continue
            f = fr.features(i)
            o = fr.outcomes(i, f["d"])
            if o is not None:
                self.samples.append((fr.close_ms[i], o["resolve_ms"], f["key"], o))

    def advance(self, t_ms: int) -> None:
        if t_ms < self._t:
            raise ValueError("Incremental.advance: time went backwards")
        self._t = t_ms
        fr = self.fr
        calib = fr.fp.calib_days * 86_400_000
        self._prepare(fr.index_at(t_ms - fr.fp.outcome_hours() * H1))
        while self._added < len(self.samples) and self.samples[self._added][1] <= t_ms:
            s = self.samples[self._added]
            if s[0] >= t_ms - calib:
                self.model.update(s[2], s[3], +1)
                self._in.add(self._added)
            self._added += 1
        while self._oldest < self._added and self.samples[self._oldest][0] < t_ms - calib:
            if self._oldest in self._in:
                s = self.samples[self._oldest]
                self.model.update(s[2], s[3], -1)
                self._in.discard(self._oldest)
            self._oldest += 1

    def forecast(self, t_ms: int, price: float | None = None, m15: Sequence[Candle] | None = None) -> dict[str, Any] | None:
        self.advance(t_ms)
        return current(self.fr, self.model, t_ms, price, m15)


def current(fr: Frame, model: Model, t_ms: int, price: float | None, m15: Sequence[Candle] | None) -> dict[str, Any] | None:
    i = fr.index_at(t_ms)
    if i < 0 or not fr.ok(i):
        return None
    if m15 is None and fr.m15:
        m15 = fr.m15_upto(t_ms, 8)
    px = price if price is not None else (m15[-1].close if m15 else fr.h1[i].close)
    feat = fr.features(i, m15=m15, t_ms=t_ms, price=px)
    out = model.forecast(feat, px)
    out["hour_close_ms"] = fr.close_ms[i]
    return out


def build(fr: Frame, t_ms: int) -> Model:
    """The live model at t_ms from scratch: every sample whose outcome finished at or before t_ms and whose hour
    closed within the last calib_days."""
    model = Model(fr.fp)
    calib = fr.fp.calib_days * 86_400_000
    last = fr.index_at(t_ms - fr.fp.outcome_hours() * H1)
    for i in range(0, last + 1):
        if fr.close_ms[i] < t_ms - calib or not _sample_ok(fr, i):
            continue
        f = fr.features(i)
        o = fr.outcomes(i, f["d"])
        if o is not None and o["resolve_ms"] <= t_ms:
            model.update(f["key"], o, +1)
    return model


def forecast_now(h1: Sequence[Candle], h4: Sequence[Candle], m15: Sequence[Candle] | None, t_ms: int, ip: idy.Params,
                 fp: FParams, price: float | None = None) -> dict[str, Any] | None:
    """Live: the forecast at t_ms from closed candles only (any candle after t_ms is ignored)."""
    h1c = [c for c in h1 if c.open_ms + H1 <= t_ms]
    h4c = [c for c in h4 if c.open_ms + 4 * H1 <= t_ms]
    m15c = [c for c in (m15 or []) if c.open_ms + M15 <= t_ms]
    fr = Frame(h1c, h4c, m15c, ip, fp)
    model = build(fr, t_ms)
    return current(fr, model, t_ms, price, m15c[-8:] if m15c else None)


# ---------------------------------------------------------------- use by the rules (pure)
def against(fc: dict[str, Any] | None, direction: int) -> dict[str, Any]:
    """The warning that threatens a position / entry in `direction` (top for longs, bottom for shorts)."""
    if not fc:
        return {"level": 0, "signs": []}
    return fc["warnings"]["top" if direction > 0 else "bottom"]


def p_fav(fc: dict[str, Any], direction: int) -> tuple[float | None, float | None]:
    """(probability the next passage_atr move is with `direction`, against it)."""
    if direction > 0:
        return fc.get("p_up_first"), fc.get("p_down_first")
    return fc.get("p_down_first"), fc.get("p_up_first")


def entry_veto(fc: dict[str, Any] | None, direction: int) -> str | None:
    """B (forecast entry filter): no entry straight into an active warning against it, nor when similar situations
    reversed clearly more often than they continued (with at least medium confidence)."""
    if not fc:
        return None
    w = against(fc, direction)
    if int(w.get("level") or 0) >= 1:
        return f"forecast: {LEVEL_ZH[int(w['level'])]} against the entry ({', '.join(w.get('signs') or [])})"
    fav, adv = p_fav(fc, direction)
    if fav is not None and adv is not None and fc.get("confidence") != "low" and adv - fav >= 0.15:
        return f"forecast: similar situations went against this side first {adv:.0%} vs {fav:.0%}"
    return None
