"""v2.0.0 live intraday runner: one run every 15 minutes (decide and manage in the same run).

Order of a run (under the bot's file lock):
  1. reconcile (engine): exchange position vs local trade, SL must exist, fills / funding, kill switches
  2. candle cache: only new Binance candles; integrity check (count, gaps, close time)
  3. manage the open position (intraday trades only): TP1 seen -> cancel leg A's leftover stop, stop to break-even
     plus costs, ATR trail, invalidation / time / no-progress exits. A position opened by an older version (no
     `intraday` record) keeps its own exchange SL / TP and is never touched by these rules.
  4. decide: intraday.evaluate on the closed candles; costs from the real order book and the account's fee rate;
     entry only when flat, data complete, not late, not paused, no event blackout.
Every decision (entry or not, with all reasons, score parts and costs) is stored in `decisions`.
Idempotent per 15-minute candle: one decision row per candle key, deterministic client order ids per leg; a re-run
of the same candle never places a second entry.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from perpbot import costs
from perpbot import intraday as idy
from perpbot.candles import CandleCache, boundary, check
from perpbot.exchange.base import (
    ACTIVE_TRIGGER_STATUSES,
    FILLED_STATUSES,
    ExchangeError,
    Order,
    mark_vs_book,
)
from perpbot.records import client_order_id
from perpbot.risk import (
    estimate_liquidation,
    liquidation_ok,
    quantize_price,
    validate_order,
    validate_price,
)
from perpbot.strategy import tier_fraction
from perpbot.timeutil import DAY_MS, HOUR_MS, MINUTE_MS, fmt_hkt, fmt_utc, from_ms, to_ms, utc_day

log = logging.getLogger("perpbot.intraday")

M15 = idy.M15_MS
EXIT_LABEL_ZH = {"TP1": "第一目標", "TP2": "第二目標", "SL": "止損", "BE_stop": "保本止損", "trail_stop": "追蹤止損",
                 "invalidation": "失效離場", "time_stop": "持倉時間到", "no_progress": "冇進展離場"}


def bar_key(t_ms: int) -> str:
    """Decision key of the 15-minute candle closing at t_ms (UTC), e.g. 2026-10-06T03:15/15m."""
    return from_ms(t_ms).strftime("%Y-%m-%dT%H:%M") + "/15m"


def _fmt(d: Decimal) -> str:
    return format(d, "f")


def _hours(interval: str | None) -> float:
    s = str(interval or "1h").strip().lower()
    try:
        if s.endswith("h"):
            return max(float(s[:-1]), 1e-9)
        if s.endswith("m"):
            return max(float(s[:-1]) / 60.0, 1e-9)
    except ValueError:
        pass
    return 1.0


def event_blackout(calendar: Any, now: Any, before_min: float, after_min: float) -> list[str]:
    out = []
    for ev in calendar.events:
        if ev.release_utc - timedelta(minutes=before_min) <= now <= ev.release_utc + timedelta(minutes=after_min):
            out.append(f"{ev.type} {fmt_hkt(ev.release_utc)}")
    return out


class IntradayRunner:
    def __init__(self, engine: Any) -> None:
        self.e = engine
        self.cfg = engine.cfg
        self.ic = engine.cfg.intraday
        self.p = idy.Params.from_cfg(engine.cfg)
        self.cache = CandleCache(engine.store, engine.bn, backfill_days=float(self.ic.cache_backfill_days))

    # ================================================================ data
    def load_data(self, now_ms: int) -> dict[str, Any]:
        upd = self.cache.update(("15m", "1h", "4h"), now_ms)
        p = self.p
        need = {"15m": p.need_15m(), "1h": p.need_1h(), "4h": p.need_4h()}
        span = {"15m": M15, "1h": HOUR_MS, "4h": 4 * HOUR_MS}
        bars = {iv: self.cache.load(iv, now_ms - (need[iv] + 4) * span[iv], now_ms) for iv in need}
        checks = {iv: check(bars[iv], iv, need[iv], now_ms) for iv in need}
        problems = [x for c in checks.values() for x in c.problems] + list(upd["errors"])
        ok = all(c.ok for c in checks.values())
        if not ok:
            self.e.alert("data incomplete", "; ".join(problems)[:600] + " - no new entries; the open position stays "
                         "protected and managed", dedupe_key=f"data_incomplete:{utc_day(from_ms(now_ms))}")
        self.e.store.insert("data_health", kind="intraday_check",
                            data={"ok": ok, "checks": {k: v.to_dict() for k, v in checks.items()}, "update": upd,
                                  "requests": self.cache.requests})
        return {"ok": ok, "problems": problems, "m15": bars["15m"], "h1": bars["1h"], "h4": bars["4h"],
                "fresh_15m": checks["15m"].last_close_ms == checks["15m"].expected_close_ms, "update": upd}

    # ================================================================ run
    def run(self, *, entries: bool) -> dict[str, Any]:
        e = self.e
        now = e.now()
        now_ms = to_ms(now)
        t_ms = boundary(now_ms, M15)
        key = bar_key(t_ms)
        until, status = self.cache.blocked_until()        # a stored Binance pause applies to EVERY request of this run
        if until > now_ms and hasattr(e.bn, "block_until"):
            e.bn.block_until(until, status)
        rr = e.reconcile()
        e.log_market_data()
        data = self.load_data(now_ms)
        out: dict[str, Any] = {"reconcile": rr.actions, "bar": key, "data_ok": data["ok"],
                               "data_problems": data["problems"][:10]}
        inst = e.instrument()
        pos = e.ex.get_account().position(inst.id)
        trade = e.rec.open_trade()
        manage: list[str] = []
        if pos is not None and trade is not None and not trade.get("intraday") and trade.get("adopted"):
            trade = self.upgrade_adopted(trade, now_ms) or trade
        if pos is not None and trade is not None:
            if trade.get("intraday"):
                manage = self.manage(trade, data, now_ms)
            else:
                manage = ["position opened before v2.0.0: kept with its own exchange SL / TP (no intraday exits)"]
        out["manage"] = manage
        out["decision"] = self.decide(t_ms, key, now_ms, data, entries=entries)
        self.log_manage(manage, out)
        return out

    # ================================================================ history (no chasing)
    def history(self, now_ms: int) -> idy.History:
        rows = self.e.store.query("SELECT data FROM trades WHERE event='open' AND ts_ms >= ?", [now_ms - 3 * DAY_MS])
        legs = [r["data"].get("leg_id") for r in rows if isinstance(r["data"], dict) and r["data"].get("leg_id")]
        day0 = now_ms // DAY_MS * DAY_MS
        today = sum(1 for r in rows if isinstance(r["data"], dict) and r["data"].get("intraday")
                    and int(r["data"].get("entry_ts_ms") or 0) >= day0)
        last = self.e.store.query("SELECT data FROM trades WHERE event='close' ORDER BY id DESC LIMIT 1")
        last_exit = int(last[0]["data"].get("exit_ts_ms") or 0) if last and isinstance(last[0]["data"], dict) else None
        return idy.History.from_legs(legs, last_exit or None, today)

    # ================================================================ costs
    def fee_rate(self, inst: Any) -> tuple[float, str]:
        try:
            sched = self.e.ex.get_fee_schedule()
        except ExchangeError as ex:
            log.warning("fee schedule unavailable: %s", ex)
            sched = None
        fills = [f for f in self.e.stored_fills(to_ms(self.e.now()) - 30 * DAY_MS) if f.taker]
        return costs.account_fee_rate(sched, inst.category, fills, float(self.cfg.shadow.fee_rate_estimate),
                                      int(self.ic.fee_min_fills))

    def market(self, inst: Any) -> dict[str, Any]:
        ex = self.e.ex
        book = ex.get_book(inst.id, int(self.ic.cost_book_depth))
        ticker = ex.get_ticker(inst.id)
        ok, dev = mark_vs_book(ticker.mark, book, inst.price_bounds)
        if not ok:
            raise ExchangeError(f"mark {ticker.mark} does not match the {inst.symbol} order book (deviation {dev})")
        hourly = float(ticker.funding_rate) / _hours(inst.funding_interval) if ticker.funding_rate is not None else None
        fee, src = self.fee_rate(inst)
        return {"book": book, "ticker": ticker, "funding_hourly": hourly, "fee_rate": fee, "fee_source": src}

    def cost_estimate(self, mk: dict[str, Any], qty: float, d: int) -> costs.CostEstimate:
        return costs.from_book(book=mk["book"], qty=qty, direction=d, fee_rate=mk["fee_rate"], fee_source=mk["fee_source"],
                               stop_slippage_bps=float(self.ic.stop_slippage_bps),
                               funding_rate_hourly=mk["funding_hourly"], funding_hold_hours=float(self.ic.funding_hold_hours))

    # ================================================================ decide
    def decide(self, t_ms: int, key: str, now_ms: int, data: dict[str, Any], *, entries: bool) -> dict[str, Any]:
        e = self.e
        cfg = self.cfg
        if not entries:                                     # `manage`: never opens, never takes the candle's decision
            return {"key": key, "action": "manage-only", "reasons": ["manage run: never opens"]}
        prior = e.store.latest("decisions", "utc_day = ?", [key])
        if prior is not None:
            resumed = self.resume_entry(key, prior)
            return {"key": key, "action": prior["action"], "note": "decision for this 15-minute candle already made",
                    "resumed": resumed}
        inst = e.instrument()
        acct = e.ex.get_account()
        pos = acct.position(inst.id)
        trade = e.rec.open_trade()
        eqd = e.equity(acct)
        blocks: list[str] = []
        if not entries:
            blocks.append("manage run: never opens")
        delay = (now_ms - t_ms) / MINUTE_MS
        if delay > float(self.ic.entry_max_delay_minutes):
            blocks.append(f"run {delay:.0f} min after the candle closed (> {self.ic.entry_max_delay_minutes}): no late entry")
        if not data["ok"]:
            blocks.append("market data stale or incomplete: " + "; ".join(data["problems"][:3]))
        paused = e.paused_reason()
        if paused:
            blocks.append(f"paused: {paused}")
        if eqd["block_entries"]:
            blocks.append("equity unreadable or sources disagree")
        blocks += e.decision_blocks(utc_day(from_ms(now_ms)))
        ev = event_blackout(e.calendar, e.now(), float(self.ic.event_block_before_minutes),
                            float(self.ic.event_block_after_minutes))
        if ev:
            blocks.append(f"event blackout ({', '.join(ev)})")
        try:
            raw_geo = e.ex.get_geoblock()
            if raw_geo.get("blocked") is True:
                blocks.append(f"region blocked by geoblock ({raw_geo.get('country')}/{raw_geo.get('region')})")
        except ExchangeError as ex:
            log.warning("geoblock read failed: %s", ex)
        pos_dir = pos.direction if pos is not None else 0
        mk: dict[str, Any] | None = None
        replay_inputs: dict[str, Any] | None = None
        dec = idy.Decision(t_ms, "none")
        if data["ok"]:
            try:
                mk = self.market(inst)
            except ExchangeError as ex:
                blocks.append(f"exchange market data unusable: {ex}")
            equity = float(eqd["equity"] or 0.0)
            full = float(cfg.risk.notional_multiple_full_tier or 0.0) or 1.0
            cost_log: dict[int, float] = {}

            def cost_unit(d: int, entry: float) -> float:
                if mk is None:
                    v = entry * 1.0             # no book: the gate refuses (cost >> R)
                else:
                    qty = equity * full / entry if equity > 0 else 0.0
                    v = self.cost_estimate(mk, qty, d).per_unit(entry)
                cost_log[d] = v
                return v

            hist = self.history(now_ms)
            dec = idy.evaluate(self.p, t_ms, data["m15"], data["h1"], data["h4"], cost_unit_fn=cost_unit,
                               history=hist, position_dir=pos_dir)
            replay_inputs = {"now_ms": now_ms, "cost_per_unit_by_dir": cost_log, "position_dir": pos_dir,
                             "history": {"traded_legs": hist.legs_list(), "last_exit_ms": hist.last_exit_ms,
                                         "entries_today": hist.entries_today}}
        else:
            dec.reasons.append("data check failed: no evaluation")
        if pos is not None:
            blocks.append("position open" + ("" if (trade or {}).get("intraday") else " (opened before v2.0.0)"))
        frac = tier_fraction(dec.score, cfg.strategy) if dec.action == "enter" else 0.0
        if dec.action == "enter" and dec.score < float(cfg.strategy.min_entry_abs_score):
            blocks.append(f"score {dec.score:.1f} below the entry threshold {cfg.strategy.min_entry_abs_score}")
        record: dict[str, Any] = {"key": key, "bar_close_utc": fmt_utc(from_ms(t_ms)), "run_hkt": fmt_hkt(e.now()),
                                  "decision": dec.to_dict(), "blocks": blocks, "tier_fraction": frac,
                                  "data_ok": data["ok"], "data_problems": data["problems"][:10],
                                  "replay": replay_inputs}
        if mk is not None:
            record["market"] = {"mark": mk["ticker"].mark, "best_bid": mk["book"].bids[0][0], "best_ask": mk["book"].asks[0][0],
                                "funding_hourly": mk["funding_hourly"], "fee_rate": mk["fee_rate"],
                                "fee_source": mk["fee_source"]}
        action = dec.action if not (dec.action == "enter" and blocks) else "blocked"
        record["action"] = action
        result: dict[str, Any] = {"key": key, "action": action, "reasons": blocks + dec.reasons}
        if action == "enter":
            e.rec.write_intent(key, {"action": "enter", "target_direction": dec.direction, "intraday": True,
                                     "decision": dec.to_dict(), "tier_fraction": frac})
            res = self.enter(key, dec, frac, eqd, mk)
            record["entry"] = res
            result["entry"] = res
            if not res.get("ok"):
                record["action"] = action = "rejected"
                result["action"] = action
        self._store_decision(key, record, dec, frac, eqd)
        return result

    def _store_decision(self, key: str, record: dict[str, Any], dec: idy.Decision, frac: float, eqd: dict[str, Any]) -> None:
        from perpbot import analysis

        e = self.e
        reason = "; ".join(record["blocks"] + dec.reasons + ([str(record.get("entry", {}).get("reason"))]
                                                             if record.get("entry") and not record["entry"].get("ok") else []))
        try:
            record["analysis"] = analysis.render_intraday(record, self.cfg, equity=eqd.get("equity"),
                                                          position=e.rec.open_trade())
        except Exception:  # noqa: BLE001 - the text must never stop a decision
            log.warning("intraday analysis text failed", exc_info=True)
        data = {"intraday": record, "analysis": record.get("analysis"),
                "score": {"score": dec.score, "direction": dec.direction, "tier_fraction": frac,
                          "components": dec.components},
                "plan": {"action": record["action"], "cadence": "intraday_15m", "late": False}}
        e.store.insert("decisions", utc_day=key, score=dec.score, direction=dec.direction, action=record["action"],
                       reason=(reason or record["action"])[:2000], data=data)
        quiet = bool(self.cfg.notifications.analysis_toast_only_actions) and record["action"] not in ("enter", "rejected")
        if bool(self.cfg.notifications.analysis_toast) and not quiet:
            e.notify("分析", (record.get("analysis") or [record["action"]])[0][:200])

    # ================================================================ entry
    def sizing(self, eq: float, frac: float, ref: float, r: float, inst: Any) -> dict[str, Any]:
        return idy.position_size(self.cfg, eq, frac, ref, r, inst)

    def split_legs(self, qty: Decimal, ref: float, inst: Any) -> list[tuple[str, Decimal]]:
        return idy.split_legs(qty, ref, inst, float(self.ic.tp1_fraction))

    def enter(self, key: str, dec: idy.Decision, frac: float, eqd: dict[str, Any], mk: dict[str, Any] | None) -> dict[str, Any]:
        e = self.e
        inst = e.instrument()
        d = int(dec.direction)

        def fail(why: str, **extra: Any) -> dict[str, Any]:
            e.rec.intent_event(key, "entry", "failed", {"reason": why, **extra})
            e.rec.set_state(position_state="flat" if e.ex.get_account().position(inst.id) is None else "open",
                            note=f"entry not made: {why[:80]}")
            return {"ok": False, "reason": why, **extra}

        if mk is None:
            return fail("no exchange market data")
        left = [o for o in e.ex.get_open_orders(inst.id) if o.is_active and (o.is_trigger or o.reduce_only)]
        if left:
            e.cancel_leftovers(inst, left)
            if [o for o in e.ex.get_open_orders(inst.id) if o.is_active and (o.is_trigger or o.reduce_only)]:
                return fail("leftover TP/SL or reduce-only orders could not be cancelled")
        if e.sync_flows()[1]:
            return fail("pending deposit/withdrawal")
        eq = float(eqd["equity"] or 0.0)
        if eq <= 0:
            return fail("equity unreadable")
        book = mk["book"]
        ref = book.best_ask if d > 0 else book.best_bid
        r = float(dec.r or 0.0)
        sz = self.sizing(eq, frac, ref, r, inst)
        qty = sz["qty"]
        if sz["leverage"] < 1 or qty <= 0:
            return fail(f"no position possible: {sz.get('note') or 'quantity rounds to zero'}", sizing=_jsonable(sz))
        cost = self.cost_estimate(mk, float(qty), d)
        # R comes from the decision's Binance prices: the cost in R uses the same price (the Polymarket basis cancels)
        cost_unit = cost.per_unit(float(dec.entry_ref or ref))
        cost_r = cost_unit / r if r > 0 else float("inf")
        if not cost.depth_ok:
            return fail("order book too thin for the size (depth)", cost=cost.to_dict())
        if cost_r > float(self.ic.max_cost_r) + 1e-9:
            return fail(f"costs {cost_r:.2f} R > {self.ic.max_cost_r} R at the real book / size", cost=cost.to_dict())
        lev = int(sz["leverage"])
        notional = float(qty) * ref
        liq_est = estimate_liquidation(ref, d, lev, inst, notional, float(self.cfg.risk.liq_estimate_mmr_divisor))
        if not liquidation_ok(ref, liq_est, r, float(self.cfg.risk.liq_min_sl_multiple)):
            return fail(f"estimated liquidation {liq_est:.1f} closer than {self.cfg.risk.liq_min_sl_multiple} x the stop")
        ok, why = e.ensure_leverage(inst, lev)
        if not ok:
            return fail(f"leverage {lev}x isolated could not be set: {why}")
        stop = quantize_price(ref - d * r, inst.price_decimals, "nearest")
        tp1 = quantize_price(ref + d * float(self.ic.tp1_r) * r, inst.price_decimals, "nearest")
        tp2 = quantize_price(ref + d * float(self.ic.tp2_r) * r, inst.price_decimals, "nearest")
        legs = self.split_legs(qty, ref, inst)
        try:
            for px in (stop, tp1, tp2):
                validate_price(inst, px)
            validate_order(inst, qty=qty, price=ref, leverage=lev, market=False)
        except Exception as ex:  # noqa: BLE001
            return fail(f"order violates instrument rules: {ex}")
        e.rec.intent_event(key, "entry", "started", {"legs": [[n, str(q)] for n, q in legs], "stop": str(stop),
                                                     "tp1": str(tp1), "tp2": str(tp2), "ref": ref})
        e.rec.set_state(position_state="pending_entry", note="intraday entry")
        slip = float(self.cfg.exits.entry_slippage_bps) / 1e4
        side = "BUY" if d > 0 else "SELL"
        filled: dict[str, dict[str, Any]] = {}
        for name, q in legs:
            tp = tp2 if name == "B" else tp1
            bk = book if name == "B" else e.ex.get_book(inst.id, int(self.cfg.polymarket.book_depth))
            px_ref = bk.best_ask if d > 0 else bk.best_bid
            limit = quantize_price(px_ref * (1 + d * slip), inst.price_decimals, "down" if d > 0 else "up")
            coid = client_order_id(key, f"entry:{name}")
            req = {"side": side, "quantity": _fmt(q), "tif": "fok", "price": _fmt(limit), "tp": _fmt(tp), "sl": _fmt(stop),
                   "leg": name, "ref_price": px_ref, "leverage": lev, "decision_key": key}
            e.log_order("entry", "request", coid, None, None, req)
            res = e.ex.place_order(instrument_id=inst.id, side=side, quantity=_fmt(q), tif="fok", price=_fmt(limit),
                                   reduce_only=False, client_order_id=coid, tp_trigger=_fmt(tp), sl_trigger=_fmt(stop))
            e.log_order("entry", "response", coid, res.order_id, "accepted" if res.accepted else "rejected",
                        {"error": res.error, "restriction": res.restriction, "outcome_unknown": res.outcome_unknown,
                         "tp_order_id": res.tp_order_id, "sl_order_id": res.sl_order_id, "leg": name})
            o = e.confirm_order(coid, res, "entry")
            got = o is not None and o.status in FILLED_STATUSES
            if not got:
                pos_now = e.ex.get_account().position(inst.id)
                have = sum(float(v["qty"]) for v in filled.values())
                if pos_now is not None and pos_now.direction == d and abs(pos_now.size) > have + 1e-12:
                    got = True                             # the position shows the fill even if the status did not
                    o = o or Order(id=res.order_id or -1, instrument_id=inst.id, side=side, price=float(limit),
                                   quantity=float(q), tif="fok", reduce_only=False, status="filled",
                                   filled_quantity=float(q), resting_quantity=0.0, client_order_id=coid)
            e.rec.intent_event(key, f"leg_{name}", "filled" if got else "unfilled",
                               {"order_id": res.order_id, "status": o.status if o else None, "error": res.error})
            if not got:
                if res.outcome_unknown:
                    e.alert("entry outcome unknown", f"leg {name} of {key}: {res.error}; the next run reconciles first")
                break
            orders = e.ex.get_open_orders(inst.id)
            oid = o.id if o else res.order_id
            filled[name] = {"qty": float(q), "order_id": oid, "coid": coid, "tp": float(tp), "sl": float(stop),
                            "tp_order_id": res.tp_order_id or next((x.id for x in orders if x.tpsl_kind == "tp"
                                                                    and x.parent_order_id == oid), None),
                            "sl_order_id": res.sl_order_id or next((x.id for x in orders if x.tpsl_kind == "sl"
                                                                    and x.parent_order_id == oid), None)}
        if not filled:
            e.rec.set_state(position_state="flat", note="intraday entry not filled")
            return fail("FOK entry not filled", legs=[n for n, _ in legs])
        return self._record_entry(key, dec, frac, sz, cost, cost_unit, cost_r, filled, stop, tp1, tp2, eq, ref, inst)

    def _record_entry(self, key: str, dec: idy.Decision, frac: float, sz: dict[str, Any], cost: costs.CostEstimate,
                      cost_unit: float, cost_r: float, filled: dict[str, dict[str, Any]], stop: Decimal, tp1: Decimal,
                      tp2: Decimal, eq: float, ref: float, inst: Any) -> dict[str, Any]:
        e = self.e
        d = int(dec.direction)
        e.sync_fills()
        ids = {v["order_id"] for v in filled.values()}
        fills = [f for f in e.stored_fills(to_ms(e.now()) - DAY_MS) if f.order_id in ids]
        pos = e.ex.get_account().position(inst.id)
        qty = abs(pos.size) if pos is not None else sum(v["qty"] for v in filled.values())
        entry_price = pos.entry_price if pos is not None and pos.entry_price else (
            sum(f.price * f.quantity for f in fills) / sum(f.quantity for f in fills) if fills else ref)
        entry_ts = min((f.ts_ms for f in fills), default=to_ms(e.now()))
        two = "A" in filled and "B" in filled
        b = filled["B"]
        r_pm = abs(entry_price - float(stop))
        trade = {
            "trade_uid": uuid.uuid4().hex, "direction": d, "qty": qty, "entry_price": entry_price, "entry_ts_ms": entry_ts,
            "entry_utc_day": key[:10], "entry_period": key, "sl_price": float(stop), "tp_price": float(tp2),
            "sl_order_id": b.get("sl_order_id"), "tp_order_id": b.get("tp_order_id"), "atr": dec.atr1h,
            "sl_distance": r_pm, "initial_risk_usd": r_pm * qty, "equity_at_entry": eq, "score": dec.score,
            "components": dec.components, "tier_fraction": frac, "effective_fraction": frac, "caps": [],
            "gates_triggered": [], "entry_fees": sum(f.fee for f in fills),
            "maker_taker": ["taker" if f.taker else "maker" for f in fills], "entry_order_id": b.get("order_id"),
            "entry_coid": b.get("coid"), "decision_mark": ref, "decision_ts_ms": dec.t_ms,
            "liquidation_price": pos.liquidation_price if pos else None, "adopted": False, "external": False,
            "live": True, "plan_action": "enter",
            # v2.0.0 intraday
            "intraday": True, "setup": dec.setup, "leg_id": dec.leg_id, "bn_entry": dec.entry_ref,
            "bn_offset": entry_price - float(dec.entry_ref or entry_price), "r_bn": dec.r, "invalidation_bn": dec.invalidation,
            "tp1_price": float(tp1) if two else None, "tp2_price": float(tp2), "stage": "initial", "two_legs": two,
            "legs": filled, "tp1_done": False, "position_sl_id": None, "position_sl_ids": {},
            "cost": cost.to_dict(), "cost_unit": cost_unit, "cost_r": cost_r, "atr15": dec.atr15,
            "trade_leverage": sz["leverage"], "multiple": sz["multiple"], "position_note": sz.get("note"),
            "room": dec.room, "obstacle": dec.obstacle,
            "slippage_bps_vs_decision_mark": ((entry_price - ref) / ref * 1e4 * d) if ref else None,
        }
        e.rec.record_trade("open", trade["trade_uid"], d, trade)
        e.rec.intent_event(key, "entry", "done", {"trade_uid": trade["trade_uid"], "legs": list(filled)})
        e.rec.set_state(position_state="open", note="intraday entry filled")
        e.log_order("entry", "fills", b.get("coid"), b.get("order_id"), "filled",
                    {"fills": [f.__dict__ for f in fills], "entry_price": entry_price, "qty": qty, "legs": filled})
        name = "LONG" if d > 0 else "SHORT"
        e.alert("open", f"{name} {qty} {inst.symbol} @ {entry_price:.1f} | {dec.setup} | SL {stop} | "
                f"TP1 {tp1 if two else '-'} TP2 {tp2} | risk {trade['initial_risk_usd']:.2f} "
                f"({trade['initial_risk_usd'] / eq * 100:.1f}% equity), position x{sz['multiple']:.2g} at {sz['leverage']}x"
                f"{' (' + sz['note'] + ')' if sz.get('note') else ''} | costs {cost_r:.2f} R | score {dec.score:.0f}")
        liq_mult = float(self.cfg.risk.liq_after_fill_sl_multiple or self.cfg.risk.liq_min_sl_multiple)
        if pos is not None and not liquidation_ok(entry_price, pos.liquidation_price, r_pm, liq_mult, isolated=not pos.cross):
            e.alert("liquidation check", f"liquidation price {pos.liquidation_price!r} closer than {liq_mult:.2f} x "
                    f"the stop distance {r_pm:.1f}; closing")
            e.close_position(reason="liq_check")
            return {"ok": True, "closed_by": "liq_check", "trade_uid": trade["trade_uid"]}
        if pos is not None:
            e.ensure_protection(inst, e.rec.open_trade() or trade, pos, e.ex.get_open_orders(inst.id))
        return {"ok": True, "trade_uid": trade["trade_uid"], "legs": list(filled), "entry_price": entry_price, "qty": qty,
                "cost_r": cost_r, "leverage": sz["leverage"], "multiple": sz["multiple"]}

    def resume_entry(self, key: str, prior: dict[str, Any]) -> str | None:
        """A re-run of a candle whose entry was interrupted: never a second order; the fills are adopted by reconcile."""
        if prior["action"] != "enter":
            return None
        ev = self.e.rec.intent_events(key)
        done = {(x["step"], x["status"]) for x in ev}
        if ("entry", "done") in done or ("entry", "failed") in done:
            return None
        self.e.rec.intent_event(key, "entry", "failed", {"reason": "interrupted run: not retried (reconcile adopts any fill)"})
        return "interrupted entry closed without a new order"

    def upgrade_adopted(self, trade: dict[str, Any], now_ms: int) -> dict[str, Any] | None:
        """An entry interrupted after a fill was adopted by reconcile as an unknown position: when the intent of a
        recent candle shows our own intraday entry in that direction, restore its intraday record (legs, stop,
        targets) so the exits manage it."""
        e = self.e
        rows = e.store.query("SELECT utc_day, data FROM intents WHERE ts_ms >= ? ORDER BY id DESC", [now_ms - 2 * HOUR_MS])
        for r in rows:
            data = r["data"] if isinstance(r["data"], dict) else {}
            if not data.get("intraday") or int(data.get("target_direction") or 0) != int(trade["direction"]):
                continue
            key, dec = r["utc_day"], data.get("decision") or {}
            legs: dict[str, dict[str, Any]] = {}
            orders = e.ex.get_open_orders(e.instrument().id)
            for name in ("B", "A"):
                coid = client_order_id(key, f"entry:{name}")
                try:
                    found = [o for o in e.orders_by_coid(coid) if o.status in FILLED_STATUSES]
                except ExchangeError:
                    found = []
                if not found:
                    continue
                o = found[0]
                tp = next((x for x in orders if x.tpsl_kind == "tp" and x.parent_order_id == o.id), None)
                sl = next((x for x in orders if x.tpsl_kind == "sl" and x.parent_order_id == o.id), None)
                legs[name] = {"qty": float(o.filled_quantity or o.quantity), "order_id": o.id, "coid": coid,
                              "tp": tp.trigger_price if tp else None, "sl": sl.trigger_price if sl else trade.get("sl_price"),
                              "tp_order_id": tp.id if tp else None, "sl_order_id": sl.id if sl else None}
            if "B" not in legs:
                return None
            r_bn = float(dec.get("r") or 0.0)
            upd = {"intraday": True, "setup": dec.get("setup"), "leg_id": dec.get("leg_id"), "bn_entry": dec.get("entry_ref"),
                   "bn_offset": float(trade["entry_price"]) - float(dec.get("entry_ref") or trade["entry_price"]),
                   "r_bn": r_bn or None, "invalidation_bn": dec.get("invalidation"), "stage": "initial",
                   "two_legs": "A" in legs, "legs": legs, "tp1_done": False, "position_sl_id": None,
                   "position_sl_ids": {}, "cost_unit": dec.get("cost_per_unit"), "score": dec.get("score"),
                   "tp1_price": legs.get("A", {}).get("tp"), "tp2_price": legs["B"].get("tp"),
                   "sl_order_id": legs["B"].get("sl_order_id"), "tp_order_id": legs["B"].get("tp_order_id"),
                   "entry_period": key, "note": "interrupted intraday entry recovered"}
            e.rec.record_trade("update", trade["trade_uid"], int(trade["direction"]), upd)
            e.rec.intent_event(key, "entry", "done", {"recovered": True, "legs": list(legs)})
            e.alert("entry recovered", f"intraday entry of {key} recovered after an interrupted run (legs {list(legs)})")
            return e.rec.open_trade()
        return None

    # ================================================================ manage
    def _status(self, oid: Any) -> str | None:
        if not oid:
            return None
        try:
            found = self.e.ex.get_orders(order_id=int(oid))
        except ExchangeError:
            return None
        return found[0].status if found else None

    def bars_since(self, data: dict[str, Any], entry_ts: int) -> list[Any]:
        start = boundary(entry_ts, M15)
        return [c for c in data["m15"] if c.open_ms >= start]

    def manage(self, trade: dict[str, Any], data: dict[str, Any], now_ms: int) -> list[str]:
        e = self.e
        p = self.p
        inst = e.instrument()
        acts: list[str] = []
        d = int(trade["direction"])
        uid = trade["trade_uid"]
        orders = e.ex.get_open_orders(inst.id)
        active = {o.id for o in orders if o.status in ACTIVE_TRIGGER_STATUSES}
        pos = e.ex.get_account().position(inst.id)
        if pos is None:
            return ["flat"]
        # --- TP1 (leg A) seen
        if trade.get("two_legs") and not trade.get("tp1_done"):
            a, b = trade["legs"]["A"], trade["legs"]["B"]
            st = self._status(a.get("tp_order_id"))
            reduced = abs(pos.size) <= float(b["qty"]) + 1e-9
            tp1_fill = reduced and any(e._intraday_label(trade, f) == "TP1" for f in e.exit_fill_candidates(trade))
            if st in ("triggered", "filled") or tp1_fill:
                upd: dict[str, Any] = {"tp1_done": True, "tp1_seen_ms": now_ms, "note": "TP1 (leg A) filled"}
                sl_a = a.get("sl_order_id")
                if sl_a and int(sl_a) in active:
                    res = e.ex.cancel_orders([int(sl_a)])
                    e.log_order("tp1_cleanup", "cancel", None, int(sl_a), None, {"results": [x.__dict__ for x in res]})
                    acts.append(f"cancelled leg A's stop {sl_a} after TP1")
                e.rec.record_trade("update", uid, d, upd)
                trade.update(upd)
                e.alert("partial", f"TP1 hit: {a['qty']} closed near {a['tp']}; runner {b['qty']} keeps TP2 {b['tp']}")
                acts.append("TP1 done")
        # --- bars since entry
        bars = self.bars_since(data, int(trade["entry_ts_ms"]))
        entry_bn = float(trade.get("bn_entry") or trade["entry_price"])
        off = float(trade.get("bn_offset") or 0.0)
        best = max([c.high for c in bars], default=entry_bn) if d > 0 else min([c.low for c in bars], default=entry_bn)
        worst = min([c.low for c in bars], default=entry_bn) if d > 0 else max([c.high for c in bars], default=entry_bn)
        state = idy.ManageState(direction=d, entry=entry_bn, r=float(trade.get("r_bn") or trade["sl_distance"]),
                                entry_ms=int(trade["entry_ts_ms"]), invalidation=trade.get("invalidation_bn"),
                                stage=str(trade.get("stage") or "initial"), stop=float(trade["sl_price"]) - off,
                                best=best, worst=worst, two_legs=bool(trade.get("two_legs")),
                                cost_unit=float(trade.get("cost_unit") or 0.0))
        inv_bars = data["h1"] if p.invalidation_timeframe == "1h" else data["m15"]
        last = idy.invalidation_bar(inv_bars, int(trade["entry_ts_ms"]), p) if data["fresh_15m"] else None
        reason, why = idy.exit_signal(state, last, now_ms, p, fresh=bool(data["ok"] and data["fresh_15m"]))
        if reason:
            e.alert(reason.replace("_", " "), f"{EXIT_LABEL_ZH.get(reason, reason)}: {why}; closing reduce-only")
            closed, clean = e.close_position(reason=reason)
            return acts + [f"{reason}: {'closed' if closed else 'CLOSE FAILED'}{'' if clean else ' (leftovers)'}"]
        if not data["ok"]:
            return acts + ["data incomplete: break-even / trail not moved this run (exchange stops stay)"]
        atr1h = idy.last_atr(data["h1"], p.atr_period)
        atr15 = idy.last_atr(data["m15"], p.atr_period)
        if not atr1h or not atr15:
            return acts + ["ATR unavailable: stop not moved"]
        stage, new_stop = idy.next_stop(state, atr1h, atr15, p, bool(trade.get("tp1_done")))
        if stage != state.stage and new_stop is None:
            e.rec.record_trade("update", uid, d, {"stage": stage, "note": "stage"})
        if new_stop is None:
            return acts
        new_pm = new_stop + off
        label = "BE_stop" if state.stage == "initial" else "trail_stop"
        mark = e.ex.get_ticker(inst.id).mark
        if (mark - new_pm) * d <= 0:
            e.alert(label.replace("_", " "), f"price {mark} already beyond the new stop {new_pm:.1f}: closing")
            closed, _ = e.close_position(reason=label)
            return acts + [f"{label}: price beyond the new stop -> {'closed' if closed else 'CLOSE FAILED'}"]
        acts.append(self.move_stop(trade, new_pm, label, stage))
        return acts

    def move_stop(self, trade: dict[str, Any], new_pm: float, label: str, stage: str) -> str:
        """Replace the position-scope stop (break-even / trail). The runner leg's own bracket stop at the original
        level stays as the backstop the whole time, so the position is never without a stop."""
        e = self.e
        inst = e.instrument()
        d = int(trade["direction"])
        uid = trade["trade_uid"]
        price = quantize_price(new_pm, inst.price_decimals, "down" if d > 0 else "up")
        old = trade.get("position_sl_id")
        active = {o.id for o in e.ex.get_open_orders(inst.id) if o.status in ACTIVE_TRIGGER_STATUSES}
        if old and int(old) in active:
            res = e.ex.cancel_orders([int(old)])
            e.log_order("stop_move", "cancel", None, int(old), None, {"results": [x.__dict__ for x in res]})
        e.log_order("stop_move", "request", None, None, None, {"sl": _fmt(price), "label": label})
        res = e.ex.place_position_tpsl(instrument_id=inst.id, tp_trigger=None, sl_trigger=_fmt(price))
        e.log_order("stop_move", "response", None, res.sl_order_id, "accepted" if res.accepted else "rejected",
                    {"error": res.error, "label": label})
        if res.accepted and res.sl_order_id:
            ids = dict(trade.get("position_sl_ids") or {})
            ids[str(res.sl_order_id)] = label
            upd = {"sl_price": float(price), "position_sl_id": res.sl_order_id, "position_sl_ids": ids, "stage": stage,
                   "note": f"{label} moved to {price}"}
            e.rec.record_trade("update", uid, d, upd)
            trade.update(upd)
            e.alert("stop moved", f"{'break-even' if label == 'BE_stop' else 'trailing'} stop now {price}")
            return f"{label} -> {price}"
        e.alert("stop move failed", f"{label} at {price} rejected ({res.error}); the original bracket stop "
                f"{trade.get('legs', {}).get('B', {}).get('sl')} still protects; retried next run",
                dedupe_key=f"stop_move_fail:{uid}:{price}")
        if old:
            e.rec.record_trade("update", uid, d, {"position_sl_id": None, "note": "position stop cancelled, new one failed"})
            trade["position_sl_id"] = None
        return f"{label} move FAILED ({res.error})"

    # ================================================================ log
    def log_manage(self, actions: list[str], out: dict[str, Any]) -> None:
        e = self.e
        inst = e.instrument()
        acct = e.ex.get_account()
        pos = acct.position(inst.id)
        orders = e.ex.get_open_orders(inst.id)
        mark = e.ex.get_ticker(inst.id).mark
        sls = [o.trigger_price for o in orders if o.tpsl_kind == "sl" and o.status in ACTIVE_TRIGGER_STATUSES]
        tps = [o.trigger_price for o in orders if o.tpsl_kind == "tp" and o.status in ACTIVE_TRIGGER_STATUSES]
        sl = max(sls) if (sls and pos is not None and pos.direction > 0) else (min(sls) if sls else None)
        tp = min(tps) if (tps and pos is not None and pos.direction > 0) else (max(tps) if tps else None)
        data = {"state": e.rec.display_state(), "mark": mark, "position": pos.__dict__ if pos else None,
                "unrealized_pnl": pos.unrealized_pnl if pos else 0.0, "sl": sl, "tp": tp, "sl_all": sls, "tp_all": tps,
                "dist_to_sl": (abs(mark - sl) if (pos and sl) else None),
                "dist_to_tp": (abs(tp - mark) if (pos and tp) else None),
                "liquidation_price": pos.liquidation_price if pos else None, "actions": actions,
                "intraday": {"bar": out.get("bar"), "decision": out.get("decision", {}).get("action"),
                             "data_ok": out.get("data_ok")}}
        e.store.insert("manage_log", state=data["state"], data=data)


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}
