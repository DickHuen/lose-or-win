"""v2.0.0 intraday rules (owner 2026-10-06): direction from CLOSED short-period structure, symmetric long / short.

Pure functions only. The live engine (intraday_live.py) and the backtest (intraday_bt.py) call exactly these, so a
decision made live can be recomputed from the same candles (`intraday-replay`). Every number comes from the config
section `intraday` (Params); none is tuned to a single day.

Inputs at a decision time T (a 15-minute boundary): Binance BTCUSDT 15m, 1h and 4h candles that CLOSED at or before T.

1. Structure (1h). Swing high: a 1h high above the `swing_k` highs before it and not below the `swing_k` highs after
   it (confirmed `swing_k` hours later); swing low mirrored. From the last two swing highs (H1, H2) and lows (L1, L2):
   up = H2 > H1 and L2 > L1; down = H2 < H1 and L2 < L1; anything else = range.
   Break (change of character): in an up structure a 1h CLOSE below L2; in a down structure a 1h close above H2.
2. Setups (one direction per decision, all mirror-symmetric):
   - continuation (順勢回調): structure up (down), no break. The leg from L2 (H2) reached an extreme >= leg_min_atr x
     ATR1h away; price pulled back retrace_min..retrace_max of the leg on 15m, the pullback extreme is among the last
     pullback_max_bars 15m candles, and the last 15m candle turns: closes beyond the previous 15m candle's high (low),
     in the trade direction (close vs open), in the outer part of its range (CLV >= trigger_clv_min).
   - reversal (轉勢): structure up (down) whose L2 (H2) broke within reversal_window_hours; price came back to within
     retest_zone_atr x ATR1h of the broken level without a 15m close beyond it by reclaim_atr x ATR1h, and the last
     15m candle turns away from the level as above.
   - range (震盪): no entry.
3. Stop: beyond the setup's structure extreme by stop_buffer_atr15 x ATR15, at least max(sl_min_atr1h x ATR1h,
   sl_min_pct of the price, the round-trip cost / max_cost_r); refused when wider than sl_max_atr1h x ATR1h or
   sl_max_pct. R = entry - stop.
4. Room: distance to the nearest obstacle (confirmed 1h swing level in the last structure_lookback_hours, or the
   extreme of the last room_recent_bars 15m candles) beyond the entry; open_room_atr x ATR1h when there is none.
   Entry needs room >= tp1_r x R (TP1 fits before the obstacle) and cost <= max_cost_r x R (costs.py).
5. No chasing: one entry per leg (the swing that defines the setup), cooldown_bars 15m candles after any exit,
   at most max_entries_per_day entries per UTC day.
6. Exits (intraday_live / intraday_bt): two legs when the size allows - leg A takes TP1 = tp1_r R (tp1_fraction of
   the position), leg B TP2 = tp2_r R; both start with the stop. After TP1 the runner's stop goes to break-even plus
   costs and then trails trail_atr1h x ATR1h behind the best price since entry (moved only forward, steps of at
   least trail_min_step_atr15 x ATR15). Single leg (too small to split): stop to break-even after MFE >= be_trigger_r
   R, then the same trail. Before TP1 / break-even: a 15m close beyond the setup's invalidation level exits at
   market. Time stop max_hold_hours; no-progress exit after no_progress_hours if MFE < no_progress_mfe_r R.
7. Conviction score 0-100 (sizing only, through strategy.size_tiers): base by setup + trend efficiency or break depth
   + 4h context (aligned / neutral / against, never a block) + room + cost efficiency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from perpbot.indicators import Candle, atr_wilder, clip, clv, ema

M15_MS = 900_000
H1_MS = 3_600_000
H4_MS = 4 * H1_MS
SETUPS = ("continuation", "reversal")


@dataclass(frozen=True)
class Params:
    swing_k: int
    structure_lookback_hours: int
    atr_period: int
    er_hours: int
    ctx_ema_fast: int
    ctx_ema_slow: int
    ctx_slope_bars: int
    leg_min_atr: float
    retrace_min: float
    retrace_max: float
    pullback_max_bars: int
    trigger_clv_min: float
    reversal_window_hours: int
    retest_zone_atr: float
    reclaim_atr: float
    stop_buffer_atr15: float
    sl_min_atr1h: float
    sl_min_pct: float
    sl_max_atr1h: float
    sl_max_pct: float
    max_cost_r: float
    tp1_r: float
    tp1_fraction: float
    tp2_r: float
    room_recent_bars: int
    open_room_atr: float
    invalidation_timeframe: str
    trail_atr1h: float
    trail_min_step_atr15: float
    be_trigger_r: float
    max_hold_hours: float
    no_progress_hours: float
    no_progress_mfe_r: float
    cooldown_bars: int
    max_entries_per_day: int
    score_base_continuation: float
    score_base_reversal: float
    score_trend: float
    score_ctx_aligned: float
    score_ctx_neutral: float
    score_room: float
    score_room_full_r: float
    score_cost: float
    break_full_atr: float

    @classmethod
    def from_cfg(cls, cfg: Any) -> "Params":
        sec = cfg.intraday
        return cls(**{f: getattr(sec, f) for f in cls.__dataclass_fields__})

    # candles each decision needs (the integrity check covers exactly these)
    def need_15m(self) -> int:
        return max(self.room_recent_bars, self.pullback_max_bars, self.atr_period + 1,
                   self.reversal_window_hours * 4, self.structure_lookback_hours * 4) + 2

    def need_1h(self) -> int:
        return max(self.structure_lookback_hours, self.er_hours, self.atr_period + 1) + 2 * self.swing_k + 2

    def need_4h(self) -> int:
        return self.ctx_ema_slow + self.ctx_slope_bars + 2


@dataclass(frozen=True)
class Swing:
    idx: int
    open_ms: int
    price: float


@dataclass
class Structure:
    trend: int                      # +1 up, -1 down, 0 range
    highs: list[Swing]
    lows: list[Swing]
    break_dir: int = 0              # -1: up structure broke down (close < L2); +1: down structure broke up
    break_level: float | None = None
    break_close_ms: int | None = None
    break_swing_ms: int | None = None
    efficiency: float = 0.0         # signed Kaufman efficiency of the last er_hours 1h closes

    def summary(self) -> dict[str, Any]:
        last2 = lambda xs: [[s.open_ms, s.price] for s in xs[-2:]]  # noqa: E731
        return {"trend": self.trend, "swing_highs": last2(self.highs), "swing_lows": last2(self.lows),
                "break_dir": self.break_dir, "break_level": self.break_level, "break_close_ms": self.break_close_ms,
                "efficiency": round(self.efficiency, 4)}


@dataclass
class Setup:
    kind: str                       # continuation | reversal
    direction: int
    leg_id: str
    ok: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)
    stop: float | None = None       # structure stop before the minimum / maximum distance rules
    invalidation: float | None = None


@dataclass
class Decision:
    t_ms: int
    action: str                     # enter | none | blocked
    direction: int = 0
    setup: str | None = None
    leg_id: str | None = None
    entry_ref: float | None = None  # last 15m close (Binance)
    stop: float | None = None       # final stop price (Binance terms)
    r: float | None = None          # entry_ref - stop distance
    tp1: float | None = None
    tp2: float | None = None
    invalidation: float | None = None
    room: float | None = None
    obstacle: float | None = None
    cost_per_unit: float | None = None
    cost_r: float | None = None
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    atr1h: float | None = None
    atr15: float | None = None
    structure: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    setups: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- building blocks
def closed(bars: Sequence[Candle], step_ms: int, t_ms: int) -> list[Candle]:
    return [c for c in bars if c.open_ms + step_ms <= t_ms]


def find_swings(h1: Sequence[Candle], k: int) -> tuple[list[Swing], list[Swing]]:
    highs: list[Swing] = []
    lows: list[Swing] = []
    for i in range(k, len(h1) - k):
        hi, lo = h1[i].high, h1[i].low
        if all(hi > h1[j].high for j in range(i - k, i)) and all(hi >= h1[j].high for j in range(i + 1, i + k + 1)):
            highs.append(Swing(i, h1[i].open_ms, hi))
        if all(lo < h1[j].low for j in range(i - k, i)) and all(lo <= h1[j].low for j in range(i + 1, i + k + 1)):
            lows.append(Swing(i, h1[i].open_ms, lo))
    return highs, lows


def efficiency(closes: Sequence[float]) -> float:
    """Signed Kaufman efficiency ratio: net move / sum of absolute moves (-1..1)."""
    if len(closes) < 2:
        return 0.0
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    return (closes[-1] - closes[0]) / path if path > 0 else 0.0


def _trend_from(highs: Sequence[Swing], lows: Sequence[Swing]) -> int:
    if len(highs) < 2 or len(lows) < 2:
        return 0
    if highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price:
        return 1
    if highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price:
        return -1
    return 0


def find_break(win: Sequence[Candle], highs: Sequence[Swing], lows: Sequence[Swing]) -> tuple[int, Swing | None, int | None]:
    """The newest change of character: an up structure (two rising highs before its newest higher low) that lost
    that higher low on a 1h CLOSE, or a down structure that lost its newest lower high. (direction of the break,
    broken swing, close time of the breaking candle)."""
    best: tuple[int, Swing | None, int | None] = (0, None, None)
    for d, own, other in ((-1, lows, highs), (1, highs, lows)):
        for j in range(len(own) - 1, 0, -1):
            sw, prev = own[j], own[j - 1]
            before = [x for x in other if x.idx < sw.idx]
            if len(before) < 2:
                continue
            ascending = sw.price > prev.price and before[-1].price > before[-2].price
            descending = sw.price < prev.price and before[-1].price < before[-2].price
            if not (ascending if d < 0 else descending):
                continue
            for c in win[sw.idx + 1:]:
                if (c.close - sw.price) * d > 0:
                    t = c.open_ms + H1_MS
                    if best[2] is None or t > best[2]:
                        best = (d, sw, t)
                    break
            break
    return best


def structure(h1: Sequence[Candle], p: Params) -> Structure:
    win = list(h1[-p.structure_lookback_hours:])
    highs, lows = find_swings(win, p.swing_k)
    st = Structure(_trend_from(highs, lows), highs, lows)
    closes = [c.close for c in h1[-(p.er_hours + 1):]]
    st.efficiency = efficiency(closes)
    d, sw, t = find_break(win, highs, lows)
    if d and sw is not None:
        st.break_dir, st.break_level, st.break_close_ms, st.break_swing_ms = d, sw.price, t, sw.open_ms
    return st


def context(h4: Sequence[Candle], p: Params) -> dict[str, Any]:
    """4h background for sizing only: +1 when EMA fast > slow and rising, -1 mirrored, 0 otherwise."""
    closes = [c.close for c in h4]
    f, s = ema(closes, p.ctx_ema_fast), ema(closes, p.ctx_ema_slow)
    if len(closes) <= p.ctx_slope_bars or f[-1] is None or s[-1] is None or f[-1 - p.ctx_slope_bars] is None:
        return {"bias": 0, "ema_fast": f[-1] if f else None, "ema_slow": s[-1] if s else None, "note": "not enough 4h"}
    fast, slow, prev = float(f[-1]), float(s[-1]), float(f[-1 - p.ctx_slope_bars])
    bias = 1 if (fast > slow and fast > prev) else -1 if (fast < slow and fast < prev) else 0
    return {"bias": bias, "ema_fast": fast, "ema_slow": slow, "ema_fast_prev": prev}


def last_atr(bars: Sequence[Candle], period: int) -> float | None:
    a = atr_wilder(bars, period)
    return float(a[-1]) if a and a[-1] is not None else None


def _turn(prev: Candle, cur: Candle, d: int, clv_min: float) -> tuple[bool, str]:
    """The trigger candle turns in direction d: closes beyond the previous candle's extreme, in direction d, with its
    close in the outer part of its range."""
    beyond = cur.close > prev.high if d > 0 else cur.close < prev.low
    body = cur.close > cur.open if d > 0 else cur.close < cur.open
    loc = clv(cur.high, cur.low, cur.close) * d >= clv_min
    if not beyond:
        return False, "15m trigger: close not beyond the previous 15m " + ("high" if d > 0 else "low")
    if not body:
        return False, "15m trigger: candle body against the trade"
    if not loc:
        return False, "15m trigger: close not in the outer part of the candle"
    return True, "15m trigger ok"


# ---------------------------------------------------------------- setups
def continuation(st: Structure, m15: Sequence[Candle], atr1h: float, p: Params) -> Setup:
    d = st.trend
    ref = st.lows[-1] if d > 0 else st.highs[-1]
    leg_id = f"C{d:+d}:{ref.open_ms}"
    s = Setup("continuation", d, leg_id, False, "")
    if st.break_dir == -d and st.break_close_ms is not None and st.break_close_ms > ref.open_ms:
        s.reason = "structure broken since the leg started: no continuation"
        return s
    bars = [c for c in m15 if c.open_ms >= ref.open_ms]
    if len(bars) < 2:
        s.reason = "leg too young"
        return s
    ext_i = max(range(len(bars)), key=lambda i: bars[i].high) if d > 0 else min(range(len(bars)), key=lambda i: bars[i].low)
    ext = bars[ext_i].high if d > 0 else bars[ext_i].low
    leg = (ext - ref.price) * d
    after = bars[ext_i + 1:]
    s.detail = {"leg_start": ref.price, "leg_extreme": ext, "leg_atr": leg / atr1h if atr1h else None}
    if leg < p.leg_min_atr * atr1h:
        s.reason = f"leg {leg / atr1h:.2f} ATR1h < {p.leg_min_atr}"
        return s
    if len(after) < 2:
        s.reason = "no pullback yet"
        return s
    pb_i = min(range(len(after)), key=lambda i: after[i].low) if d > 0 else max(range(len(after)), key=lambda i: after[i].high)
    pb = after[pb_i].low if d > 0 else after[pb_i].high
    retr = (ext - pb) * d / leg
    s.detail.update({"pullback_extreme": pb, "retracement": round(retr, 4)})
    if not (p.retrace_min <= retr <= p.retrace_max):
        s.reason = f"retracement {retr:.3f} outside {p.retrace_min}-{p.retrace_max}"
        return s
    if pb_i < len(after) - p.pullback_max_bars:
        s.reason = f"pullback extreme older than {p.pullback_max_bars} 15m candles"
        return s
    ok, why = _turn(m15[-2], m15[-1], d, p.trigger_clv_min)
    if not ok:
        s.reason = why
        return s
    if (m15[-1].close - ext) * d >= 0:
        s.reason = "trigger closed beyond the leg extreme (chasing)"
        return s
    s.ok, s.reason = True, "continuation: pullback held, 15m turned"
    s.invalidation = pb
    s.stop = pb  # buffer added by finalize()
    return s


def reversal(st: Structure, m15: Sequence[Candle], atr1h: float, t_ms: int, p: Params) -> Setup:
    d = st.break_dir
    leg_id = f"R{d:+d}:{st.break_swing_ms}"
    s = Setup("reversal", d, leg_id, False, "")
    if not d or st.break_level is None or st.break_close_ms is None:
        s.reason = "no structure break"
        return s
    if t_ms - st.break_close_ms > p.reversal_window_hours * H1_MS:
        s.reason = f"break older than {p.reversal_window_hours}h"
        return s
    lvl = st.break_level
    bars = [c for c in m15 if c.open_ms >= st.break_close_ms]
    s.detail = {"broken_level": lvl, "break_close_ms": st.break_close_ms}
    if len(bars) < 2:
        s.reason = "no retest yet"
        return s
    reclaim = lvl - d * p.reclaim_atr * atr1h
    if any((c.close - reclaim) * d < 0 for c in bars):
        s.reason = "broken level reclaimed: reversal cancelled"
        return s
    rt_i = max(range(len(bars)), key=lambda i: bars[i].high) if d < 0 else min(range(len(bars)), key=lambda i: bars[i].low)
    rt = bars[rt_i].high if d < 0 else bars[rt_i].low
    dist = (lvl - rt) * -d
    s.detail.update({"retest_extreme": rt, "retest_gap_atr": dist / atr1h if atr1h else None})
    if dist > p.retest_zone_atr * atr1h:
        s.reason = f"no retest: closest {dist / atr1h:.2f} ATR1h from the level (> {p.retest_zone_atr})"
        return s
    if rt_i < len(bars) - p.pullback_max_bars:
        s.reason = f"retest older than {p.pullback_max_bars} 15m candles"
        return s
    ok, why = _turn(m15[-2], m15[-1], d, p.trigger_clv_min)
    if not ok:
        s.reason = why
        return s
    if (m15[-1].close - lvl) * d <= 0:
        s.reason = "trigger closed on the wrong side of the broken level"
        return s
    post = [c for c in m15 if st.break_close_ms - H1_MS <= c.open_ms <= bars[rt_i].open_ms]
    far = min(c.low for c in post) if d < 0 else max(c.high for c in post)
    s.detail["break_depth_atr"] = abs(lvl - far) / atr1h if atr1h else 0.0
    s.ok, s.reason = True, "reversal: break, failed retest, 15m turned"
    s.invalidation = rt
    s.stop = rt
    return s


def obstacle(d: int, entry: float, st: Structure, m15: Sequence[Candle], atr1h: float, p: Params) -> tuple[float | None, float]:
    """(nearest level beyond the entry in direction d, room); open room when nothing is in the way."""
    levels = [s.price for s in (st.highs if d > 0 else st.lows)]
    recent = m15[-p.room_recent_bars:]
    if recent:
        levels.append(max(c.high for c in recent) if d > 0 else min(c.low for c in recent))
    ahead = [x for x in levels if (x - entry) * d > 0]
    if not ahead:
        return None, p.open_room_atr * atr1h
    lvl = min(ahead) if d > 0 else max(ahead)
    return lvl, abs(lvl - entry)


def stop_distance(structure_stop: float, entry: float, d: int, atr1h: float, atr15: float, cost_unit: float,
                  p: Params) -> tuple[float | None, str]:
    """Final R (price distance) or None with the reason."""
    raw = (entry - (structure_stop - d * p.stop_buffer_atr15 * atr15)) * d
    floor = max(p.sl_min_atr1h * atr1h, p.sl_min_pct / 100.0 * entry, cost_unit / p.max_cost_r if p.max_cost_r else 0.0)
    r = max(raw, floor)
    cap = min(p.sl_max_atr1h * atr1h, p.sl_max_pct / 100.0 * entry)
    if r > cap:
        return None, f"stop {r / entry * 100:.2f}% / {r / atr1h:.2f} ATR1h beyond the maximum (entry too far from structure)"
    return r, "structure stop" if raw >= floor else "widened to the minimum stop"


def score(kind: str, d: int, st: Structure, setup: Setup, ctx: dict[str, Any], room_r: float, cost_r: float,
          p: Params) -> tuple[float, dict[str, float]]:
    comp: dict[str, float] = {}
    comp["setup"] = p.score_base_continuation if kind == "continuation" else p.score_base_reversal
    if kind == "continuation":
        comp["trend"] = p.score_trend * clip(st.efficiency * d, 0.0, 1.0)
    else:
        depth = float(setup.detail.get("break_depth_atr") or 0.0)
        comp["trend"] = p.score_trend * clip(depth / p.break_full_atr, 0.0, 1.0) if p.break_full_atr else 0.0
    bias = int(ctx.get("bias") or 0)
    comp["context"] = p.score_ctx_aligned if bias == d else p.score_ctx_neutral if bias == 0 else 0.0
    span = p.score_room_full_r - p.tp1_r
    comp["room"] = p.score_room * clip((room_r - p.tp1_r) / span, 0.0, 1.0) if span > 0 else 0.0
    comp["cost"] = p.score_cost * clip(1.0 - cost_r / p.max_cost_r, 0.0, 1.0) if p.max_cost_r else 0.0
    return round(sum(comp.values()), 4), {k: round(v, 4) for k, v in comp.items()}


@dataclass
class History:
    """What the no-chasing rules need: legs already traded, last exit, entries today."""
    traded_legs: set[str] = field(default_factory=set)
    last_exit_ms: int | None = None
    entries_today: int = 0


def evaluate(p: Params, t_ms: int, m15: Sequence[Candle], h1: Sequence[Candle], h4: Sequence[Candle], *,
             cost_unit_fn: Any, history: History, position_dir: int = 0) -> Decision:
    """The decision at T from candles closed at or before T. `cost_unit_fn(direction, entry)` returns the expected
    round-trip cost per unit of BTC (fees, spread / depth, slippage, funding) for that trade."""
    m15 = closed(m15, M15_MS, t_ms)
    h1 = closed(h1, H1_MS, t_ms)
    h4 = closed(h4, H4_MS, t_ms)
    dec = Decision(t_ms, "none")
    if len(m15) < p.need_15m() or len(h1) < p.need_1h():
        dec.action = "blocked"
        dec.reasons.append("not enough closed candles")
        return dec
    atr1h, atr15 = last_atr(h1, p.atr_period), last_atr(m15, p.atr_period)
    if not atr1h or not atr15:
        dec.action = "blocked"
        dec.reasons.append("ATR unavailable")
        return dec
    dec.atr1h, dec.atr15 = atr1h, atr15
    entry = m15[-1].close
    dec.entry_ref = entry
    st = structure(h1, p)
    ctx = context(h4, p)
    dec.structure, dec.context = st.summary(), ctx
    if position_dir:
        dec.reasons.append("position open: no new entry")
        return dec
    cands: list[Setup] = []
    if st.trend:
        cands.append(continuation(st, m15, atr1h, p))
    if st.break_dir and st.break_close_ms is not None and t_ms - st.break_close_ms <= p.reversal_window_hours * H1_MS:
        cands.append(reversal(st, m15, atr1h, t_ms, p))
    if not cands:
        dec.reasons.append("range: mixed swings and no recent structure break - no trade")
    for s in cands:
        rec = {"kind": s.kind, "direction": s.direction, "leg_id": s.leg_id, "ok": s.ok, "reason": s.reason,
               "detail": s.detail}
        dec.setups.append(rec)
        if not s.ok:
            continue
        d = s.direction
        if s.leg_id in history.traded_legs:
            rec.update(ok=False, reason="this leg was already traded (no chasing)")
            continue
        if history.last_exit_ms is not None and t_ms - history.last_exit_ms < p.cooldown_bars * M15_MS:
            rec.update(ok=False, reason=f"cooldown: {p.cooldown_bars} 15m candles after the last exit")
            continue
        if history.entries_today >= p.max_entries_per_day:
            rec.update(ok=False, reason=f"{p.max_entries_per_day} entries today already")
            continue
        cost_unit = float(cost_unit_fn(d, entry))
        r, why = stop_distance(float(s.stop), entry, d, atr1h, atr15, cost_unit, p)
        rec["stop_rule"] = why
        if r is None:
            rec.update(ok=False, reason=why)
            continue
        lvl, room = obstacle(d, entry, st, m15, atr1h, p)
        cost_r = cost_unit / r
        rec.update(r=r, room=room, obstacle=lvl, cost_per_unit=cost_unit, cost_r=round(cost_r, 4))
        if cost_r > p.max_cost_r + 1e-9:
            rec.update(ok=False, reason=f"costs {cost_r:.2f} R > {p.max_cost_r} R")
            continue
        if room < p.tp1_r * r - 1e-9:
            rec.update(ok=False, reason=f"room {room / r:.2f} R to {lvl} < TP1 {p.tp1_r} R")
            continue
        sc, comp = score(s.kind, d, st, s, ctx, room / r, cost_r, p)
        dec.action, dec.direction, dec.setup, dec.leg_id = "enter", d, s.kind, s.leg_id
        dec.stop, dec.r = entry - d * r, r
        dec.tp1, dec.tp2 = entry + d * p.tp1_r * r, entry + d * p.tp2_r * r
        dec.invalidation, dec.room, dec.obstacle = s.invalidation, room, lvl
        dec.cost_per_unit, dec.cost_r, dec.score, dec.components = cost_unit, cost_r, sc, comp
        rec["score"] = sc
        break
    if dec.action != "enter" and not dec.reasons:
        dec.reasons += [f"{r['kind']} {'long' if r['direction'] > 0 else 'short'}: {r['reason']}" for r in dec.setups] \
            or ["no setup"]
    return dec


# ---------------------------------------------------------------- exits (shared)
def break_even(direction: int, entry: float, cost_unit: float) -> float:
    """Stop price at which the remaining position nets about zero after the round-trip costs."""
    return entry + direction * cost_unit


def trail_level(direction: int, best: float, atr1h: float, p: Params) -> float:
    return best - direction * p.trail_atr1h * atr1h


def improved(direction: int, old: float | None, new: float, atr15: float, p: Params) -> bool:
    if old is None:
        return True
    return (new - old) * direction >= p.trail_min_step_atr15 * atr15


@dataclass
class ManageState:
    """A live / simulated intraday position as the exit rules see it (Binance price terms)."""
    direction: int
    entry: float
    r: float
    entry_ms: int
    invalidation: float | None
    stage: str                      # initial | runner
    stop: float                     # the stop now in force
    best: float                     # best price since entry
    worst: float
    two_legs: bool
    cost_unit: float


def invalidation_bar(bars: Sequence[Candle], entry_ms: int, p: Params) -> Candle | None:
    """The newest closed candle of invalidation_timeframe that closed after the entry (None before one did)."""
    step = H1_MS if p.invalidation_timeframe == "1h" else M15_MS
    if not bars or bars[-1].open_ms + step <= entry_ms:
        return None
    return bars[-1]


def exit_signal(s: ManageState, last15: Candle | None, now_ms: int, p: Params, fresh: bool = True) -> tuple[str | None, str]:
    """(reason, detail) for an exit at market, checked once per 15m run. Stops and targets on the exchange act on
    their own between runs. With stale / incomplete candles (fresh False) only the clock-based time stop applies.
    `last15`: the candle from invalidation_bar()."""
    held_h = (now_ms - s.entry_ms) / H1_MS
    if held_h >= p.max_hold_hours:
        return "time_stop", f"held {held_h:.1f}h >= {p.max_hold_hours}h"
    if not fresh:
        return None, "candles stale: only the time stop is checked"
    mfe_r = (s.best - s.entry) * s.direction / s.r if s.r else 0.0
    if s.stage == "initial" and held_h >= p.no_progress_hours and mfe_r < p.no_progress_mfe_r:
        return "no_progress", f"{held_h:.1f}h held, best move {mfe_r:.2f} R < {p.no_progress_mfe_r} R"
    if s.stage == "initial" and last15 is not None and s.invalidation is not None:
        if (last15.close - s.invalidation) * s.direction < 0:
            return "invalidation", f"{p.invalidation_timeframe} close {last15.close} beyond the setup level {s.invalidation}"
    return None, ""


def next_stop(s: ManageState, atr1h: float, atr15: float, p: Params, tp1_done: bool) -> tuple[str, float | None]:
    """(stage, new stop or None) after this run's bars: break-even after TP1 (two legs) or after MFE >= be_trigger_r
    R (one leg), then the ATR trail; only ever moved in the trade's favour."""
    stage = s.stage
    mfe_r = (s.best - s.entry) * s.direction / s.r if s.r else 0.0
    if stage == "initial" and ((s.two_legs and tp1_done) or (not s.two_legs and mfe_r >= p.be_trigger_r)):
        stage = "runner"
    if stage != "runner":
        return stage, None
    target = break_even(s.direction, s.entry, s.cost_unit)
    trail = trail_level(s.direction, s.best, atr1h, p)
    if (trail - target) * s.direction > 0:
        target = trail
    if (target - s.stop) * s.direction <= 0:
        return stage, None
    if s.stage == "runner" and not improved(s.direction, s.stop, target, atr15, p):
        return stage, None
    return stage, target


# ---------------------------------------------------------------- sizing (shared by live and backtest)
def position_size(cfg: Any, equity: float, frac: float, ref: float, r: float, inst: Any) -> dict[str, Any]:
    """Quantity for an entry: the owner's position map (strategy.size_tiers x risk.notional_multiple_full_tier) at the
    lowest isolated leverage that fits (risk.trade_leverage, cut when the stop is too wide for the liquidation
    distance), or risk_per_trade_pct sizing when notional_multiple_full_tier is null. Rounded DOWN to the
    instrument's quantity step."""
    from perpbot.risk import quantize_qty, trade_leverage

    rk = cfg.risk
    sl_pct = 1.01 * r / ref if ref > 0 else 0.0
    if rk.notional_multiple_full_tier:
        want = float(rk.notional_multiple_full_tier) * frac
        lev, mult, note = trade_leverage(multiple=want, sl_pct=sl_pct, max_leverage=int(rk.leverage),
                                         margin_use_pct=float(rk.max_margin_use_pct),
                                         liq_multiple=float(rk.liq_min_sl_multiple), inst=inst, notional=equity * want,
                                         mmr_divisor=float(rk.liq_estimate_mmr_divisor))
        qty = quantize_qty(equity * mult / ref, inst.quantity_decimals) if (lev >= 1 and ref > 0) else quantize_qty(0, 0)
        return {"mode": "position", "wanted_multiple": want, "multiple": mult, "leverage": lev, "note": note, "qty": qty}
    risk_usd = equity * float(rk.risk_per_trade_pct) / 100.0 * frac
    qty_f = min(risk_usd / r, equity * float(rk.notional_cap_pct_equity) / 100.0 / ref, equity * int(rk.leverage) / ref)
    qty = quantize_qty(qty_f, inst.quantity_decimals)
    return {"mode": "risk", "wanted_multiple": None, "multiple": float(qty) * ref / equity if equity else 0.0,
            "leverage": int(rk.leverage), "note": None, "qty": qty}


def split_legs(qty: Any, ref: float, inst: Any, tp1_fraction: float) -> list[tuple[str, Any]]:
    """[("B", runner), ("A", TP1 part)] when both parts reach the exchange minimum; else [("B", all)]."""
    from perpbot.risk import quantize_qty

    a = quantize_qty(float(qty) * float(tp1_fraction), inst.quantity_decimals)
    b = qty - a
    if a > 0 and b > 0 and float(a) * ref >= inst.min_notional and float(b) * ref >= inst.min_notional:
        return [("B", b), ("A", a)]
    return [("B", qty)]


def blackout(release_ms: Sequence[int], t_ms: int, before_min: float, after_min: float) -> bool:
    return any(r - before_min * 60_000 <= t_ms <= r + after_min * 60_000 for r in release_ms)
