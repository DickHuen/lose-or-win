"""`preview`: what the strategy would decide right now (v1.5.0). Read-only: public Binance data only, no exchange
keys, no orders, nothing written. It does not know the live-only blocks (pause, clock check, region, the
exchange's own mark price), so the real decision can still differ; the analysis says so."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from perpbot.exchange.base import Instrument
from perpbot.risk import trade_leverage
from perpbot.strategy import (
    HOUR_MS,
    ROLLING_PERIOD_MS,
    base_bar_ms,
    day_features,
    live_rolling_features,
    period_key,
    period_start,
    plan_for,
    sc_day,
)
from perpbot.timeutil import DAY_MS, MINUTE_MS, day_start_ms, fmt_hkt, fmt_utc, from_ms, hkt_at, to_ms, utc_day


# BTC-USD limits as read live on 2026-09-30 (preview has no exchange connection): up to 50x, maintenance 0.5 / 50.
BTC_USD_ASSUMED = Instrument(0, "BTC-USD", "BTC", "USD", "crypto", 5, 1, 10.0, 0.0, 0.0, 50, True, 0.02, 0, "1h",
                             [(0.0, 50), (250_000.0, 25), (1_000_000.0, 20)])


def preview_decision(cfg: Any, calendar: Any, now: datetime, bn: Any,
                     open_trade: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    """(decision-like record for analysis.render, next decision time HKT)."""
    cadence = str(cfg.strategy.cadence)
    rolling = cadence in ROLLING_PERIOD_MS
    now_ms = to_ms(now)
    if rolling:
        t = period_start(now_ms, ROLLING_PERIOD_MS[cadence])
        day_d, key = sc_day(t), period_key(t)
    else:
        day_d = utc_day(now)
        t, key = day_start_ms(day_d), day_d.isoformat()
    fstart = day_start_ms(day_d) - int(cfg.binance.funding_days_to_load) * DAY_MS
    funding = [(ts, r) for ts, r, _ in bn.funding(fstart, now_ms)]
    active = calendar.active_windows(now, cfg.gates.event_anchor_hkt, float(cfg.gates.event_post_release_hours))
    events = [{"type": e.type, "release_utc": fmt_utc(e.release_utc), "window_start_utc": fmt_utc(s),
               "window_end_utc": fmt_utc(en), "note": e.note} for e, s, en in active]
    if rolling:
        days = int(cfg.binance.daily_candles_to_load) + int(cfg.strategy.opposite_days_rule) + 2
        h1 = []
        if base_bar_ms(cadence) == HOUR_MS:                     # v1.9.0 rolling_1h
            h1 = bn.klines_range("1h", t - days * DAY_MS, now_ms)
            h4 = bn.klines("4h", int(cfg.binance.h4_candles_to_load), now_ms)
        else:
            h4 = bn.klines_range("4h", t - days * DAY_MS, now_ms)
        f = live_rolling_features(cfg, t, h4, funding, events, cadence=cadence, base=h1)
        nxt = from_ms(t + ROLLING_PERIOD_MS[cadence] + int(cfg.schedule.period_entry_start_minutes) * MINUTE_MS)
    else:
        daily = bn.klines("1d", int(cfg.binance.daily_candles_to_load), now_ms)
        h4 = bn.klines("4h", int(cfg.binance.h4_candles_to_load), now_ms)
        f = day_features(cfg, day_d, daily, h4, funding, events)
        nxt = hkt_at(day_d + timedelta(days=1), cfg.schedule.entry_window_start_hkt)
    pos_dir = int(open_trade["direction"]) if open_trade else 0
    entry_key = (open_trade.get("entry_period") or open_trade.get("entry_utc_day")) if open_trade else key
    entry_day = datetime.fromisoformat(str(open_trade["entry_utc_day"])[:10]).date() \
        if open_trade and open_trade.get("entry_utc_day") else day_d
    plan, ctx = plan_for(f, cfg, position_dir=pos_dir, entry_day=entry_day, entered_today=False, paused_reason=None,
                         entry_key=entry_key)
    try:
        mark = float(bn.price())
    except Exception:  # noqa: BLE001
        mark = f.score.close
    sc = f.score
    plan_d = plan.to_dict()
    plan_d.update({"utc_day": key, "score": sc.score, "direction": sc.direction, "tier_fraction": sc.tier_fraction,
                   "caps": f.caps, "gates_triggered": f.gates_triggered(), "atr": sc.atr, "mark": mark,
                   "position_dir_at_decision": pos_dir, "cadence": f.cadence, "period_utc": fmt_utc(from_ms(t)),
                   "late": False})
    if cfg.risk.notional_multiple_full_tier and not bool(cfg.bold.enabled) and plan.enter_direction and mark:
        mult = float(cfg.risk.notional_multiple_full_tier)          # v1.9.0: the per-trade leverage live would use
        lev, got, note = trade_leverage(
            multiple=mult * float(plan.enter_fraction), sl_pct=float(cfg.exits.sl_atr_multiple) * float(sc.atr) / float(mark),
            max_leverage=int(cfg.risk.leverage), margin_use_pct=float(cfg.risk.max_margin_use_pct),
            liq_multiple=float(cfg.risk.liq_min_sl_multiple), inst=BTC_USD_ASSUMED, notional=0.0,
            mmr_divisor=float(cfg.risk.liq_estimate_mmr_divisor))
        plan_d["sizing"] = {"multiple": got, "leverage": lev, "note": note}
    decision = {
        "utc_day": key, "decision_hkt": fmt_hkt(now), "score": sc.to_dict(),
        "inputs": {"cadence": f.cadence, "position_dir": pos_dir, "mark": mark, "opposite_streak": ctx.opposite_streak},
        "gates": {g.name: {"triggered": g.triggered, "cap": g.cap, "blocks_entry": g.blocks_entry, "detail": g.detail}
                  for g in f.gates},
        "plan": plan_d,
    }
    return decision, fmt_hkt(nxt)[:16]
