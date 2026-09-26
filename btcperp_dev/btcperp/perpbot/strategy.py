"""Hybrid trend/structure strategy: score, size tiers, gates and the daily plan.

Pure functions only: no I/O. Both the live engine and the shadow simulator
use `decide_plan`, so the rules are implemented exactly once.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Sequence

from perpbot.indicators import Candle, atr_wilder, clip, clv, ema, last_value, percentile_rank
from perpbot.timeutil import DAY_MS, day_start_ms


class InsufficientData(Exception):
    pass


# ---------------------------------------------------------------- score

@dataclass
class ScoreResult:
    decision_day: str          # UTC day D (ISO) the decision is made on
    candle_open_ms: int        # the closed daily candle used (day D-1)
    close: float
    high: float
    low: float
    prev_high: float
    prev_low: float
    ema_trend: float
    ema_regime: float
    atr: float
    clv: float
    trend_component: float
    breakout_component: float
    clv_component: float
    structure_component: float
    score: float
    direction: int
    abs_score: float
    tier_fraction: float
    candles_used: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def closed_candles_before(candles: Sequence[Candle], cutoff_ms: int) -> list[Candle]:
    """Candles fully closed at cutoff_ms (open + interval <= cutoff), sorted, de-duplicated."""
    seen: dict[int, Candle] = {}
    for c in candles:
        end = c.close_ms if c.close_ms else c.open_ms + DAY_MS
        if end <= cutoff_ms:
            seen[c.open_ms] = c
    return [seen[k] for k in sorted(seen)]


def direction_of(score: float) -> int:
    if score > 0:
        return 1
    if score < 0:
        return -1
    return 0


def tier_fraction(abs_score: float, s: Any) -> float:
    """Size tier as a fraction of the risk budget. abs_score must be > 0."""
    if abs_score < s.tier_low_max:
        return float(s.tier_low_fraction)
    if abs_score <= s.tier_mid_max:
        return float(s.tier_mid_fraction)
    return float(s.tier_high_fraction)


def compute_score(daily: Sequence[Candle], decision_day: date, s: Any) -> ScoreResult:
    """Score for UTC day `decision_day` from daily candles closed before its 00:00 UTC."""
    closed = closed_candles_before(daily, day_start_ms(decision_day))
    need = max(s.min_daily_candles, s.ema_regime_period, s.atr_period + 1, 2)
    if len(closed) < need:
        raise InsufficientData(f"need {need} closed daily candles, have {len(closed)}")
    last = closed[-1]
    expected_open = day_start_ms(decision_day) - DAY_MS
    if last.open_ms != expected_open:
        raise InsufficientData(
            f"latest closed daily candle opens at {last.open_ms}, expected {expected_open} (data not up to date)")
    prev = closed[-2]
    closes = [c.close for c in closed]
    ema_t = last_value(ema(closes, s.ema_trend_period))
    ema_r = last_value(ema(closes, s.ema_regime_period))
    atr = last_value(atr_wilder(closed, s.atr_period))
    if atr <= 0:
        raise InsufficientData("ATR is zero")
    c, h, l = last.close, last.high, last.low
    trend = s.trend_weight * clip((c - ema_t) / (s.trend_atr_multiple * atr), -1.0, 1.0)
    if c > prev.high:
        breakout = float(s.breakout_points)
    elif c < prev.low:
        breakout = -float(s.breakout_points)
    else:
        breakout = 0.0
    clv_v = clv(h, l, c)
    clv_comp = s.clv_weight * clv_v
    structure = breakout + clv_comp
    score = round(trend + structure, s.score_round_decimals)
    if score == 0:
        score = 0.0  # normalise -0.0
    d = direction_of(score)
    a = abs(score)
    frac = tier_fraction(a, s) if d != 0 else 0.0
    return ScoreResult(
        decision_day=decision_day.isoformat(), candle_open_ms=last.open_ms, close=c, high=h, low=l,
        prev_high=prev.high, prev_low=prev.low, ema_trend=ema_t, ema_regime=ema_r, atr=atr, clv=clv_v,
        trend_component=trend, breakout_component=breakout, clv_component=clv_comp,
        structure_component=structure, score=score, direction=d, abs_score=a, tier_fraction=frac,
        candles_used=len(closed))


def direction_history(daily: Sequence[Candle], today: date, days: int, s: Any) -> dict[str, int]:
    """Signal direction for today and the previous days-1 UTC days (from candle data, not runs)."""
    out: dict[str, int] = {}
    for i in range(days):
        d = today - timedelta(days=i)
        try:
            out[d.isoformat()] = compute_score(daily, d, s).direction
        except InsufficientData:
            break
    return out


def opposite_streak(position_dir: int, entry_day: date | None, directions: dict[str, int], today: date) -> int:
    """Consecutive UTC days ending today whose signal is opposite to the position,
    counting only days after the position's entry day."""
    if position_dir == 0:
        return 0
    n = 0
    d = today
    while True:
        if entry_day is not None and d <= entry_day:
            break
        if directions.get(d.isoformat()) != -position_dir:
            break
        n += 1
        d -= timedelta(days=1)
    return n


# ---------------------------------------------------------------- gates

@dataclass
class GateResult:
    name: str
    triggered: bool
    cap: float | None = None
    blocks_entry: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


def regime_gate(close: float, ema_regime: float, direction: int, cap: float) -> GateResult:
    regime = "bull" if close > ema_regime else "bear" if close < ema_regime else "neutral"
    against = (direction > 0 and regime == "bear") or (direction < 0 and regime == "bull")
    return GateResult("ema200_regime", against, cap if against else None, False,
                      {"regime": regime, "close": close, "ema_regime": ema_regime})


def h4_gate(ema_fast: float, ema_slow: float, candle_open_ms: int, direction: int, cap: float) -> GateResult:
    trend = "up" if ema_fast > ema_slow else "down" if ema_fast < ema_slow else "flat"
    disagree = (direction > 0 and trend == "down") or (direction < 0 and trend == "up")
    return GateResult("h4_trend", disagree, cap if disagree else None, False,
                      {"trend": trend, "ema_fast": ema_fast, "ema_slow": ema_slow, "candle_open_ms": candle_open_ms})


def h4_emas(h4: Sequence[Candle], decision_day: date, fast: int, slow: int) -> tuple[float, float, int]:
    """EMA fast/slow on 4h closes up to the last 4h candle closed at D 00:00 UTC (20:00-00:00)."""
    cutoff = day_start_ms(decision_day)
    closed = [c for c in h4 if (c.close_ms if c.close_ms else c.open_ms + 4 * 3_600_000) <= cutoff]
    closed.sort(key=lambda c: c.open_ms)
    if len(closed) < slow:
        raise InsufficientData(f"need {slow} closed 4h candles, have {len(closed)}")
    expected_open = cutoff - 4 * 3_600_000
    if closed[-1].open_ms != expected_open:
        raise InsufficientData("last closed 4h candle is not the 20:00-00:00 UTC candle")
    closes = [c.close for c in closed]
    return last_value(ema(closes, fast)), last_value(ema(closes, slow)), closed[-1].open_ms


@dataclass
class FundingStat:
    current_rate: float
    current_ts_ms: int
    percentile: float
    samples: int


def funding_percentile(records: Sequence[tuple[int, float]], cutoff_ms: int, lookback_days: int) -> FundingStat:
    """Point-in-time percentile of the latest funding print at/before cutoff within the lookback window."""
    window = sorted((ts, r) for ts, r in records if cutoff_ms - lookback_days * DAY_MS < ts <= cutoff_ms)
    if not window:
        raise InsufficientData("no funding records in lookback window")
    span_days = (window[-1][0] - window[0][0]) / DAY_MS
    if span_days < lookback_days - 2:
        raise InsufficientData(f"funding history covers {span_days:.1f} days, need {lookback_days}")
    cur_ts, cur = window[-1]
    pct = percentile_rank([r for _, r in window], cur)
    return FundingStat(current_rate=cur, current_ts_ms=cur_ts, percentile=pct, samples=len(window))


def funding_gate(pct: float, direction: int, high: float, low: float) -> GateResult:
    block = (direction > 0 and pct > high) or (direction < 0 and pct < low)
    return GateResult("extreme_funding", block, None, block, {"percentile": pct, "high": high, "low": low})


def event_gate(active: list[dict[str, Any]]) -> GateResult:
    return GateResult("event_window", bool(active), None, bool(active), {"events": active})


# ---------------------------------------------------------------- plan

@dataclass
class DecisionContext:
    direction: int
    abs_score: float
    tier_fraction: float
    caps: list[tuple[str, float]]          # triggered size caps
    funding_pct: float | None
    funding_high: float
    funding_low: float
    event_active: bool
    position_dir: int
    opposite_streak: int
    entered_today: bool
    paused_reason: str | None              # manual pause / kill switch: no strategy actions
    flip_min_abs_score: float
    opposite_days_rule: int
    event_allows_rule_closes: bool


@dataclass
class Plan:
    action: str                    # paused | hold | none | enter | close | flip | close_then_enter
    target_direction: int
    close_reason: str | None = None
    enter_direction: int = 0
    enter_fraction: float = 0.0
    entry_blocked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _funding_blocks(ctx: DecisionContext, direction: int) -> bool:
    if ctx.funding_pct is None:
        return False
    return (direction > 0 and ctx.funding_pct > ctx.funding_high) or (
        direction < 0 and ctx.funding_pct < ctx.funding_low)


def evaluate_entry(ctx: DecisionContext) -> tuple[int, float, list[str]]:
    blocked: list[str] = []
    if ctx.direction == 0:
        return 0, 0.0, ["score is exactly 0 (no signal)"]
    if ctx.entered_today:
        blocked.append("already entered this UTC day (max one entry per day)")
    if ctx.event_active:
        blocked.append("event window (FOMC/CPI/NFP): no new positions")
    if _funding_blocks(ctx, ctx.direction):
        side = "long" if ctx.direction > 0 else "short"
        blocked.append(f"extreme funding percentile {ctx.funding_pct:.2f}: no new {side}")
    if blocked:
        return 0, 0.0, blocked
    fraction = min([ctx.tier_fraction] + [c for _, c in ctx.caps])  # minimum, never multiply
    return ctx.direction, fraction, []


def decide_plan(ctx: DecisionContext) -> Plan:
    if ctx.paused_reason:
        return Plan("paused", ctx.position_dir, notes=[f"paused: {ctx.paused_reason}; position and SL/TP unchanged"])
    pos = ctx.position_dir
    close_reason = None
    notes: list[str] = []
    if pos != 0:
        crowded = ctx.funding_pct is not None and (
            (pos > 0 and ctx.funding_pct > ctx.funding_high) or (pos < 0 and ctx.funding_pct < ctx.funding_low))
        if crowded:
            if ctx.event_active and not ctx.event_allows_rule_closes:
                notes.append("funding rule suppressed by event window")
            else:
                close_reason = "funding_rule"
        elif ctx.opposite_streak >= ctx.opposite_days_rule:
            if ctx.event_active and not ctx.event_allows_rule_closes:
                notes.append("3-day rule suppressed by event window")
            else:
                close_reason = "three_day_rule"
        elif ctx.direction == -pos and ctx.abs_score >= ctx.flip_min_abs_score:
            if ctx.event_active:
                notes.append("event window: flip suppressed, holding")
            else:
                close_reason = "flip"
        elif ctx.direction == -pos:
            notes.append("weak opposite signal: hold, SL/TP unchanged")
        elif ctx.direction == pos:
            notes.append("same direction: hold")
        else:
            notes.append("score 0: hold unchanged")
        if close_reason is None:
            return Plan("hold", pos, notes=notes)

    enter_dir, frac, blocked = evaluate_entry(ctx)
    if close_reason is None:
        if enter_dir == 0:
            return Plan("none", 0, entry_blocked=blocked, notes=notes)
        return Plan("enter", enter_dir, enter_direction=enter_dir, enter_fraction=frac, notes=notes)
    if enter_dir == 0:
        return Plan("close", 0, close_reason=close_reason, entry_blocked=blocked, notes=notes)
    action = "flip" if enter_dir == -pos else "close_then_enter"
    return Plan(action, enter_dir, close_reason=close_reason, enter_direction=enter_dir,
                enter_fraction=frac, notes=notes)


def sl_is_tighter(direction: int, old_sl: float, new_sl: float) -> bool:
    return new_sl > old_sl if direction > 0 else new_sl < old_sl


def bracket_prices(direction: int, ref_price: float, atr: float, sl_mult: float, tp_mult: float) -> tuple[float, float]:
    sl = ref_price - direction * sl_mult * atr
    tp = ref_price + direction * tp_mult * atr
    return sl, tp
