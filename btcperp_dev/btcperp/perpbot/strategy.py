"""Hybrid trend/structure strategy: score, size tiers, gates and the plan.

Pure functions only: no I/O. The live engine, the shadow simulator and the backtest all use
`day_features` / `period_features` + `plan_for` (-> `decide_plan`), so the rules are implemented exactly once.

Cadence (strategy.cadence):
- daily: one decision per UTC day D, on Binance daily candles closed at D 00:00 UTC.
- rolling_4h (v1.5.0, option B): one decision per 4-hour period starting at T (UTC 00/04/08/12/16/20), on daily
  candles that END at T (built from 4h candles). Same rules and parameters; the "day" simply ends at T instead of
  at 00:00 UTC. The 3-day rule counts 3 x 6 periods; a flip may need the opposite signal in consecutive periods.
- rolling_1h (v1.9.0, owner): the same every hour, on daily candles ending at each UTC hour, built from 1h candles;
  the 3-day rule counts 3 x 24 periods. The h4 gate uses the last 4h candle closed at T.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Sequence

from bisect import bisect_left, bisect_right
from datetime import datetime, timezone

from perpbot.indicators import Candle, atr_wilder, clip, clv, ema, last_value, percentile_rank
from perpbot.timeutil import DAY_MS, HOUR_MS, MINUTE_MS, day_start_ms, from_ms, to_ms

H4_MS = 4 * HOUR_MS
CADENCES = ("daily", "rolling_4h", "rolling_1h")
# Period length of each rolling cadence. rolling_2h (v1.6.0) exists in the backtest only (variant R2h_live); live
# config accepts CADENCES. rolling_1h (v1.9.0) is live and in the backtest (R1h_live).
ROLLING_PERIOD_MS = {"rolling_4h": H4_MS, "rolling_2h": 2 * HOUR_MS, "rolling_1h": HOUR_MS}


def base_bar_ms(cadence: str) -> int:
    """Length of the candles the daily candles ending at a period boundary are built from: 4h candles when every
    boundary is a 4h one, 1h candles otherwise."""
    return H4_MS if ROLLING_PERIOD_MS[cadence] % H4_MS == 0 else HOUR_MS


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


def size_table(s: Any) -> list[tuple[float, float]] | None:
    """v1.9.0 `strategy.size_tiers`: [[from |score|, fraction], ...] ascending, first from 0; null = the 3 tiers."""
    get = getattr(s, "get", None)
    table = get("size_tiers") if callable(get) else getattr(s, "size_tiers", None)
    return [(float(a), float(f)) for a, f in table] if table else None


def tier_fraction(abs_score: float, s: Any) -> float:
    """Size tier as a fraction of the risk budget (or of the full-tier position). abs_score must be > 0."""
    table = size_table(s)
    if table:
        frac = table[0][1]
        for lo, f in table:
            if abs_score >= lo:
                frac = f
        return frac
    if abs_score < s.tier_low_max:
        return float(s.tier_low_fraction)
    if abs_score <= s.tier_mid_max:
        return float(s.tier_mid_fraction)
    return float(s.tier_high_fraction)


def compute_score(daily: Sequence[Candle], decision_day: date, s: Any, *, cutoff_ms: int | None = None,
                  label: str | None = None) -> ScoreResult:
    """Score for UTC day `decision_day` from daily candles closed before its 00:00 UTC. With `cutoff_ms` (rolling
    cadence): from daily candles closed at cutoff_ms, the last one opening exactly one day before it."""
    cut = day_start_ms(decision_day) if cutoff_ms is None else int(cutoff_ms)
    closed = closed_candles_before(daily, cut)
    need = max(s.min_daily_candles, s.ema_regime_period, s.atr_period + 1, 2)
    if len(closed) < need:
        raise InsufficientData(f"need {need} closed daily candles, have {len(closed)}")
    last = closed[-1]
    expected_open = cut - DAY_MS
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
        decision_day=label or decision_day.isoformat(), candle_open_ms=last.open_ms, close=c, high=h, low=l,
        prev_high=prev.high, prev_low=prev.low, ema_trend=ema_t, ema_regime=ema_r, atr=atr, clv=clv_v,
        trend_component=trend, breakout_component=breakout, clv_component=clv_comp,
        structure_component=structure, score=score, direction=d, abs_score=a, tier_fraction=frac,
        candles_used=len(closed))


def score_history(daily: Sequence[Candle], today: date, days: int, s: Any) -> dict[str, float]:
    """Score for today and the previous days-1 UTC days (daily cadence)."""
    out: dict[str, float] = {}
    for i in range(days):
        d = today - timedelta(days=i)
        try:
            out[d.isoformat()] = compute_score(daily, d, s).score
        except InsufficientData:
            break
    return out


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


def h4_emas(h4: Sequence[Candle], decision_day: date, fast: int, slow: int, *,
            cutoff_ms: int | None = None) -> tuple[float, float, int]:
    """EMA fast/slow on 4h closes up to the last 4h candle closed at D 00:00 UTC (20:00-00:00), or at cutoff_ms."""
    cutoff = day_start_ms(decision_day) if cutoff_ms is None else int(cutoff_ms)
    closed = [c for c in h4 if (c.close_ms if c.close_ms else c.open_ms + 4 * 3_600_000) <= cutoff]
    closed.sort(key=lambda c: c.open_ms)
    if len(closed) < slow:
        raise InsufficientData(f"need {slow} closed 4h candles, have {len(closed)}")
    expected_open = cutoff - 4 * 3_600_000
    if closed[-1].open_ms != expected_open:
        raise InsufficientData("last closed 4h candle does not end at the decision time")
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
    entry_block: str | None = None         # blocks NEW positions only (calendar expired, clock skew); closes still run
    funding_rule_closes: bool = True       # backtest variant: False = crowded funding blocks entries but never closes
    flip_confirmed: bool = True            # rolling_4h with flip_confirm_periods > 1: earlier periods agree
    period_word: str = "UTC day"           # for messages
    min_entry_abs_score: float = 0.0       # v1.8.0 owner: no new position below this |score|


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
    if ctx.abs_score < ctx.min_entry_abs_score:
        blocked.append(f"score {ctx.abs_score:.1f} below the entry threshold {ctx.min_entry_abs_score:g}")
    if ctx.entered_today:
        blocked.append(f"already entered this {ctx.period_word} (max one entry per {ctx.period_word})")
    if ctx.entry_block:
        blocked.append(ctx.entry_block)
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
        if crowded and not ctx.funding_rule_closes:
            notes.append("crowded funding: hold (variant without the funding close)")
        if crowded and ctx.funding_rule_closes:
            if ctx.event_active and not ctx.event_allows_rule_closes:
                notes.append("funding rule suppressed by event window")
            else:
                close_reason = "funding_rule"
        elif ctx.opposite_streak >= ctx.opposite_days_rule:
            if ctx.event_active and not ctx.event_allows_rule_closes:
                notes.append("3-day rule suppressed by event window")
            else:
                close_reason = "three_day_rule"
        elif ctx.direction == -pos and ctx.abs_score >= ctx.flip_min_abs_score and not ctx.flip_confirmed:
            notes.append("opposite signal not yet confirmed by the previous period: hold, flip needs confirmation")
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


def _opt(section: Any, name: str, default: Any) -> Any:
    get = getattr(section, "get", None)
    v = get(name) if callable(get) else getattr(section, name, None)
    return default if v is None else v


def exit_distances(cfg: Any, mark: float, daily_atr: float, h1: Sequence[Candle] | None = None,
                   t_ms: int | None = None, opens: Sequence[int] | None = None) -> dict[str, Any]:
    """SL / TP distances in price for an entry at `mark` (v1.10.0, owner: "the target is too far for the swings").
    exits.atr_source "1h": the multiples apply to the Wilder ATR of the 1h candles closed at t_ms (the period start),
    so the brackets follow the current hourly swings; "daily" (before v1.10.0): to the daily ATR of the score.
    Then at least exits.sl_min_pct / tp_min_pct of the price (the round-trip fees must be covered). Falls back to the
    daily ATR when there are not enough 1h candles. `h1` sorted by open time; `opens` = their open times."""
    ex = cfg.exits
    src = str(_opt(ex, "atr_source", "daily"))
    atr, used = float(daily_atr), "daily"
    if src == "1h" and h1:
        period = int(ex.atr_1h_period)
        op = opens if opens is not None else [c.open_ms for c in h1]
        end = bisect_right(op, int(t_ms) - HOUR_MS) if t_ms is not None else len(h1)   # closed by t_ms
        window = list(h1[max(0, end - period * period):end])        # Wilder warm-up: (13/14)^182 ~ 0
        a = last_value(atr_wilder(window, period)) if len(window) > period else None
        if a:
            atr, used = float(a), "1h"
    sl = max(float(ex.sl_atr_multiple) * atr, float(_opt(ex, "sl_min_pct", 0.0)) / 100.0 * float(mark))
    tp = max(float(ex.tp_atr_multiple) * atr, float(_opt(ex, "tp_min_pct", 0.0)) / 100.0 * float(mark))
    return {"source": used, "atr": atr, "sl_dist": sl, "tp_dist": tp,
            "sl_pct": sl / float(mark) * 100.0 if mark else None, "tp_pct": tp / float(mark) * 100.0 if mark else None}


def restrict_to_close(plan: Plan, reason: str, position_dir: int) -> Plan:
    """Drop the entry part of a plan (region blocked, late decision): closes still happen."""
    if plan.enter_direction:
        plan.entry_blocked.append(reason)
        plan.enter_direction, plan.enter_fraction = 0, 0.0
        plan.action = "close" if plan.close_reason else "none"
        plan.target_direction = 0 if plan.close_reason else position_dir
    return plan


def hold_for_bold(plan: Plan, position_dir: int) -> Plan:
    """v1.7.0 bold mode (live only): an open bet ends only at its TP or SL. Strategy exits (flip, 3-day, funding,
    flat rules) and entries while it is open are dropped; a flat account decides as usual."""
    if position_dir and plan.action not in ("hold", "paused", "none"):
        plan.notes.append(f"bold mode: TP/SL only, {plan.action} ignored"
                          + (f" ({plan.close_reason})" if plan.close_reason else ""))
        plan.action, plan.close_reason = "hold", None
        plan.enter_direction, plan.enter_fraction = 0, 0.0
        plan.target_direction = position_dir
    return plan


# ---------------------------------------------------------------- one day, shared by engine and backtest

@dataclass
class DayFeatures:
    """Everything about UTC day D that does not depend on the position."""
    day: date
    score: ScoreResult
    h4_fast: float
    h4_slow: float
    h4_open_ms: int
    funding: FundingStat
    directions: dict[str, int]
    events: list[dict[str, Any]]
    g_regime: GateResult
    g_h4: GateResult
    g_funding: GateResult
    g_event: GateResult
    caps: list[tuple[str, float]]
    cadence: str = "daily"
    key: str = ""                          # period key: "YYYY-MM-DD" (daily) or "YYYY-MM-DDTHH:00" (rolling_4h)
    period_ms: int = 0                     # decision time T (UTC ms): D 00:00 or the 4h boundary
    scores: dict[str, float] = field(default_factory=dict)   # score per period key, newest included (flip confirm)
    daily_used: list[Candle] = field(default_factory=list)   # rolling_4h: the daily candles ending at T (for logs)

    @property
    def gates(self) -> list[GateResult]:
        return [self.g_regime, self.g_h4, self.g_funding, self.g_event]

    @property
    def event_active(self) -> bool:
        return self.g_event.triggered

    def gates_triggered(self) -> list[str]:
        return [g.name for g in self.gates if g.triggered]


def day_features(cfg: Any, day_d: date, daily: Sequence[Candle], h4: Sequence[Candle],
                 funding_records: Sequence[tuple[int, float]], events: list[dict[str, Any]]) -> DayFeatures:
    """Score, gates and signal history for UTC day `day_d` from data closed before D 00:00 UTC.
    `events`: event windows active at the decision time (from the calendar)."""
    s, g = cfg.strategy, cfg.gates
    sc = compute_score(daily, day_d, s)
    e_fast, e_slow, h4_open = h4_emas(h4, day_d, int(g.h4_ema_fast), int(g.h4_ema_slow))
    cutoff = day_start_ms(day_d) + int(float(g.funding_cutoff_tolerance_minutes) * MINUTE_MS)
    fstat = funding_percentile(funding_records, cutoff, int(g.funding_lookback_days))
    scores = score_history(daily, day_d, int(s.opposite_days_rule) + 2, s)
    dirs = {k: direction_of(v) for k, v in scores.items()}
    g_regime = regime_gate(sc.close, sc.ema_regime, sc.direction, float(g.regime_cap))
    g_h4 = h4_gate(e_fast, e_slow, h4_open, sc.direction, float(g.h4_cap))
    g_fund = funding_gate(fstat.percentile, sc.direction, float(g.funding_high_percentile), float(g.funding_low_percentile))
    caps = [(x.name, float(x.cap)) for x in (g_regime, g_h4) if x.triggered and x.cap is not None]
    return DayFeatures(day_d, sc, e_fast, e_slow, h4_open, fstat, dirs, events, g_regime, g_h4, g_fund,
                       event_gate(events), caps, "daily", day_d.isoformat(), day_start_ms(day_d), scores)


def plan_for(f: DayFeatures, cfg: Any, *, position_dir: int, entry_day: date | None, entered_today: bool,
             paused_reason: str | None, entry_block: str | None = None, ungated: bool = False,
             funding_rule_closes: bool = True, ignore_events: bool = False, entry_key: str | None = None,
             flip_confirm_periods: int | None = None) -> tuple[Plan, DecisionContext]:
    """`entry_day` (daily) / `entry_key` (rolling_4h: the entry period key) of the open position."""
    s, g = cfg.strategy, cfg.gates
    sc = f.score
    rolling = f.cadence in ROLLING_PERIOD_MS
    if rolling:
        per_day = DAY_MS // ROLLING_PERIOD_MS[f.cadence]
        streak = opposite_streak_keys(position_dir, entry_key, f.directions, f.key)
        rule = int(s.opposite_days_rule) * per_day
        k = int(flip_confirm_periods if flip_confirm_periods is not None else s.flip_confirm_periods)
        confirmed = flip_confirmed(position_dir, f.scores, f.key, k, float(s.flip_min_abs_score))
    else:
        streak = opposite_streak(position_dir, entry_day, f.directions, f.day)
        rule = int(s.opposite_days_rule)
        confirmed = True
    ctx = DecisionContext(
        direction=sc.direction, abs_score=sc.abs_score, tier_fraction=sc.tier_fraction,
        caps=[] if ungated else list(f.caps),
        funding_pct=None if ungated else f.funding.percentile,
        funding_high=float(g.funding_high_percentile), funding_low=float(g.funding_low_percentile),
        event_active=False if (ungated or ignore_events) else f.event_active, position_dir=position_dir,
        opposite_streak=streak,
        entered_today=entered_today, paused_reason=paused_reason,
        flip_min_abs_score=float(s.flip_min_abs_score), opposite_days_rule=rule,
        event_allows_rule_closes=bool(g.event_allows_rule_closes), entry_block=entry_block,
        funding_rule_closes=funding_rule_closes, flip_confirmed=confirmed,
        period_word=f"{ROLLING_PERIOD_MS[f.cadence] // HOUR_MS}-hour period" if rolling else "UTC day",
        min_entry_abs_score=float(s.min_entry_abs_score))
    return decide_plan(ctx), ctx


# ---------------------------------------------------------------- rolling 4h cadence (v1.5.0, option B)

def period_key(t_ms: int) -> str:
    """'YYYY-MM-DDTHH:00' (UTC) of a period boundary."""
    return from_ms(int(t_ms)).strftime("%Y-%m-%dT%H:00")


def period_start(t_ms: int, period_ms: int = H4_MS) -> int:
    """The period boundary at or before t_ms (4h: UTC 00/04/08/12/16/20; 1h: every UTC hour)."""
    return int(t_ms) // int(period_ms) * int(period_ms)


def key_ms(key: str) -> int:
    """Inverse of period_key (also accepts a plain 'YYYY-MM-DD' = 00:00 UTC)."""
    k = key if "T" in key else key + "T00:00"
    return to_ms(datetime.fromisoformat(k).replace(tzinfo=timezone.utc))


def shifted_daily(h4: Sequence[Candle], cutoff_ms: int, count: int, bar_ms: int = H4_MS) -> list[Candle]:
    """Daily candles that END exactly at cutoff_ms (a 4h boundary): candle k covers
    [cutoff - (count-k)*1d, cutoff - (count-k-1)*1d), built from the 4h candles inside it (open of the first,
    highest high, lowest low, close of the last, summed volume). A day without any 4h candle is left out.
    `h4` must be sorted by open time. At cutoff 00:00 UTC this equals Binance's own daily candles.
    `bar_ms`: the length of the input candles (v1.6.0: 1h candles for a 2h boundary in the backtest)."""
    start = int(cutoff_ms) - int(count) * DAY_MS
    opens = [c.open_ms for c in h4]
    i, j = bisect_left(opens, start), bisect_left(opens, int(cutoff_ms))
    out: list[Candle] = []
    cur_k, bucket = None, []  # type: ignore[var-annotated]

    def flush() -> None:
        if bucket:
            o = start + cur_k * DAY_MS
            out.append(Candle(o, bucket[0].open, max(c.high for c in bucket), min(c.low for c in bucket),
                              bucket[-1].close, sum(c.volume for c in bucket), o + DAY_MS))

    for c in h4[i:j]:
        if c.open_ms + bar_ms > cutoff_ms:
            continue
        k = (c.open_ms - start) // DAY_MS
        if k != cur_k:
            flush()
            cur_k, bucket = k, []
        bucket.append(c)
    flush()
    return out


def h4_gate_window(h4_sorted: Sequence[Candle], t_ms: int, n: int, opens: Sequence[int] | None = None) -> list[Candle]:
    """The last n 4h candles closed at t_ms (what the live decide loads for the h4 gate). `opens`: the open times
    of h4_sorted, when the caller already has them (backtest)."""
    op = opens if opens is not None else [c.open_ms for c in h4_sorted]
    j = bisect_right(op, int(t_ms) - H4_MS)
    return list(h4_sorted[max(0, j - n):j])


def rolling_score(daily_shifted: Sequence[Candle], t_ms: int, s: Any) -> ScoreResult:
    """The daily-rule score at the 4h boundary t_ms, from daily candles ending at t_ms."""
    d = from_ms(int(t_ms)).date()
    return compute_score(daily_shifted, d, s, cutoff_ms=int(t_ms), label=period_key(t_ms))


def opposite_streak_keys(position_dir: int, entry_key: str | None, directions: dict[str, int], current_key: str) -> int:
    """Consecutive periods ending at current_key whose signal is opposite to the position, counting only periods
    after the entry period. A daily entry key 'YYYY-MM-DD' counts as that day's 00:00 UTC period."""
    if position_dir == 0:
        return 0
    ek = None if entry_key is None else (entry_key if "T" in entry_key else entry_key + "T00:00")
    n = 0
    for k in sorted((k for k in directions if k <= current_key), reverse=True):
        if ek is not None and k <= ek:
            break
        if directions[k] != -position_dir:
            break
        n += 1
    return n


def flip_confirmed(position_dir: int, scores: dict[str, float], current_key: str, periods: int,
                   flip_min: float) -> bool:
    """True when the newest `periods` periods (current included) all have an opposite signal of at least
    flip_min. periods <= 1: always True (no confirmation needed)."""
    if periods <= 1 or position_dir == 0:
        return True
    recent = sorted((k for k in scores if k <= current_key), reverse=True)[:periods]
    if len(recent) < periods:
        return False
    return all(direction_of(scores[k]) == -position_dir and abs(scores[k]) >= flip_min for k in recent)


def period_features(cfg: Any, t_ms: int, score_at: Any, h4: Sequence[Candle],
                    funding_records: Sequence[tuple[int, float]], events: list[dict[str, Any]],
                    cadence: str = "rolling_4h") -> DayFeatures:
    """Rolling features at the period boundary t_ms (4h; 2h for the backtest-only rolling_2h). `score_at(T)` returns
    the ScoreResult at boundary T (or raises InsufficientData); live computes it from the fetched 4h candles, the
    backtest from a cache of the same values. `h4`: the 4h candles available at t_ms (the h4 gate uses the ones
    closed at t_ms)."""
    per = ROLLING_PERIOD_MS[cadence]
    s, g = cfg.strategy, cfg.gates
    sc = score_at(t_ms)
    h4_cut = int(t_ms) // H4_MS * H4_MS            # rolling_2h at 02:00, 06:00, ...: the 4h candle closed at 00:00
    e_fast, e_slow, h4_open = h4_emas(h4, sc_day(t_ms), int(g.h4_ema_fast), int(g.h4_ema_slow), cutoff_ms=h4_cut)
    cutoff = int(t_ms) + int(float(g.funding_cutoff_tolerance_minutes) * MINUTE_MS)
    fstat = funding_percentile(funding_records, cutoff, int(g.funding_lookback_days))
    n = int(s.opposite_days_rule) * int(DAY_MS // per) + 2
    scores: dict[str, float] = {}
    for i in range(n):
        ti = int(t_ms) - i * per
        try:
            scores[period_key(ti)] = (sc if i == 0 else score_at(ti)).score
        except InsufficientData:
            break
    dirs = {k: direction_of(v) for k, v in scores.items()}
    g_regime = regime_gate(sc.close, sc.ema_regime, sc.direction, float(g.regime_cap))
    g_h4 = h4_gate(e_fast, e_slow, h4_open, sc.direction, float(g.h4_cap))
    g_fund = funding_gate(fstat.percentile, sc.direction, float(g.funding_high_percentile), float(g.funding_low_percentile))
    caps = [(x.name, float(x.cap)) for x in (g_regime, g_h4) if x.triggered and x.cap is not None]
    return DayFeatures(sc_day(t_ms), sc, e_fast, e_slow, h4_open, fstat, dirs, events, g_regime, g_h4, g_fund,
                       event_gate(events), caps, cadence, period_key(t_ms), int(t_ms), scores)


def live_rolling_features(cfg: Any, t_ms: int, h4: Sequence[Candle], funding_records: Sequence[tuple[int, float]],
                          events: list[dict[str, Any]], *, cadence: str = "rolling_4h",
                          base: Sequence[Candle] | None = None) -> DayFeatures:
    """Rolling features at T from fetched candles (live decide and preview); the backtest feeds `period_features`
    from cached scores of the same shifted candles. rolling_1h: `base` = the 1h candles the daily candles are built
    from; `h4` = the 4h candles for the h4 gate."""
    h4s = sorted(h4, key=lambda c: c.open_ms)
    bar = base_bar_ms(cadence)
    bases = h4s if bar == H4_MS or base is None else sorted(base, key=lambda c: c.open_ms)
    n_days = int(cfg.binance.daily_candles_to_load)
    cache: dict[int, Any] = {}

    def score_at(ti: int) -> Any:
        if ti not in cache:
            try:
                cache[ti] = rolling_score(shifted_daily(bases, ti, n_days, bar_ms=bar), ti, cfg.strategy)
            except InsufficientData as e:
                cache[ti] = e
        v = cache[ti]
        if isinstance(v, InsufficientData):
            raise v
        return v

    gate = h4_gate_window(h4s, t_ms, int(cfg.binance.h4_candles_to_load))
    return period_features(cfg, t_ms, score_at, gate, funding_records, events, cadence=cadence)


def sc_day(t_ms: int) -> date:
    return from_ms(int(t_ms)).date()
