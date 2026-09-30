"""Shadow tracking (simulation only, never places orders).

1. Gate shadow trades: every day a gate blocked or reduced the trade, record what
   the un-gated trade would have done (entry at decision mark, bracket SL/TP,
   exit at first SL/TP touch on Polymarket 1h candles or after N days).
2. Parallel strategy variants replayed from the logged daily decisions:
   - live_rules:   the live rules (sanity check vs. real fills)
   - v2_breakeven: live rules + one-time SL move to breakeven at +1 ATR
   - flat_allowed: |score| < tier_low_max means flat (close / no entry)
   - ungated:      live rules with all gates ignored
Results are in R (multiples of the full 100%-tier risk), after fees, funding over the holding
time (Polymarket funding prints, Binance as fallback) and the configured FOK entry slippage.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

from perpbot.exchange.base import taker_fee_for
from perpbot.strategy import DecisionContext, decide_plan
from perpbot.timeutil import DAY_MS, HOUR_MS, to_ms

VARIANTS = ("live_rules", "v2_breakeven", "flat_allowed", "ungated")


@dataclass
class SimTrade:
    variant: str
    entry_day: str
    direction: int
    fraction: float
    entry_price: float
    entry_ts_ms: int
    atr: float
    sl: float
    tp: float
    exit_price: float | None = None
    exit_ts_ms: int | None = None
    exit_reason: str | None = None
    r: float | None = None
    be_moved: bool = False


@dataclass
class SimState:
    trade: SimTrade | None = None
    closed: list[SimTrade] = field(default_factory=list)


def _day_records(store: Any) -> list[dict[str, Any]]:
    rows = store.query("SELECT utc_day, data FROM decisions WHERE score IS NOT NULL ORDER BY id")
    by_day: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = r["data"]
        if isinstance(d, dict) and d.get("plan"):
            by_day[r["utc_day"]] = d
    return [by_day[k] for k in sorted(by_day)]


def _candles(store: Any, start_ms: int) -> list[dict[str, Any]]:
    return store.query("SELECT open_ms, open, high, low, close FROM pm_klines_1h WHERE open_ms >= ? ORDER BY open_ms", [start_ms])


def _funding_series(store: Any) -> list[tuple[int, float]]:
    rows = store.query("SELECT fund_ts_ms AS ts, rate FROM pm_funding ORDER BY fund_ts_ms")
    if not rows:
        rows = store.query("SELECT fund_ts_ms AS ts, rate FROM bn_funding ORDER BY fund_ts_ms")
    return [(int(r["ts"]), float(r["rate"])) for r in rows]


def _r(t: SimTrade, sl_mult: float, fee_rate: float, funding: list[tuple[int, float]] | None = None) -> float:
    """R after fees and funding (review D16). Positive funding rate: longs pay, shorts receive."""
    risk_unit = sl_mult * t.atr
    gross = (t.exit_price - t.entry_price) * t.direction / risk_unit
    fees = 2 * fee_rate * t.entry_price / risk_unit
    paid = 0.0
    if funding and t.exit_ts_ms is not None:
        rate_sum = sum(r for ts, r in funding if t.entry_ts_ms < ts <= t.exit_ts_ms)
        paid = rate_sum * t.entry_price * t.direction / risk_unit
    return (gross - fees - paid) * t.fraction


def _slipped(mark: float, direction: int, cfg: Any) -> float:
    return mark * (1 + direction * float(cfg.exits.entry_slippage_bps) / 1e4)


def _walk(state: SimState, candles: list[dict[str, Any]], until_ms: int, v2: bool, be_atr: float) -> None:
    t = state.trade
    if t is None:
        return
    first_hour = (t.entry_ts_ms // HOUR_MS + 1) * HOUR_MS  # skip the partial entry hour
    for c in candles:
        if c["open_ms"] < first_hour or c["open_ms"] + HOUR_MS > until_ms:
            continue
        if t.exit_price is not None:
            break
        hi, lo = c["high"], c["low"]
        sl_hit = lo <= t.sl if t.direction > 0 else hi >= t.sl
        tp_hit = hi >= t.tp if t.direction > 0 else lo <= t.tp
        if sl_hit:  # conservative: SL first when both touch in one candle
            t.exit_price, t.exit_ts_ms, t.exit_reason = t.sl, c["open_ms"] + HOUR_MS, "BE" if t.be_moved else "SL"
        elif tp_hit:
            t.exit_price, t.exit_ts_ms, t.exit_reason = t.tp, c["open_ms"] + HOUR_MS, "TP"
        elif v2 and not t.be_moved:
            fav = hi - t.entry_price if t.direction > 0 else t.entry_price - lo
            if fav >= be_atr * t.atr:
                t.sl = t.entry_price
                t.be_moved = True
    if t.exit_price is not None:
        state.closed.append(t)
        state.trade = None


def _streak(dirs: dict[str, int], pos_dir: int, entry_day: str, today: str) -> int:
    n = 0
    d = date.fromisoformat(today)
    e = date.fromisoformat(entry_day)
    while d > e and dirs.get(d.isoformat()) == -pos_dir:
        n += 1
        d -= timedelta(days=1)
    return n


def simulate_variant(variant: str, days: list[dict[str, Any]], candles: list[dict[str, Any]], cfg: Any,
                     now_ms: int, fee_rate: float, funding: list[tuple[int, float]] | None = None) -> SimState:
    s, g, ex = cfg.strategy, cfg.gates, cfg.exits
    sl_mult, tp_mult = float(ex.sl_atr_multiple), float(ex.tp_atr_multiple)
    be_atr = float(cfg.shadow.breakeven_trigger_atr)
    v2 = variant == "v2_breakeven"
    state = SimState()
    dirs: dict[str, int] = {}
    for rec in days:
        sc, plan = rec["score"], rec["plan"]
        day = rec["utc_day"]
        ts = int(plan.get("decision_ts_ms") or 0)
        mark = float(plan.get("mark") or rec["inputs"].get("mark") or 0)
        if not ts or not mark:
            continue
        _walk(state, candles, ts, v2, be_atr)
        dirs[day] = int(sc["direction"])
        t = state.trade
        pos_dir = t.direction if t else 0
        ungated = variant == "ungated"
        ctx = DecisionContext(
            direction=int(sc["direction"]), abs_score=float(sc["abs_score"]), tier_fraction=float(sc["tier_fraction"]),
            caps=[] if ungated else [tuple(c) for c in plan.get("caps") or []],
            funding_pct=None if ungated else plan.get("funding_percentile"),
            funding_high=float(g.funding_high_percentile), funding_low=float(g.funding_low_percentile),
            event_active=False if ungated else bool(plan.get("event_active")), position_dir=pos_dir,
            opposite_streak=_streak(dirs, pos_dir, t.entry_day, day) if t else 0, entered_today=False,
            paused_reason=None, flip_min_abs_score=float(s.flip_min_abs_score),
            opposite_days_rule=int(s.opposite_days_rule), event_allows_rule_closes=bool(g.event_allows_rule_closes))
        p = decide_plan(ctx)
        close_reason, enter_dir, frac = p.close_reason, p.enter_direction, p.enter_fraction
        if variant == "flat_allowed" and float(sc["abs_score"]) < float(s.tier_low_max):
            enter_dir, frac = 0, 0.0
            if t is not None and close_reason is None:
                close_reason = "flat_rule"
        if t is not None and close_reason:
            t.exit_price, t.exit_ts_ms, t.exit_reason = mark, ts, close_reason
            state.closed.append(t)
            state.trade = None
        if enter_dir and state.trade is None:
            atr = float(sc["atr"])
            px = _slipped(mark, enter_dir, cfg)
            state.trade = SimTrade(variant, day, enter_dir, frac, px, ts, atr,
                                   px - enter_dir * sl_mult * atr, px + enter_dir * tp_mult * atr)
    _walk(state, candles, now_ms, v2, be_atr)
    for tr in state.closed:
        tr.r = _r(tr, sl_mult, fee_rate, funding)
    return state


def gate_trades(days: list[dict[str, Any]], candles: list[dict[str, Any]], cfg: Any, now_ms: int,
                fee_rate: float, funding: list[tuple[int, float]] | None = None) -> list[dict[str, Any]]:
    """Resolved hypothetical trades for days where a gate blocked or reduced the entry."""
    sl_mult, tp_mult = float(cfg.exits.sl_atr_multiple), float(cfg.exits.tp_atr_multiple)
    max_hold = float(cfg.shadow.gate_trade_max_hold_days) * DAY_MS
    out = []
    for rec in days:
        sc, plan = rec["score"], rec["plan"]
        if int(sc["direction"]) == 0:
            continue
        gated_blocks = [b for b in plan.get("entry_blocked") or [] if ("event" in b or "funding" in b or "region" in b)]
        reduced = plan.get("enter_direction") and float(plan.get("enter_fraction") or 0) < float(sc["tier_fraction"])
        if not gated_blocks and not reduced:
            continue
        ts = int(plan.get("decision_ts_ms") or 0)
        mark = float(plan.get("mark") or 0)
        if not ts or not mark:
            continue
        d, atr = int(sc["direction"]), float(sc["atr"])
        px = _slipped(mark, d, cfg)
        t = SimTrade("gate", rec["utc_day"], d, float(sc["tier_fraction"]), px, ts, atr,
                     px - d * sl_mult * atr, px + d * tp_mult * atr)
        st = SimState(trade=t)
        horizon = min(ts + int(max_hold), now_ms)
        _walk(st, candles, horizon, False, 0.0)
        if st.trade is not None:
            if now_ms < ts + max_hold:
                continue  # still open: resolve on a later run
            last = [c for c in candles if ts <= c["open_ms"] and c["open_ms"] + HOUR_MS <= ts + max_hold]
            if not last:
                continue
            t.exit_price, t.exit_ts_ms, t.exit_reason = last[-1]["close"], last[-1]["open_ms"] + HOUR_MS, "time"
        t.r = _r(t, sl_mult, fee_rate, funding)
        live_frac = float(plan.get("enter_fraction") or 0.0) if plan.get("enter_direction") else 0.0
        out.append({"utc_day": rec["utc_day"], "gates": plan.get("gates_triggered"), "blocked": gated_blocks,
                    "reduced": bool(reduced), "ungated_fraction": t.fraction, "live_fraction": live_frac,
                    "trade": asdict(t), "r_ungated": t.r,
                    "r_live_equivalent": (t.r / t.fraction * live_frac) if t.fraction else 0.0})
    return out


def _fee_rate(engine: Any) -> float:
    estimate = float(engine.cfg.shadow.fee_rate_estimate)
    try:
        cat = str(getattr(engine.instrument(), "category", "") or engine.cfg.market.category)
        return taker_fee_for(engine.ex.get_fee_schedule(), cat, estimate)[0]
    except Exception:  # noqa: BLE001
        return estimate


def update_shadow(engine: Any) -> None:
    store, cfg = engine.store, engine.cfg
    days = _day_records(store)
    if not days:
        return
    first_ts = min(int(d["plan"].get("decision_ts_ms") or 0) for d in days) - DAY_MS
    candles = _candles(store, first_ts)
    now_ms = to_ms(engine.now())
    fee = _fee_rate(engine)
    funding = _funding_series(store)
    for g in gate_trades(days, candles, cfg, now_ms, fee, funding):
        store.insert_ignore("shadow_log", kind="gate_trade", variant="gate", unique_key=f"gate:{g['utc_day']}", data=g)
    for v in VARIANTS:
        st = simulate_variant(v, days, candles, cfg, now_ms, fee, funding)
        trades = [asdict(t) for t in st.closed]
        summary = {"variant": v, "closed_trades": len(trades), "total_r": sum(t["r"] or 0 for t in trades),
                   "wins": sum(1 for t in trades if (t["r"] or 0) > 0),
                   "open_trade": asdict(st.trade) if st.trade else None, "trades": trades,
                   "days_simulated": len(days), "fee_rate": fee}
        digest = hashlib.sha256(json.dumps(summary, sort_keys=True, default=str).encode()).hexdigest()
        last = store.latest("shadow_log", "kind='variant_snapshot' AND variant=?", [v])
        if last and last["data"].get("digest") == digest:
            continue
        summary["digest"] = digest
        store.insert("shadow_log", kind="variant_snapshot", variant=v, unique_key=None, data=summary)
