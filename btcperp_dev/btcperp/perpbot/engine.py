"""Trading engine: reconcile, decide, manage, kill/pause/resume, protection and execution.

Every trading run starts with `reconcile()`:
  1. exchange position vs local trade (book intraday TP/SL closes, adopt unknown positions)
  2. SL must exist on the exchange; re-place it; if that fails, close reduce-only
  3. fills / funding payments synced into the append-only store; cumulative_funding recorded
  4. Telegram /pause /kill /status from the configured chat
  5. equity log + kill switches
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from perpbot.calendar_events import EventCalendar
from perpbot.exchange.base import (
    ACTIVE_ORDER_STATUSES,
    ACTIVE_TRIGGER_STATUSES,
    FILLED_STATUSES,
    NOT_FILLED_TERMINAL,
    AccountSnapshot,
    Exchange,
    ExchangeError,
    Fill,
    Instrument,
    Order,
    PlaceResult,
    Position,
)
from perpbot.indicators import Candle
from perpbot.records import Records, client_order_id
from perpbot.risk import (
    DrawdownState,
    compute_size,
    drawdown,
    estimate_liquidation,
    liquidation_ok,
    losing_streak,
    quantize_price,
    quantize_qty,
    risk_pct_for_trade,
    size_weighted_expectancy,
    validate_order,
    validate_price,
)
from perpbot.storage import Store
from perpbot.strategy import (
    DecisionContext,
    InsufficientData,
    bracket_prices,
    compute_score,
    decide_plan,
    direction_history,
    event_gate,
    funding_gate,
    funding_percentile,
    h4_emas,
    h4_gate,
    opposite_streak,
    regime_gate,
)
from perpbot.telegram import Telegram, parse_command
from perpbot.timeutil import (
    DAY_MS,
    HOUR_MS,
    Clock,
    day_start_ms,
    entry_window,
    fmt_hkt,
    fmt_utc,
    from_ms,
    to_ms,
    utc_day,
)

log = logging.getLogger("perpbot.engine")

EXIT_REASON = {
    "flip": "flip", "three_day_rule": "3day_rule", "funding_rule": "funding_rule",
    "kill_switch": "kill_switch", "manual_kill": "manual", "protection_failure": "protection_failure",
    "liq_check": "liq_check",
}


class EngineError(Exception):
    pass


@dataclass
class ReconcileResult:
    instrument: Instrument
    account: AccountSnapshot
    position: Position | None
    open_orders: list[Order]
    trade: dict[str, Any] | None
    equity: dict[str, Any]
    actions: list[str] = field(default_factory=list)


def _fmt_dec(d: Decimal) -> str:
    return format(d, "f")


class Engine:
    def __init__(self, *, cfg: Any, calendar: EventCalendar, store: Store, exchange: Exchange, binance: Any,
                 telegram: Telegram, clock: Clock, secrets: Any, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.calendar = calendar
        self.store = store
        self.rec = Records(store)
        self.ex = exchange
        self.bn = binance
        self.tg = telegram
        self.clock = clock
        self.secrets = secrets
        self.sleep = sleep
        self._inst: Instrument | None = None
        self._kill_close_tried = False

    # ================================================================ utilities
    def now(self) -> datetime:
        return self.clock.now()

    def today(self) -> str:
        return utc_day(self.now()).isoformat()

    def alert(self, kind: str, text: str, dedupe_key: str | None = None) -> None:
        """Store an alert for the owner. `python3 run.py alerts` prints undelivered alerts for Grok Bot
        to forward; Telegram is used only if enabled in config."""
        if dedupe_key and self.rec.alert_sent(dedupe_key):
            return
        msg = f"[btcperp] {kind}: {text}\n({fmt_hkt(self.now())})"
        sent = self.tg.send(msg)
        self.store.insert("alerts", kind=kind, dedupe_key=dedupe_key, sent=int(sent), text=msg[:4000])
        log.warning("ALERT %s: %s", kind, text)

    def instrument(self) -> Instrument:
        if self._inst is not None:
            return self._inst
        insts = self.ex.get_instruments()
        m = self.cfg.market
        by_sym = {i.symbol: i for i in insts}
        chosen = next((by_sym[s] for s in m.symbol_candidates if s in by_sym), None)
        if chosen is None:
            cands = [i for i in insts if i.base_asset == m.base_asset and i.quote_asset == m.quote_asset
                     and i.category == m.category]
            if len(cands) != 1:
                raise EngineError(f"cannot resolve BTC instrument from /v1/info/instruments "
                                  f"(symbols: {sorted(by_sym)[:20]})")
            chosen = cands[0]
        if chosen.max_leverage < self.cfg.risk.leverage:
            raise EngineError(f"instrument max_leverage {chosen.max_leverage} < configured {self.cfg.risk.leverage}")
        if chosen.isolated_only and self.cfg.risk.cross_margin:
            raise EngineError("instrument is isolated-only but config asks for cross margin")
        self._inst = chosen
        return chosen

    def coid(self, action: str, day: str | None = None) -> str:
        return client_order_id(day or self.today(), action)

    def log_order(self, purpose: str, event: str, coid: str | None, order_id: int | None, status: str | None,
                  data: dict[str, Any]) -> None:
        self.store.insert("orders", purpose=purpose, event=event, client_order_id=coid, order_id=order_id,
                          status=status, data=data)

    def paused_reason(self) -> str | None:
        st = self.rec.state()
        return ", ".join(st["pause_reasons"]) if st["paused"] else None

    # ================================================================ equity
    def equity(self, acct: AccountSnapshot) -> dict[str, Any]:
        """Equity from ONE fixed source (config) for both the peak and the current value (review C8).
        The other measure is a cross-check: disagreement blocks new entries and alerts; an unreadable
        value marks the equity invalid (kill-switch evaluation is skipped for that run)."""
        hint = self.cfg.polymarket.collateral_asset_hint
        wallet = sum(b.value for b in acct.balances if not hint or b.asset == hint)
        upnl = sum(p.unrealized_pnl for p in acct.positions)
        mtm = wallet + upnl
        tav = acct.total_account_value
        src = self.cfg.risk.equity_source
        used = tav if src == "total_account_value" else mtm
        valid = used is not None and used == used and used > 0
        disagree = False
        if valid and tav and tav > 0:
            disagree = abs(mtm - tav) / tav * 100.0 > float(self.cfg.risk.equity_crosscheck_tolerance_pct)
        if not valid:
            self.alert("equity unreadable", f"{src} = {used!r}; kill switches skipped this run, new entries blocked",
                       dedupe_key=f"equity_invalid:{self.today()}")
        elif disagree:
            self.alert("equity cross-check", f"wallet+uPnL={mtm:.2f} vs total_account_value={tav:.2f} "
                       f"(> {self.cfg.risk.equity_crosscheck_tolerance_pct}%); using {src}; new entries blocked",
                       dedupe_key=f"equity_xcheck:{self.today()}")
        return {"equity": used, "wallet": wallet, "upnl": upnl, "mtm_wallet_plus_upnl": mtm,
                "total_account_value": tav, "source": src, "withdrawable": acct.withdrawable,
                "valid": valid, "sources_disagree": disagree, "block_entries": (not valid) or disagree}

    # ================================================================ sync
    def sync_fills(self) -> int:
        last = self.store.latest("fills")
        lookback = int(float(self.cfg.polymarket.fills_lookback_hours) * HOUR_MS)
        start = (int(last["fill_ts_ms"]) - lookback) if last else to_ms(self.now()) - 30 * DAY_MS
        n = 0
        for f in self.ex.get_fills(start):
            if self.store.insert_ignore("fills", trade_id=f.trade_id, order_id=f.order_id, client_order_id=f.client_order_id,
                                        side=f.side, price=f.price, quantity=f.quantity, fee=f.fee, taker=int(f.taker),
                                        pnl=f.pnl, liquidation=int(f.liquidation), previous_size=f.previous_size,
                                        fill_ts_ms=f.ts_ms, data=f.__dict__):
                n += 1
        return n

    def sync_funding(self, inst: Instrument) -> int:
        last = self.store.latest("funding_payments")
        start = (int(last["pay_ts_ms"]) - 6 * HOUR_MS) if last else to_ms(self.now()) - 30 * DAY_MS
        n = 0
        for p in self.ex.get_funding_payments(inst.id, start):
            if self.store.insert_ignore("funding_payments", payment_id=p.id, pay_ts_ms=p.ts_ms, funding=p.funding,
                                        size=p.size, rate=p.funding_rate):
                n += 1
        return n

    def sync_flows(self) -> tuple[float, list[Any], list[Any]]:
        """(net newly confirmed deposits(+)/withdrawals(-), pending flows, newly seen flows)."""
        adj = 0.0
        try:
            flows = self.ex.get_flows(to_ms(self.now()) - 90 * DAY_MS)
        except ExchangeError as e:
            log.warning("flows read failed: %s", e)
            return 0.0, [], []
        pending, new_seen = [], []
        for f in flows:
            new = self.store.insert_ignore("flows", flow_key=f"{f.key}:{f.status}", kind=f.kind, amount=f.amount,
                                           status=f.status, flow_ts_ms=f.ts_ms)
            if f.status == "pending":
                pending.append(f)
            if new:
                new_seen.append(f)
                if f.status == "confirmed":
                    adj += f.amount if f.kind == "deposit" else -f.amount
        return adj, pending, new_seen

    def stored_fills(self, since_ms: int) -> list[Fill]:
        rows = self.store.query("SELECT data FROM fills WHERE fill_ts_ms >= ? ORDER BY fill_ts_ms, trade_id", [since_ms])
        out = []
        inst_id = self.instrument().id
        for r in rows:
            d = r["data"]
            if int(d.get("instrument_id", inst_id)) != inst_id:
                continue
            out.append(Fill(**{k: d[k] for k in Fill.__dataclass_fields__}))
        return out

    def smoketest_coids(self) -> set[str]:
        return {r["client_order_id"] for r in self.store.query(
            "SELECT DISTINCT client_order_id FROM orders WHERE purpose='smoketest' AND client_order_id IS NOT NULL")}

    def entered_today(self, day: str) -> bool:
        start = day_start_ms(datetime.fromisoformat(day).date())
        smoke = self.smoketest_coids()
        if any(f.is_opening and f.client_order_id not in smoke for f in self.stored_fills(start)):
            return True
        for r in self.store.query("SELECT data FROM trades WHERE event='open' AND ts_ms >= ?", [start]):
            if r["data"].get("entry_utc_day") == day and r["data"].get("live", True):
                return True
        return any(e["step"] == "entry" and e["status"] == "done" for e in self.rec.intent_events(day))

    # ================================================================ reconcile
    def reconcile(self, *, allow_actions: bool = True) -> ReconcileResult:
        inst = self.instrument()
        actions: list[str] = []
        prev_state = self.rec.state()
        acct = self.ex.get_account()
        pos = acct.position(inst.id)
        orders = self.ex.get_open_orders(inst.id)
        self.sync_fills()
        try:
            self.sync_funding(inst)
        except ExchangeError as e:
            log.warning("funding payments read failed: %s", e)
        self.repair_incomplete_closes()
        trade = self.rec.open_trade()
        if prev_state["position_state"] in ("pending_entry", "pending_exit"):
            actions.append(f"resolving interrupted state {prev_state['position_state']}")

        if trade and pos is None:
            flat, why = self.confirm_flat(inst, trade)
            if flat:
                self.book_closed_trade(trade, forced_reason=None)
                actions.append(f"booked intraday close ({why})")
                trade = None
            else:
                pos = self.ex.get_account().position(inst.id)
                orders = self.ex.get_open_orders(inst.id)
                actions.append(f"position missing on one read only ({why}); nothing booked or cancelled")
        if trade and pos is not None and pos.direction != trade["direction"]:
            self.book_closed_trade(trade, forced_reason="external")
            actions.append("local trade direction differs from exchange: booked as external close")
            trade = None
        if pos is not None and trade is None:
            trade = self.adopt_position(inst, pos, orders)
            actions.append("adopted exchange position")

        if pos is not None and trade is not None:
            self.store.insert("position_snapshots", size=pos.size, entry_price=pos.entry_price, mark=None,
                              upnl=pos.unrealized_pnl, cumulative_funding=pos.cumulative_funding,
                              liquidation_price=pos.liquidation_price, data={"trade_uid": trade["trade_uid"]})
            if allow_actions:
                actions += self.ensure_protection(inst, trade, pos, orders)
        elif pos is None and any(o.is_active and (o.is_trigger or o.reduce_only) for o in orders):
            flat, why = self.confirm_flat(inst, None)
            if flat:
                actions += self.cancel_leftovers(inst, orders)
            else:
                actions.append(f"leftover orders kept: flat not confirmed ({why})")

        acct = self.ex.get_account()
        pos = acct.position(inst.id)
        self.rec.set_state(position_state="open" if pos else "flat")
        orders = self.ex.get_open_orders(inst.id)
        trade = self.rec.open_trade()

        if allow_actions:
            actions += self.process_telegram()
            acct = self.ex.get_account()
            pos = acct.position(inst.id)
        eq = self.kill_switch_check(acct, allow_actions=allow_actions)
        if allow_actions:
            acct = self.ex.get_account()
            pos = acct.position(inst.id)
            orders = self.ex.get_open_orders(inst.id)
            trade = self.rec.open_trade()
        self.check_key_expiry()
        return ReconcileResult(inst, acct, pos, orders, trade, eq, actions)

    def confirm_flat(self, inst: Instrument, trade: dict[str, Any] | None) -> tuple[bool, str]:
        """Evidence that the position is really gone (review C5): exit fills covering the trade,
        a trigger that fired / was closed with the position, or two reads apart that both show flat."""
        if trade is not None:
            covered = sum(min(f.quantity, abs(f.previous_size)) for f in self.exit_fill_candidates(trade))
            if covered >= float(trade["qty"]) - 1e-9:
                return True, "exit fills cover the position"
            for oid in (trade.get("sl_order_id"), trade.get("tp_order_id")):
                if not oid:
                    continue
                try:
                    for o in self.ex.get_orders(order_id=int(oid)):
                        if o.status in ("triggered", "filled", "position_closed"):
                            return True, f"trigger {oid} {o.status}"
                except ExchangeError:
                    pass
        self.sleep(float(self.cfg.polymarket.flat_confirm_delay_seconds))
        if self.ex.get_account().position(inst.id) is None:
            return True, "flat on two reads"
        return False, "position visible again on the second read"

    def _opening_fill_for(self, pos: Position) -> Fill | None:
        """Most recent fill that opened a position from flat (the start of the current position)."""
        fills = [f for f in self.stored_fills(to_ms(self.now()) - 30 * DAY_MS) if f.is_opening]
        return fills[-1] if fills else None

    def adopt_position(self, inst: Instrument, pos: Position, orders: list[Order]) -> dict[str, Any]:
        day = self.today()
        intent = self.rec.intent(day) or {}
        entry_order = None
        for a in range(1, int(self.cfg.exits.entry_attempts) + 1):
            try:
                found = [o for o in self.ex.get_orders(client_order_id=self.coid(f"entry:{a}", day)) if not o.is_trigger]
            except ExchangeError:
                found = []
            if found and found[0].status in FILLED_STATUSES:
                entry_order = found[0]
        sl = next((o.trigger_price for o in orders if o.tpsl_kind == "sl" and o.status in ACTIVE_TRIGGER_STATUSES), None)
        tp = next((o.trigger_price for o in orders if o.tpsl_kind == "tp" and o.status in ACTIVE_TRIGGER_STATUSES), None)
        atr = intent.get("atr") or self._latest_atr()
        sl_mult, tp_mult = float(self.cfg.exits.sl_atr_multiple), float(self.cfg.exits.tp_atr_multiple)
        if sl is None and atr:
            sl = pos.entry_price - pos.direction * sl_mult * atr
        if tp is None and atr:
            tp = pos.entry_price + pos.direction * tp_mult * atr
        external = entry_order is None or intent.get("enter_direction") != pos.direction
        fills = [f for f in self.stored_fills(to_ms(self.now()) - 2 * DAY_MS) if entry_order and f.order_id == entry_order.id]
        opening = fills[0] if fills else self._opening_fill_for(pos)
        entry_ts = opening.ts_ms if opening else to_ms(self.now())
        entry_day = utc_day(from_ms(entry_ts)).isoformat()          # review D12: from the opening fill
        entry_fees = sum(f.fee for f in fills) if fills else (opening.fee if opening else 0.0)
        sl_dist = abs(pos.entry_price - sl) if sl else (sl_mult * atr if atr else 0.0)
        acct = self.ex.get_account()
        eq = self.equity(acct)["equity"]
        trade = {
            "trade_uid": uuid.uuid4().hex, "direction": pos.direction, "qty": abs(pos.size),
            "entry_price": pos.entry_price, "entry_ts_ms": entry_ts,
            "entry_utc_day": entry_day, "sl_price": sl, "tp_price": tp,
            "sl_order_id": next((o.id for o in orders if o.tpsl_kind == "sl" and o.status in ACTIVE_TRIGGER_STATUSES), None),
            "tp_order_id": next((o.id for o in orders if o.tpsl_kind == "tp" and o.status in ACTIVE_TRIGGER_STATUSES), None),
            "atr": atr, "sl_distance": sl_dist, "initial_risk_usd": sl_dist * abs(pos.size),
            "equity_at_entry": eq, "entry_fees": entry_fees, "adopted": True, "external": external, "live": True,
            "score": intent.get("score"), "tier_fraction": intent.get("tier_fraction"),
            "effective_fraction": intent.get("enter_fraction"), "caps": intent.get("caps"),
            "gates_triggered": intent.get("gates_triggered"), "entry_order_id": entry_order.id if entry_order else None,
            "decision_mark": intent.get("mark"), "decision_ts_ms": intent.get("decision_ts_ms"),
        }
        self.rec.record_trade("open", trade["trade_uid"], pos.direction, trade)
        if entry_order is not None:
            self.rec.intent_event(day, "entry", "done", {"recovered": True, "order_id": entry_order.id})
        self.alert("position adopted" if external else "entry recovered",
                   f"{'Unknown' if external else 'Interrupted-entry'} position on exchange: size {pos.size} @ {pos.entry_price}; "
                   f"SL {sl} TP {tp}")
        budget = eq * float(self.cfg.risk.risk_per_trade_pct) / 100.0 if eq else 0.0
        if budget and trade["initial_risk_usd"] > budget * 1.01:
            self.rec.set_state(add_reason="adopted_over_budget", note="adopted position risk above budget")
            self.alert("adopted position over risk budget",
                       f"risk at SL {trade['initial_risk_usd']:.2f} > budget {budget:.2f} "
                       f"({self.cfg.risk.risk_per_trade_pct}% of equity). Bot paused; SL/TP kept. Resume only after you confirm.")
        return trade

    def _latest_atr(self) -> float | None:
        row = self.store.latest("decisions", "score IS NOT NULL")
        if row and isinstance(row["data"], dict) and (row["data"].get("score") or {}).get("atr"):
            return (row["data"].get("score") or {}).get("atr")
        if self.bn is not None:                      # no decision yet: compute ATR from Binance now
            try:
                daily = self.bn.klines("1d", int(self.cfg.binance.daily_candles_to_load), to_ms(self.now()))
                return compute_score(daily, utc_day(self.now()), self.cfg.strategy).atr
            except Exception as e:  # noqa: BLE001
                log.warning("ATR for adopted position unavailable: %s", e)
        return None

    # ================================================================ protection
    def ensure_protection(self, inst: Instrument, trade: dict[str, Any], pos: Position, orders: list[Order]) -> list[str]:
        actions: list[str] = []
        sls = [o for o in orders if o.tpsl_kind == "sl" and o.status in ACTIVE_TRIGGER_STATUSES]
        tps = [o for o in orders if o.tpsl_kind == "tp" and o.status in ACTIVE_TRIGGER_STATUSES]
        # review C4: an order-scoped SL must cover the whole position, else add a position SL (qty "0")
        full_sls = [o for o in sls if o.tpsl_scope == "position" or o.quantity >= abs(pos.size) - 1e-9]
        if full_sls and tps:
            return actions
        sl_price, tp_price = trade.get("sl_price"), trade.get("tp_price")
        if not full_sls:
            what = ("no active stop-loss" if not sls else
                    f"stop-loss covers only {max(o.quantity for o in sls)} of {abs(pos.size)}")
            self.alert("SL missing", f"{what} on exchange for {pos.size} {inst.symbol}; placing position SL at {sl_price}")
            mark = self.ex.get_ticker(inst.id).mark
            if sl_price is None:
                ok = False
                err = "no SL price known"
            elif (pos.direction > 0 and mark <= sl_price) or (pos.direction < 0 and mark >= sl_price):
                ok, err = False, f"mark {mark} already beyond SL {sl_price}"
            else:
                sl_s = _fmt_dec(quantize_price(sl_price, inst.price_decimals, "nearest"))
                tp_s = _fmt_dec(quantize_price(tp_price, inst.price_decimals, "nearest")) if (tp_price and not tps) else None
                self.log_order("sl_replace", "request", None, None, None, {"sl": sl_s, "tp": tp_s})
                res = self.ex.place_position_tpsl(instrument_id=inst.id, tp_trigger=tp_s, sl_trigger=sl_s)
                self.log_order("sl_replace", "response", None, res.sl_order_id, "accepted" if res.accepted else "rejected",
                               {"error": res.error, "restriction": res.restriction, "tp_order_id": res.tp_order_id})
                ok, err = res.accepted and res.sl_order_id is not None, res.error
                if not ok and res.outcome_unknown:
                    # review D13: outcome unknown -> re-read before deciding to close
                    self.sleep(float(self.cfg.polymarket.order_status_poll_interval_seconds))
                    again = [o for o in self.ex.get_open_orders(inst.id) if o.tpsl_kind == "sl"
                             and o.status in ACTIVE_TRIGGER_STATUSES
                             and (o.tpsl_scope == "position" or o.quantity >= abs(pos.size) - 1e-9)]
                    if again:
                        res.sl_order_id, ok = again[0].id, True
                if ok:
                    upd = {"sl_order_id": res.sl_order_id, "note": "SL re-placed on reconcile"}
                    if res.tp_order_id:
                        upd["tp_order_id"] = res.tp_order_id
                    self.rec.record_trade("update", trade["trade_uid"], trade["direction"], upd)
                    self.alert("SL re-placed", f"position SL re-placed at {sl_s}" + (f", TP at {tp_s}" if tp_s else ""))
                    actions.append("SL re-placed")
                    return actions
            self.alert("SL re-place FAILED", f"{err}; closing position with reduce-only market order")
            closed, _ = self.close_position(reason="protection_failure")
            actions.append("SL re-place failed -> closed" if closed else "SL re-place failed -> CLOSE FAILED")
            return actions
        if not tps and tp_price:
            tp_s = _fmt_dec(quantize_price(tp_price, inst.price_decimals, "nearest"))
            res = self.ex.place_position_tpsl(instrument_id=inst.id, tp_trigger=tp_s, sl_trigger=None)
            self.log_order("tp_replace", "response", None, res.tp_order_id, "accepted" if res.accepted else "rejected",
                           {"error": res.error, "tp": tp_s})
            if res.accepted:
                self.rec.record_trade("update", trade["trade_uid"], trade["direction"],
                                      {"tp_order_id": res.tp_order_id, "note": "TP re-placed"})
                self.alert("TP re-placed", f"take-profit re-placed at {tp_s}")
                actions.append("TP re-placed")
            else:
                self.alert("TP re-place failed", f"{res.error} (SL is in place)", dedupe_key=f"tp_fail:{self.today()}")
        return actions

    def cancel_leftovers(self, inst: Instrument, orders: list[Order] | None = None) -> list[str]:
        """When flat: cancel leftover TP/SL and reduce-only orders by id (never cancel-all)."""
        orders = self.ex.get_open_orders(inst.id) if orders is None else orders
        ids = [o.id for o in orders if o.is_active and (o.is_trigger or o.reduce_only)]
        others = [o for o in orders if o.is_active and not (o.is_trigger or o.reduce_only)]
        actions = []
        if others:
            self.alert("unexpected open orders", f"{len(others)} non-reduce-only open orders on {inst.symbol} "
                       f"(ids {[o.id for o in others]}); not cancelled", dedupe_key=f"others:{sorted(o.id for o in others)}")
        if ids:
            res = self.ex.cancel_orders(ids)
            self.log_order("cancel_leftovers", "cancel", None, None, None,
                           {"ids": ids, "results": [r.__dict__ for r in res]})
            actions.append(f"cancelled leftover orders {ids}")
        return actions

    # ================================================================ telegram commands
    def process_telegram(self) -> list[str]:
        if not self.tg.enabled:
            return []
        last = self.store.latest("telegram_updates")
        offset = int(last["update_id"]) + 1 if last else None
        actions = []
        for u in self.tg.get_updates(offset):
            uid, cmd, text = parse_command(u, self.tg.chat_id)
            if not self.store.insert_ignore("telegram_updates", update_id=uid, command=cmd, text=text[:200]):
                continue
            if cmd == "pause":
                self.cmd_pause(source="telegram")
                actions.append("telegram /pause")
            elif cmd == "kill":
                self.cmd_kill(source="telegram")
                actions.append("telegram /kill")
            elif cmd == "status":
                self.tg.send(self.status_text())
                actions.append("telegram /status")
            elif cmd == "unknown":
                self.tg.send("Commands: /pause /kill /status. Resume only via Grok Bot (`resume` after you confirm).")
        return actions

    # ================================================================ kill switches
    def kill_switch_check(self, acct: AccountSnapshot, *, allow_actions: bool) -> dict[str, Any]:
        inst = self.instrument()
        eq = self.equity(acct)
        prev = self.store.latest("equity_log")
        prev_data = prev["data"] if prev and isinstance(prev["data"], dict) else {}
        flow_adj, pending, new_flows = self.sync_flows()
        pos = acct.position(inst.id)
        if pos is not None:
            for f in new_flows:
                self.alert("deposit/withdrawal while holding",
                           f"{f.kind} {f.amount} ({f.status}) while a position is open. Not allowed until the smoketest "
                           f"has measured deposit/withdrawal timing.", dedupe_key=f"flow_hold:{f.key}:{f.status}")
        if pending:
            eq["block_entries"] = True
            self.alert("pending deposit/withdrawal",
                       f"{len(pending)} pending ({', '.join(f.kind for f in pending)}): drawdown and equity-floor checks "
                       f"skipped and new entries blocked until confirmed",
                       dedupe_key=f"flow_pending:{','.join(sorted(f.key for f in pending))}")
        prev_peak = (float(prev["peak"]) + flow_adj) if (prev and prev["peak"] is not None) else None
        nf_prev = prev_data.get("net_funded")
        net_funded = (float(nf_prev) + flow_adj) if nf_prev is not None else None
        evaluate = bool(eq["valid"]) and not pending          # review C8 / C9
        limit = float(self.cfg.risk.kill_drawdown_pct)
        if evaluate:
            dd = drawdown(eq["equity"], prev_peak, limit)
            if net_funded is None:
                net_funded = eq["equity"]                        # first valid equity = funded capital baseline
            floor = net_funded * float(self.cfg.risk.equity_floor_pct_of_net_funded) / 100.0
            floor_hit = eq["equity"] < floor
        else:
            peak = prev_peak if prev_peak is not None else 0.0
            dd = DrawdownState(eq["equity"] or 0.0, peak, float(prev["drawdown_pct"] or 0.0) if prev else 0.0, False)
            floor, floor_hit = None, False
        st = self.rec.state()
        streak_trades = self.rec.closed_trades(since_ms=self.rec.last_resume_ms())
        streak = losing_streak(streak_trades, float(self.cfg.risk.kill_losing_streak_pct))
        all_trades = self.rec.closed_trades()
        exp, n_exp = size_weighted_expectancy(all_trades, int(self.cfg.risk.expectancy_window_trades))
        status = {"evaluated": evaluate, "drawdown_pct": dd.drawdown_pct, "drawdown_triggered": dd.triggered,
                  "equity_floor": floor, "equity_floor_hit": floor_hit, "net_funded": net_funded,
                  "losing_streak_trades": streak.losing_trades, "losing_streak_pct": streak.loss_pct,
                  "losing_streak_triggered": streak.triggered, "expectancy_r": exp, "expectancy_n": n_exp,
                  "pending_flows": len(pending), "paused": st["paused"], "pause_reasons": st["pause_reasons"]}
        self.store.insert("equity_log", equity=eq["equity"], wallet=eq["wallet"], upnl=eq["upnl"],
                          peak=dd.peak if (evaluate or prev_peak is not None) else None, drawdown_pct=dd.drawdown_pct,
                          data={**eq, "flow_adjustment": flow_adj, "net_funded": net_funded, "kill": status})
        needs_close = False
        if floor_hit and "equity_floor" not in st["pause_reasons"]:
            self.rec.set_state(add_reason="equity_floor", note="equity floor hard stop")
            self.alert("KILL SWITCH: equity floor",
                       f"equity {eq['equity']:.2f} is below {self.cfg.risk.equity_floor_pct_of_net_funded}% of net funded "
                       f"capital {net_funded:.2f}. Closing position and stopping. `resume` cannot clear this; only a new "
                       f"config version can.")
            needs_close = True
        if dd.triggered and "kill_drawdown" not in st["pause_reasons"]:
            self.rec.set_state(add_reason="kill_drawdown", note="drawdown kill switch")
            self.alert("KILL SWITCH: drawdown",
                       f"equity {eq['equity']:.2f} is {dd.drawdown_pct:.2f}% below peak {dd.peak:.2f} "
                       f"(limit {limit}%). Closing position and pausing. Resume only after you confirm.")
            needs_close = True
        if streak.triggered and "kill_losing_streak" not in st["pause_reasons"]:
            self.rec.set_state(add_reason="kill_losing_streak", note="losing streak kill switch")
            self.alert("KILL SWITCH: losing streak",
                       f"{streak.losing_trades} consecutive losing trades lost {streak.streak_loss:.2f} "
                       f"= {streak.loss_pct:.2f}% of equity (limit {self.cfg.risk.kill_losing_streak_pct}%). "
                       f"New entries stopped; position and SL/TP kept. Resume only after you confirm.")
        if exp is not None and exp < 0:
            self.alert("warning: expectancy", f"{n_exp}-trade size-weighted expectancy is {exp:.3f} R (< 0)",
                       dedupe_key=f"expectancy:{len(all_trades)}")
        # review B2: while a closing kill is active and a position is still open, retry the close every run
        reasons_now = self.rec.state()["pause_reasons"]
        closing = [r for r in ("equity_floor", "kill_drawdown", "manual_kill") if r in reasons_now]
        if allow_actions and closing and not self._kill_close_tried:
            if self.ex.get_account().position(inst.id) is not None:
                if not needs_close:
                    self.alert("kill close retry", f"{closing[0]} is active and the position is still open; "
                               f"retrying the reduce-only close")
                self._kill_close_tried = True
                self.close_position(reason="manual_kill" if closing == ["manual_kill"] else "kill_switch")
        eq["kill"] = status
        eq["peak"] = dd.peak
        return eq

    def check_key_expiry(self) -> None:
        try:
            info = self.ex.get_proxy_key_info()
        except ExchangeError as e:
            log.warning("proxy key info unavailable: %s", e)
            info = None
        exp_ms = info.expires_at_ms if info and info.expires_at_ms else None
        if exp_ms is None and self.secrets.proxy_expires_at is not None:
            exp_ms = to_ms(self.secrets.proxy_expires_at)
        if info and self.secrets.wallet_address and info.owner_address.lower() != self.secrets.wallet_address.lower():
            self.alert("wallet mismatch", f"proxy key belongs to {info.owner_address}, .env PM_WALLET_ADDRESS is "
                       f"{self.secrets.wallet_address}", dedupe_key=f"wallet_mismatch:{self.today()}")
        if exp_ms is None:
            self.alert("key expiry unknown", "cannot read proxy key expiry; set PM_PROXY_EXPIRES_AT in .env",
                       dedupe_key=f"key_unknown:{self.today()}")
            return
        days_left = (exp_ms - to_ms(self.now())) / DAY_MS
        if days_left <= float(self.cfg.key.expiry_warn_days):
            self.alert("proxy key expiry", f"proxy signer key expires {fmt_utc(from_ms(exp_ms))} "
                       f"({days_left:.1f} days). Create a new proxy key and send it to Grok Bot.",
                       dedupe_key=f"key_expiry:{self.today()}")

    # ================================================================ orders
    def confirm_order(self, coid: str, res: PlaceResult, purpose: str) -> Order | None:
        """Poll order status by client order id until it is terminal (filled / not filled)."""
        if not res.accepted and not res.outcome_unknown:
            return None
        last: Order | None = None
        attempts = int(self.cfg.polymarket.order_status_poll_attempts)
        for i in range(attempts):
            try:
                found = [o for o in self.ex.get_orders(client_order_id=coid) if not o.is_trigger]
            except ExchangeError as e:
                log.warning("order status read failed (%s): %s", coid, e)
                found = []
            if found:
                last = found[0]
                self.log_order(purpose, "status", coid, last.id, last.status,
                               {"filled_quantity": last.filled_quantity, "check": i + 1})
                if last.status in FILLED_STATUSES or last.status in NOT_FILLED_TERMINAL:
                    return last
                if last.status == "partial" and last.tif in ("ioc", "fok"):
                    return last
            self.sleep(float(self.cfg.polymarket.order_status_poll_interval_seconds))
        if last is not None and last.status in ACTIVE_ORDER_STATUSES and last.tif in ("ioc", "fok"):
            # an IOC/FOK order should never rest; cancel it by id to be safe
            self.ex.cancel_orders([last.id])
            self.log_order(purpose, "cancel", coid, last.id, "cancel_requested", {"reason": "non-terminal after polling"})
        return last

    def ensure_leverage(self, inst: Instrument) -> tuple[bool, str]:
        lev, cross = int(self.cfg.risk.leverage), bool(self.cfg.risk.cross_margin)
        cfg = self.ex.get_account_config(inst.id)
        if cfg is not None and cfg.leverage == lev and cfg.cross == cross:
            return True, "already set"
        try:
            self.ex.update_leverage(inst.id, lev, cross)
        except ExchangeError as e:
            return False, f"updateLeverage failed: {e}"
        cfg = self.ex.get_account_config(inst.id)
        if cfg is None or cfg.leverage != lev or cfg.cross != cross:
            return False, f"account config after update is {cfg}"
        return True, "updated"

    def close_position(self, *, reason: str) -> tuple[bool, bool]:
        """Reduce-only IOC close, confirm zero, then cancel leftover TP/SL/reduce-only by id.
        Returns (position_closed, no_leftovers)."""
        inst = self.instrument()
        day = self.today()
        trade = self.rec.open_trade()
        slip = float(self.cfg.exits.close_slippage_bps) / 1e4
        attempts = int(self.cfg.exits.close_attempts)
        closed = False
        for attempt in range(1, attempts + 1):
            acct = self.ex.get_account()
            pos = acct.position(inst.id)
            if pos is None:
                self.sync_fills()
                flat, _why = self.confirm_flat(inst, trade)       # review C5
                if flat:
                    closed = True
                    break
                pos = self.ex.get_account().position(inst.id)
                if pos is None:
                    continue
            self.rec.set_state(position_state="pending_exit", note=f"closing: {reason}")
            side = "SELL" if pos.direction > 0 else "BUY"
            qty = quantize_qty(abs(pos.size), inst.quantity_decimals)
            if qty <= 0:
                break
            price_s = None
            if attempt < attempts:
                book = self.ex.get_book(inst.id, int(self.cfg.polymarket.book_depth))
                if side == "SELL":
                    price_s = _fmt_dec(quantize_price(book.best_bid * (1 - slip), inst.price_decimals, "down"))
                else:
                    price_s = _fmt_dec(quantize_price(book.best_ask * (1 + slip), inst.price_decimals, "up"))
            seq = self.store.count("orders", "purpose LIKE 'close:%' AND event='request' AND ts_ms >= ?",
                                   [day_start_ms(utc_day(self.now()))]) + 1
            coid = self.coid(f"close:{reason}:{seq}", day)
            req = {"side": side, "quantity": _fmt_dec(qty), "tif": "ioc", "price": price_s, "reduce_only": True,
                   "attempt": attempt, "reason": reason}
            self.log_order(f"close:{reason}", "request", coid, None, None, req)
            res = self.ex.place_order(instrument_id=inst.id, side=side, quantity=_fmt_dec(qty), tif="ioc", price=price_s,
                                      reduce_only=True, client_order_id=coid)
            self.log_order(f"close:{reason}", "response", coid, res.order_id, "accepted" if res.accepted else "rejected",
                           {"error": res.error, "restriction": res.restriction, "outcome_unknown": res.outcome_unknown})
            self.confirm_order(coid, res, f"close:{reason}")
        if not closed and self.ex.get_account().position(inst.id) is None:
            self.sync_fills()
            closed = self.confirm_flat(inst, trade)[0]
        if not closed:
            self.rec.set_state(position_state="open", note=f"close failed: {reason}")
            self.alert("CLOSE FAILURE", f"could not close position ({reason}) after {attempts} attempts. Manual attention needed.")
            return False, False
        clean = True
        for _ in range(2):
            orders = self.ex.get_open_orders(inst.id)
            left = [o for o in orders if o.is_active and (o.is_trigger or o.reduce_only)]
            if not left:
                break
            self.cancel_leftovers(inst, orders)
        else:
            orders = self.ex.get_open_orders(inst.id)
            clean = not [o for o in orders if o.is_active and (o.is_trigger or o.reduce_only)]
        if not clean:
            self.alert("leftover orders", "TP/SL or reduce-only orders remain after close; entry will not proceed")
        self.sync_fills()
        try:
            self.sync_funding(inst)
        except ExchangeError:
            pass
        if trade is not None:
            self.book_closed_trade(trade, forced_reason=reason)
        self.rec.set_state(position_state="flat", note=f"closed: {reason}")
        return True, clean

    # ================================================================ trade accounting
    def book_closed_trade(self, trade: dict[str, Any], forced_reason: str | None,
                          correction: bool = False) -> dict[str, Any]:
        d = trade["direction"]
        fills = self.exit_fill_candidates(trade)
        if not fills:
            try:
                for f in self.ex.get_fills(int(trade["entry_ts_ms"])):
                    self.store.insert_ignore("fills", trade_id=f.trade_id, order_id=f.order_id,
                                             client_order_id=f.client_order_id, side=f.side, price=f.price,
                                             quantity=f.quantity, fee=f.fee, taker=int(f.taker), pnl=f.pnl,
                                             liquidation=int(f.liquidation), previous_size=f.previous_size,
                                             fill_ts_ms=f.ts_ms, data=f.__dict__)
            except ExchangeError:
                pass
            fills = self.exit_fill_candidates(trade)
        # only fills up to the point the position reached zero
        qty_needed = float(trade["qty"])
        exit_fills: list[Fill] = []
        acc = 0.0
        for f in fills:
            exit_fills.append(f)
            acc += min(f.quantity, abs(f.previous_size))
            if acc >= qty_needed - 1e-9:
                break
        exit_qty = sum(min(f.quantity, abs(f.previous_size)) for f in exit_fills)
        exit_price = (sum(f.price * min(f.quantity, abs(f.previous_size)) for f in exit_fills) / exit_qty) if exit_qty else None
        exit_ts = exit_fills[-1].ts_ms if exit_fills else to_ms(self.now())
        gross = sum(f.pnl for f in exit_fills)
        exit_fees = sum(f.fee for f in exit_fills)
        entry_fees = float(trade.get("entry_fees") or 0.0)
        pays = self.store.query("SELECT funding FROM funding_payments WHERE pay_ts_ms >= ? AND pay_ts_ms <= ?",
                                [int(trade["entry_ts_ms"]), exit_ts])
        funding = int(self.cfg.risk.funding_payment_sign) * sum(float(p["funding"]) for p in pays)
        net = gross - entry_fees - exit_fees + funding
        reason = EXIT_REASON.get(forced_reason, forced_reason) if forced_reason else self.classify_exit(trade, exit_fills, exit_price)
        risk = float(trade.get("initial_risk_usd") or 0.0)
        mae, mfe = self.excursions(trade, exit_ts, exit_price)
        sl_dist = float(trade.get("sl_distance") or 0.0)
        close = {
            "exit_ts_ms": exit_ts, "exit_utc": fmt_utc(from_ms(exit_ts)), "exit_price": exit_price,
            "exit_reason": reason, "gross_pnl": gross, "entry_fees": entry_fees, "exit_fees": exit_fees,
            "fees_total": entry_fees + exit_fees, "funding": funding, "net_pnl": net,
            "r_multiple": (net / risk) if risk else None, "mae": mae, "mfe": mfe,
            "mae_r": (mae / sl_dist) if (mae is not None and sl_dist) else None,
            "mfe_r": (mfe / sl_dist) if (mfe is not None and sl_dist) else None,
            "holding_hours": (exit_ts - int(trade["entry_ts_ms"])) / HOUR_MS,
            "exit_fill_ids": [f.trade_id for f in exit_fills], "incomplete": not exit_fills,
            "forced_reason": forced_reason, "correction": correction,
        }
        self.rec.record_trade("close", trade["trade_uid"], d, close)
        self.alert("close (corrected)" if correction else "close", f"{'LONG' if d > 0 else 'SHORT'} {trade['qty']} closed ({reason}) @ {exit_price if exit_price is None else round(exit_price, 2)}; "
                   f"net PnL {net:.2f} (gross {gross:.2f}, fees {entry_fees + exit_fees:.2f}, funding {funding:.2f})"
                   + (f", {close['r_multiple']:.2f} R" if close["r_multiple"] is not None else "")
                   + (" [exit fills not found yet]" if not exit_fills else ""))
        return close

    def exit_fill_candidates(self, trade: dict[str, Any]) -> list[Fill]:
        """Fills after entry taken while the position was open (signed previous_size first;
        falls back to any non-flat previous_size if the API reports it unsigned)."""
        d = trade["direction"]
        cands = [f for f in self.stored_fills(int(trade["entry_ts_ms"])) if f.is_reducing
                 and f.order_id != trade.get("entry_order_id")]
        signed = [f for f in cands if (f.previous_size > 0) == (d > 0)]
        return signed or cands

    def repair_incomplete_closes(self) -> None:
        """Re-book closes that were recorded before their exit fills were visible (last 3 days)."""
        since = to_ms(self.now()) - 3 * DAY_MS
        for c in self.store.query("SELECT trade_uid, data FROM trades WHERE event='close' AND ts_ms >= ?", [since]):
            if not c["data"].get("incomplete"):
                continue
            latest = self.store.latest("trades", "event='close' AND trade_uid=?", [c["trade_uid"]])
            if latest is None or not latest["data"].get("incomplete"):
                continue
            o = self.store.latest("trades", "event='open' AND trade_uid=?", [c["trade_uid"]])
            if o is None:
                continue
            trade = dict(o["data"])
            for u in self.store.query("SELECT data FROM trades WHERE event='update' AND trade_uid=? ORDER BY id",
                                      [c["trade_uid"]]):
                trade.update({k: v for k, v in u["data"].items() if k != "note"})
            if self.exit_fill_candidates(trade):
                self.book_closed_trade(trade, forced_reason=c["data"].get("forced_reason"), correction=True)

    def classify_exit(self, trade: dict[str, Any], exit_fills: list[Fill], exit_price: float | None) -> str:
        if any(f.liquidation for f in exit_fills):
            return "liquidation"
        ids = {trade.get("sl_order_id"): "SL", trade.get("tp_order_id"): "TP"}
        for f in exit_fills:
            if f.order_id in ids and f.order_id is not None:
                return ids[f.order_id]
            if f.client_order_id:
                row = self.store.latest("orders", "client_order_id = ? AND event='request'", [f.client_order_id])
                if row and str(row["purpose"]).startswith("close:"):
                    r = str(row["purpose"]).split(":", 1)[1]
                    return EXIT_REASON.get(r, r)
            try:
                for o in self.ex.get_orders(order_id=f.order_id):
                    if o.tpsl_kind:
                        return o.tpsl_kind.upper()
                    if o.parent_order_id in ids and o.parent_order_id is not None:
                        return ids[o.parent_order_id]
            except ExchangeError:
                pass
        sl, tp = trade.get("sl_price"), trade.get("tp_price")
        if exit_price is not None and sl is not None and tp is not None:
            return ("SL" if abs(exit_price - sl) <= abs(exit_price - tp) else "TP") + " (inferred)"
        return "unknown"

    def excursions(self, trade: dict[str, Any], exit_ts: int, exit_price: float | None) -> tuple[float | None, float | None]:
        """MAE/MFE in price units from stored Polymarket 1h klines + recorded marks + exit price."""
        entry = float(trade["entry_price"])
        d = trade["direction"]
        start_hour = int(trade["entry_ts_ms"]) // HOUR_MS * HOUR_MS
        rows = self.store.query("SELECT high, low FROM pm_klines_1h WHERE open_ms >= ? AND open_ms <= ?", [start_hour, exit_ts])
        highs = [r["high"] for r in rows]
        lows = [r["low"] for r in rows]
        marks = [r["mark"] for r in self.store.query(
            "SELECT json_extract(data, '$.mark') AS mark FROM manage_log WHERE ts_ms >= ? AND ts_ms <= ?",
            [int(trade["entry_ts_ms"]), exit_ts]) if r["mark"] is not None]
        pts = highs + lows + marks + ([exit_price] if exit_price is not None else []) + [entry]
        if not pts:
            return None, None
        hi, lo = max(pts), min(pts)
        if d > 0:
            return max(entry - lo, 0.0), max(hi - entry, 0.0)
        return max(hi - entry, 0.0), max(entry - lo, 0.0)

    # ================================================================ entry
    def open_position(self, day: str, plan: dict[str, Any], attempts_used: int) -> bool:
        inst = self.instrument()
        d = int(plan["enter_direction"])
        ok, why = self.ensure_leverage(inst)
        if not ok:
            self.rec.intent_event(day, "entry", "failed", {"reason": why})
            self.alert("entry blocked", f"account config is not {self.cfg.risk.leverage}x isolated and could not be set: {why}. No trade today.")
            return False
        left = [o for o in self.ex.get_open_orders(inst.id) if o.is_active and (o.is_trigger or o.reduce_only)]
        if left:
            self.cancel_leftovers(inst, left)
            left = [o for o in self.ex.get_open_orders(inst.id) if o.is_active and (o.is_trigger or o.reduce_only)]
            if left:
                self.rec.intent_event(day, "entry", "failed", {"reason": "leftover TP/SL or reduce-only orders"})
                self.alert("entry blocked", f"leftover TP/SL/reduce-only orders {[o.id for o in left]} could not be "
                           f"cancelled; no entry today")
                return False
        acct = self.ex.get_account()
        eq = self.equity(acct)
        pending = [f for f in self.sync_flows()[1]]
        if eq["block_entries"] or pending:
            why = "pending deposit/withdrawal" if pending else (
                "equity unreadable" if not eq["valid"] else "equity sources disagree")
            self.rec.intent_event(day, "entry", "blocked", {"reason": why, "equity": eq})
            self.alert("entry blocked", f"{why}; no new entry", dedupe_key=f"entry_blocked:{day}:{why}")
            return False
        n_prior = self.rec.live_trades_opened()
        risk_pct, ramp = risk_pct_for_trade(self.cfg.risk, n_prior)
        if n_prior == int(self.cfg.risk.ramp_trades) and int(self.cfg.risk.ramp_trades) > 0:
            self.alert("ramp complete", f"{n_prior} live trades done: switching from {self.cfg.risk.ramp_factor}x to full "
                       f"risk ({self.cfg.risk.risk_per_trade_pct}% at the 100% tier) from this trade",
                       dedupe_key="ramp_complete")
        atr = float(plan["atr"])
        slip = float(self.cfg.exits.entry_slippage_bps) / 1e4
        side = "BUY" if d > 0 else "SELL"
        filled: Order | None = None
        res: PlaceResult | None = None
        size = None
        sl_s = tp_s = None
        ref = 0.0
        max_attempts = int(self.cfg.exits.entry_attempts)
        attempt_start_ms = 0
        for attempt in range(attempts_used + 1, max_attempts + 1):
            book = self.ex.get_book(inst.id, int(self.cfg.polymarket.book_depth))
            ref = book.best_ask if d > 0 else book.best_bid
            limit = quantize_price(ref * (1 + d * slip), inst.price_decimals, "down" if d > 0 else "up")
            size = compute_size(equity=eq["equity"], risk_pct=risk_pct, fraction=float(plan["enter_fraction"]), price=ref,
                                atr=atr, sl_atr_multiple=float(self.cfg.exits.sl_atr_multiple),
                                notional_cap_pct=float(self.cfg.risk.notional_cap_pct_equity),
                                leverage=int(self.cfg.risk.leverage), inst=inst)
            if not size.ok:
                self.rec.intent_event(day, "entry", "failed", {"reason": size.reject_reason, "size": size.to_dict()})
                self.alert("entry rejected", f"order violates limits: {size.reject_reason}")
                return False
            sl_p, tp_p = bracket_prices(d, ref, atr, float(self.cfg.exits.sl_atr_multiple), float(self.cfg.exits.tp_atr_multiple))
            sl_q = quantize_price(sl_p, inst.price_decimals, "nearest")
            tp_q = quantize_price(tp_p, inst.price_decimals, "nearest")
            try:
                validate_price(inst, limit)
                validate_price(inst, sl_q)
                validate_price(inst, tp_q)
                validate_order(inst, qty=size.qty, price=float(limit), leverage=int(self.cfg.risk.leverage), market=False)
            except Exception as e:  # noqa: BLE001
                self.rec.intent_event(day, "entry", "failed", {"reason": str(e)})
                self.alert("entry rejected", f"order violates instrument rules: {e}")
                return False
            liq_est = estimate_liquidation(ref, d, int(self.cfg.risk.leverage), inst, size.notional,
                                           float(self.cfg.risk.liq_estimate_mmr_divisor))
            if not liquidation_ok(ref, liq_est, size.sl_distance, float(self.cfg.risk.liq_min_sl_multiple)):
                self.rec.intent_event(day, "entry", "failed", {"reason": "liquidation check (estimate)", "liq_est": liq_est})
                self.alert("entry rejected", f"estimated liquidation {liq_est:.2f} closer than "
                           f"{self.cfg.risk.liq_min_sl_multiple}x SL distance {size.sl_distance:.2f}")
                return False
            sl_s, tp_s = _fmt_dec(sl_q), _fmt_dec(tp_q)
            coid = self.coid(f"entry:{attempt}", day)
            self.rec.intent_event(day, "entry_attempt", "started", {"attempt": attempt, "coid": coid})
            self.rec.set_state(position_state="pending_entry", note=f"entry attempt {attempt}")
            req = {"side": side, "quantity": _fmt_dec(size.qty), "tif": "fok", "price": _fmt_dec(limit), "tp": tp_s,
                   "sl": sl_s, "ref_price": ref, "decision_mark": plan.get("mark"), "size": size.to_dict(),
                   "risk_pct_budget": risk_pct, "ramp": ramp, "equity": eq, "attempt": attempt, "liq_estimate": liq_est}
            self.log_order("entry", "request", coid, None, None, req)
            res = self.ex.place_order(instrument_id=inst.id, side=side, quantity=_fmt_dec(size.qty), tif="fok",
                                      price=_fmt_dec(limit), reduce_only=False, client_order_id=coid,
                                      tp_trigger=tp_s, sl_trigger=sl_s)
            self.log_order("entry", "response", coid, res.order_id, "accepted" if res.accepted else "rejected",
                           {"error": res.error, "restriction": res.restriction, "outcome_unknown": res.outcome_unknown,
                            "tp_order_id": res.tp_order_id, "sl_order_id": res.sl_order_id})
            attempt_start_ms = to_ms(self.now()) - 60_000
            o = self.confirm_order(coid, res, "entry")
            if o is not None and o.status in FILLED_STATUSES:
                filled = o
                self.rec.intent_event(day, "entry_attempt", "filled", {"attempt": attempt, "order_id": o.id})
                break
            pos_now = self.ex.get_account().position(inst.id)
            if pos_now is not None and pos_now.direction == d:
                # status read did not show a fill, but the position exists: treat as filled
                filled = o if o is not None else Order(
                    id=res.order_id or -1, instrument_id=inst.id, side=side, price=float(limit),
                    quantity=float(size.qty), tif="fok", reduce_only=False, status="filled",
                    filled_quantity=abs(pos_now.size), resting_quantity=0.0, client_order_id=coid)
                self.rec.intent_event(day, "entry_attempt", "filled",
                                      {"attempt": attempt, "order_id": filled.id, "confirmed_by": "position"})
                break
            self.rec.intent_event(day, "entry_attempt", "unfilled",
                                  {"attempt": attempt, "status": o.status if o else None, "error": res.error,
                                   "restriction": res.restriction})
            if attempt < max_attempts:
                ok_retry, why = self._retry_allowed(inst, day, coid, res, o, attempt_start_ms)
                if not ok_retry:
                    # one more read after a pause: a late-visible fill is recorded and protected now
                    self.sleep(float(self.cfg.polymarket.flat_confirm_delay_seconds))
                    late = self.ex.get_account().position(inst.id)
                    if late is not None and late.direction == d:
                        filled = o if (o is not None and o.status in FILLED_STATUSES) else Order(
                            id=res.order_id or -1, instrument_id=inst.id, side=side, price=float(limit),
                            quantity=float(size.qty), tif="fok", reduce_only=False, status="filled",
                            filled_quantity=abs(late.size), resting_quantity=0.0, client_order_id=coid)
                        self.rec.intent_event(day, "entry_attempt", "filled",
                                              {"attempt": attempt, "confirmed_by": "late position read"})
                        break
                    # review B1: no second order in this run without proof the first did not fill
                    self.rec.intent_event(day, "entry", "deferred", {"after_attempt": attempt, "reason": why})
                    self.rec.set_state(position_state="flat", note="entry retry deferred")
                    self.alert("entry retry deferred", f"attempt {attempt} not filled but not proven unfilled ({why}); "
                               f"no retry in this run. The next decide run re-checks the exchange first.")
                    return False
        if filled is None or res is None or size is None:
            self.rec.intent_event(day, "entry", "failed", {"reason": f"FOK not filled after {max_attempts} attempts"})
            self.rec.set_state(position_state="flat", note="entry failed")
            self.alert("no entry", f"FOK entry not filled after {max_attempts} attempts; no entry today")
            return False
        return self._record_entry(day, plan, inst, filled, res, size, sl_s, tp_s, ref, eq, risk_pct, ramp, atr,
                                  attempt_start_ms)

    def _retry_allowed(self, inst: Instrument, day: str, coid: str, res: PlaceResult, o: Order | None,
                       since_ms: int) -> tuple[bool, str]:
        """Review B1: a second FOK in the same run needs positive proof the previous one did not fill."""
        if res.outcome_unknown:
            return False, "previous attempt outcome unknown"
        if not res.accepted:
            return False, (f"previous attempt rejected ({res.error}); the SDK rejects a whole bracket when one row is "
                           f"rejected, even if the entry row filled")
        try:
            found = [x for x in self.ex.get_orders(client_order_id=coid) if not x.is_trigger]
        except ExchangeError as e:
            return False, f"order status unreadable: {e}"
        st = found[0].status if found else (o.status if o else None)
        if st not in NOT_FILLED_TERMINAL:
            return False, f"previous order status {st!r} is not a terminal not-filled status"
        self.sync_fills()
        smoke = self.smoketest_coids()
        if any(f.is_opening and f.ts_ms >= since_ms and f.client_order_id not in smoke for f in self.stored_fills(since_ms)):
            return False, "an opening fill was seen after the attempt"
        if self.ex.get_account().position(inst.id) is not None:
            return False, "a position exists"
        if self.entered_today(day):
            return False, "an entry is already recorded today"
        return True, f"previous attempt {st}"

    def _record_entry(self, day: str, plan: dict[str, Any], inst: Instrument, order: Order, res: PlaceResult, size: Any,
                      sl_s: str | None, tp_s: str | None, ref: float, eq: dict[str, Any], risk_pct: float, ramp: bool,
                      atr: float, attempt_start_ms: int = 0) -> bool:
        d = int(plan["enter_direction"])
        self.sync_fills()
        recent = self.stored_fills(to_ms(self.now()) - DAY_MS)
        fills = ([f for f in recent if f.order_id == order.id]
                 or [f for f in recent if order.client_order_id and f.client_order_id == order.client_order_id]
                 or [f for f in recent if f.is_opening and f.ts_ms >= attempt_start_ms])
        qty = sum(f.quantity for f in fills) or float(order.filled_quantity) or float(size.qty)
        entry_price = (sum(f.price * f.quantity for f in fills) / qty) if fills else ref
        entry_ts = fills[0].ts_ms if fills else to_ms(self.now())
        acct = self.ex.get_account()
        pos = acct.position(inst.id)
        if pos is not None:
            entry_price = pos.entry_price or entry_price
            qty = abs(pos.size)
        orders = self.ex.get_open_orders(inst.id)
        sl_id = res.sl_order_id or next((o.id for o in orders if o.tpsl_kind == "sl" and o.parent_order_id == order.id), None)
        tp_id = res.tp_order_id or next((o.id for o in orders if o.tpsl_kind == "tp" and o.parent_order_id == order.id), None)
        sl_price, tp_price = float(sl_s), float(tp_s)
        sl_dist = abs(entry_price - sl_price)
        mark = plan.get("mark")
        trade = {
            "trade_uid": uuid.uuid4().hex, "direction": d, "qty": qty, "entry_price": entry_price, "entry_ts_ms": entry_ts,
            "entry_utc_day": day, "sl_price": sl_price, "tp_price": tp_price, "sl_order_id": sl_id, "tp_order_id": tp_id,
            "atr": atr, "sl_distance": sl_dist, "initial_risk_usd": sl_dist * qty, "equity_at_entry": eq["equity"],
            "risk_pct_budget": risk_pct, "ramp": ramp, "tier_fraction": plan.get("tier_fraction"),
            "effective_fraction": plan.get("enter_fraction"), "caps": plan.get("caps"), "score": plan.get("score"),
            "gates_triggered": plan.get("gates_triggered"), "entry_fees": sum(f.fee for f in fills),
            "maker_taker": ["taker" if f.taker else "maker" for f in fills], "entry_order_id": order.id,
            "entry_coid": order.client_order_id, "decision_mark": mark, "decision_ts_ms": plan.get("decision_ts_ms"),
            "slippage_bps_vs_decision_mark": ((entry_price - mark) / mark * 1e4 * d) if mark else None,
            "decision_to_fill_s": ((entry_ts - int(plan["decision_ts_ms"])) / 1000) if plan.get("decision_ts_ms") else None,
            "liquidation_price": pos.liquidation_price if pos else None, "adopted": False, "external": False, "live": True,
            "plan_action": plan.get("action"),
        }
        self.rec.record_trade("open", trade["trade_uid"], d, trade)
        self.rec.intent_event(day, "entry", "done", {"order_id": order.id, "trade_uid": trade["trade_uid"]})
        self.rec.set_state(position_state="open", note="entry filled")
        self.log_order("entry", "fills", order.client_order_id, order.id, "filled",
                       {"fills": [f.__dict__ for f in fills], "entry_price": entry_price, "qty": qty,
                        "slippage_bps_vs_decision_mark": trade["slippage_bps_vs_decision_mark"],
                        "decision_to_fill_s": trade["decision_to_fill_s"]})
        self.alert("open", f"{'LONG' if d > 0 else 'SHORT'} {qty} {inst.symbol} @ {entry_price:.2f} | SL {sl_s} TP {tp_s} | "
                   f"risk {trade['initial_risk_usd']:.2f} ({risk_pct * float(plan.get('enter_fraction') or 0):.3f}% equity)"
                   f"{' [ramp]' if ramp else ''} | score {plan.get('score')}")
        if pos is not None and not liquidation_ok(entry_price, pos.liquidation_price, sl_dist,
                                                  float(self.cfg.risk.liq_min_sl_multiple), isolated=not pos.cross):
            self.alert("liquidation check", f"liquidation price {pos.liquidation_price!r} is missing or closer than "
                       f"{self.cfg.risk.liq_min_sl_multiple}x SL distance {sl_dist:.2f}; closing")
            self.close_position(reason="liq_check")
            return False
        if pos is not None:
            self.ensure_protection(inst, self.rec.open_trade() or trade, pos, orders)
        return True

    # ================================================================ decision inputs
    def gather_inputs(self, day_d: Any) -> dict[str, Any]:
        cfg = self.cfg
        now_ms = to_ms(self.now())
        inst = self.instrument()
        daily = self.bn.klines("1d", int(cfg.binance.daily_candles_to_load), now_ms)
        h4 = self.bn.klines("4h", int(cfg.binance.h4_candles_to_load), now_ms)
        fstart = day_start_ms(day_d) - int(cfg.binance.funding_days_to_load) * DAY_MS
        funding = self.bn.funding(fstart, now_ms)
        self.store_binance(daily, h4, funding)
        ticker = self.ex.get_ticker(inst.id)
        book = self.ex.get_book(inst.id, int(cfg.polymarket.book_depth))
        try:
            pm_funding = self.ex.get_funding_history(inst.id, now_ms - DAY_MS, now_ms)
        except ExchangeError as e:
            log.warning("polymarket funding history failed: %s", e)
            pm_funding = []
        try:
            raw_geo = self.ex.get_geoblock()
            geo = {k: raw_geo.get(k) for k in ("blocked", "country", "region")}
        except ExchangeError as e:
            geo = {"error": str(e)}
        return {"daily": daily, "h4": h4, "funding": funding, "ticker": ticker, "book": book, "pm_funding": pm_funding,
                "geoblock": geo}

    def store_binance(self, daily: list[Candle], h4: list[Candle], funding: list[tuple[int, float, float]]) -> None:
        self.store.insert_many_ignore("bn_klines_1d", ({"open_ms": c.open_ms, "open": c.open, "high": c.high, "low": c.low,
                                                        "close": c.close, "volume": c.volume} for c in daily))
        self.store.insert_many_ignore("bn_klines_4h", ({"open_ms": c.open_ms, "open": c.open, "high": c.high, "low": c.low,
                                                        "close": c.close, "volume": c.volume} for c in h4))
        self.store.insert_many_ignore("bn_funding", ({"fund_ts_ms": ts, "rate": r, "mark": m} for ts, r, m in funding))

    def make_decision(self, day_d: Any, inputs: dict[str, Any], rr: ReconcileResult) -> dict[str, Any]:
        cfg = self.cfg
        now = self.now()
        day = day_d.isoformat()
        sc = compute_score(inputs["daily"], day_d, cfg.strategy)
        e_fast, e_slow, h4_open = h4_emas(inputs["h4"], day_d, int(cfg.gates.h4_ema_fast), int(cfg.gates.h4_ema_slow))
        cutoff = day_start_ms(day_d) + int(float(cfg.gates.funding_cutoff_tolerance_minutes) * 60_000)
        fstat = funding_percentile([(ts, r) for ts, r, _ in inputs["funding"]], cutoff, int(cfg.gates.funding_lookback_days))
        dirs = direction_history(inputs["daily"], day_d, int(cfg.strategy.opposite_days_rule) + 2, cfg.strategy)
        trade = rr.trade
        pos_dir = rr.position.direction if rr.position else 0
        entry_day = datetime.fromisoformat(trade["entry_utc_day"]).date() if (trade and trade.get("entry_utc_day")) else day_d
        streak = opposite_streak(pos_dir, entry_day, dirs, day_d)
        active = self.calendar.active_windows(now, cfg.gates.event_anchor_hkt, float(cfg.gates.event_post_release_hours))
        ev_list = [{"type": e.type, "release_utc": fmt_utc(e.release_utc), "window_start_utc": fmt_utc(s),
                    "window_end_utc": fmt_utc(en), "note": e.note} for e, s, en in active]
        g_regime = regime_gate(sc.close, sc.ema_regime, sc.direction, float(cfg.gates.regime_cap))
        g_h4 = h4_gate(e_fast, e_slow, h4_open, sc.direction, float(cfg.gates.h4_cap))
        g_fund = funding_gate(fstat.percentile, sc.direction, float(cfg.gates.funding_high_percentile),
                              float(cfg.gates.funding_low_percentile))
        g_event = event_gate(ev_list)
        caps = [(g.name, float(g.cap)) for g in (g_regime, g_h4) if g.triggered and g.cap is not None]
        paused = self.paused_reason()
        geo = inputs.get("geoblock") or {}
        region_blocked = geo.get("blocked") is True
        entered = self.entered_today(day)
        ctx = DecisionContext(
            direction=sc.direction, abs_score=sc.abs_score, tier_fraction=sc.tier_fraction, caps=caps,
            funding_pct=fstat.percentile, funding_high=float(cfg.gates.funding_high_percentile),
            funding_low=float(cfg.gates.funding_low_percentile), event_active=g_event.triggered, position_dir=pos_dir,
            opposite_streak=streak, entered_today=entered, paused_reason=paused,
            flip_min_abs_score=float(cfg.strategy.flip_min_abs_score),
            opposite_days_rule=int(cfg.strategy.opposite_days_rule),
            event_allows_rule_closes=bool(cfg.gates.event_allows_rule_closes))
        plan = decide_plan(ctx)
        if region_blocked and plan.enter_direction:
            plan.entry_blocked.append(f"region blocked by geoblock ({geo.get('country')}/{geo.get('region')})")
            plan.enter_direction, plan.enter_fraction = 0, 0.0
            plan.action = "close" if plan.close_reason else "none"
            plan.target_direction = 0 if plan.close_reason else pos_dir
        ticker, book = inputs["ticker"], inputs["book"]
        gates_triggered = [g.name for g in (g_regime, g_h4, g_fund, g_event) if g.triggered]
        pm_rates = [r for _, r in inputs["pm_funding"]]
        plan_d = plan.to_dict()
        plan_d.update({
            "utc_day": day, "score": sc.score, "direction": sc.direction, "abs_score": sc.abs_score,
            "tier_fraction": sc.tier_fraction, "caps": caps, "gates_triggered": gates_triggered, "atr": sc.atr,
            "mark": ticker.mark, "decision_ts_ms": to_ms(now), "position_dir_at_decision": pos_dir,
            "funding_percentile": fstat.percentile, "event_active": g_event.triggered,
        })
        spread = book.best_ask - book.best_bid if (book.bids and book.asks) else None
        decision = {
            "utc_day": day, "decision_hkt": fmt_hkt(now), "decision_utc": fmt_utc(now),
            "score": sc.to_dict(),
            "inputs": {
                "daily_candles_used": [[c.open_ms, c.open, c.high, c.low, c.close] for c in inputs["daily"]
                                       if c.open_ms <= sc.candle_open_ms],
                "C": sc.close, "H": sc.high, "L": sc.low, "prev_H": sc.prev_high, "prev_L": sc.prev_low,
                "ema50": sc.ema_trend, "ema200": sc.ema_regime, "atr14": sc.atr, "clv": sc.clv,
                "h4_ema20": e_fast, "h4_ema50": e_slow, "h4_candle_open_ms": h4_open,
                "binance_funding_rate": fstat.current_rate, "binance_funding_ts_ms": fstat.current_ts_ms,
                "binance_funding_percentile": fstat.percentile, "binance_funding_samples": fstat.samples,
                "polymarket_funding_rate": ticker.funding_rate,
                "polymarket_funding_24h_mean": (sum(pm_rates) / len(pm_rates)) if pm_rates else None,
                "polymarket_funding_sign_agrees": (None if not pm_rates else
                                                   (sum(pm_rates) >= 0) == (fstat.current_rate >= 0)),
                "mark": ticker.mark, "index": ticker.index, "spread": spread,
                "book_top10": {"bids": book.bids[:10], "asks": book.asks[:10]},
                "direction_history": dirs, "opposite_streak": streak, "position_dir": pos_dir,
                "entry_day": entry_day.isoformat(), "entered_today": entered, "geoblock": geo,
            },
            "size_tier": sc.tier_fraction,
            "gates": {g.name: {"triggered": g.triggered, "cap": g.cap, "blocks_entry": g.blocks_entry, "detail": g.detail}
                      for g in (g_regime, g_h4, g_fund, g_event)},
            "paused_reason": paused,
            "plan": plan_d,
        }
        reason = "; ".join(plan.notes + plan.entry_blocked) or plan.action
        self.store.insert("decisions", utc_day=day, score=sc.score, direction=sc.direction, action=plan.action,
                          reason=reason, data=decision)
        return plan_d

    # ================================================================ intent execution
    def execute_intent(self, day: str, plan: dict[str, Any], *, allow_entry: bool, window_open: bool) -> list[str]:
        inst = self.instrument()
        actions: list[str] = []
        intent_row = self.store.latest("intents", "utc_day = ?", [day])
        since = int(intent_row["ts_ms"]) if intent_row else 0
        events = self.rec.intent_events(day, since)
        done = {(e["step"], e["status"]) for e in events}
        acct = self.ex.get_account()
        pos = acct.position(inst.id)

        if plan.get("close_reason") and ("close", "done") not in done:
            old_dir = int(plan.get("position_dir_at_decision") or 0)
            if pos is None:
                self.rec.intent_event(day, "close", "done", {"note": "already flat"})
            elif pos.direction == old_dir:
                if self.paused_reason():
                    actions.append("paused: planned close not executed")
                    return actions
                self.rec.intent_event(day, "close", "started", {"reason": plan["close_reason"]})
                closed, clean = self.close_position(reason=plan["close_reason"])
                if not closed or not clean:
                    self.rec.intent_event(day, "close", "failed", {"closed": closed, "clean": clean})
                    actions.append("close failed")
                    return actions
                self.rec.intent_event(day, "close", "done", {"reason": plan["close_reason"]})
                actions.append(f"closed ({plan['close_reason']})")
                pos = None
                # review C6: the close may have realised a loss that trips a kill switch
                self.kill_switch_check(self.ex.get_account(), allow_actions=True)
                if self.paused_reason() and int(plan.get("enter_direction") or 0):
                    self.rec.intent_event(day, "entry", "blocked", {"reason": f"kill switch after close: {self.paused_reason()}"})
                    actions.append("entry blocked: kill switch after close")
                    return actions
            else:
                self.rec.intent_event(day, "close", "skipped", {"note": f"position direction {pos.direction} changed"})
                actions.append("close skipped: position changed")
                return actions

        enter = int(plan.get("enter_direction") or 0)
        if not enter:
            return actions
        if ("entry", "done") in done or ("entry", "failed") in done or ("entry", "missed") in done:
            return actions
        if pos is not None:
            if pos.direction == enter:
                self.rec.intent_event(day, "entry", "done", {"note": "position already in target direction"})
            else:
                actions.append("entry skipped: still holding opposite position")
            return actions
        if not allow_entry or not window_open:
            self.rec.intent_event(day, "entry", "missed", {"allow_entry": allow_entry, "window_open": window_open})
            self.alert("entry missed", f"planned {'LONG' if enter > 0 else 'SHORT'} entry for {day} not placed "
                       f"({'entry window closed' if not window_open else 'this command never opens'}); staying flat today")
            return actions
        if self.paused_reason():
            self.rec.intent_event(day, "entry", "blocked", {"reason": self.paused_reason()})
            return actions
        # resolve attempts started by an interrupted run
        started = [e["data"] for e in events if e["step"] == "entry_attempt" and e["status"] == "started"]
        resolved = {e["data"].get("attempt") for e in events if e["step"] == "entry_attempt" and e["status"] != "started"}
        for s in started:
            if s.get("attempt") in resolved:
                continue
            orders = [o for o in self.ex.get_orders(client_order_id=s["coid"]) if not o.is_trigger]
            st = orders[0].status if orders else "not_found"
            self.rec.intent_event(day, "entry_attempt", "filled" if st in FILLED_STATUSES else "unfilled",
                                  {"attempt": s.get("attempt"), "status": st, "resolved_on_resume": True})
            if st in FILLED_STATUSES:
                actions.append("entry recovered from interrupted run")
                return actions
        if self.entered_today(day):
            self.rec.intent_event(day, "entry", "blocked", {"reason": "entry already happened today"})
            return actions
        if len(started) >= int(self.cfg.exits.entry_attempts):
            self.rec.intent_event(day, "entry", "failed", {"reason": "all attempts used"})
            return actions
        if self.open_position(day, plan, attempts_used=len(started)):
            actions.append("entered")
            if plan.get("close_reason") and int(plan.get("position_dir_at_decision") or 0) == -enter:
                names = {1: "LONG", -1: "SHORT"}
                self.alert("flip", f"{names[-enter]} -> {names[enter]} completed ({plan['close_reason']})")
        else:
            actions.append("entry failed/rejected")
        return actions

    # ================================================================ market data logging
    def log_market_data(self) -> None:
        inst = self.instrument()
        now_ms = to_ms(self.now())
        try:
            t = self.ex.get_ticker(inst.id)
            b = self.ex.get_book(inst.id, int(self.cfg.polymarket.book_depth))
            depth_bid = sum(q for _, q in b.bids)
            depth_ask = sum(q for _, q in b.asks)
            snap = {"mark": t.mark, "index": t.index, "last": t.last, "funding_rate": t.funding_rate,
                    "open_interest": t.open_interest, "best_bid": b.bids[0][0] if b.bids else None,
                    "best_ask": b.asks[0][0] if b.asks else None,
                    "spread": (b.asks[0][0] - b.bids[0][0]) if (b.bids and b.asks) else None,
                    "depth_bid_qty": depth_bid, "depth_ask_qty": depth_ask, "book": {"bids": b.bids, "asks": b.asks}}
            try:
                snap["binance_price"] = self.bn.price()
            except Exception as e:  # noqa: BLE001
                snap["binance_price_error"] = str(e)
            self.store.insert("market_snapshots", data=snap)
            max_back = int(float(self.cfg.polymarket.kline_backfill_max_days) * DAY_MS)
            for table, interval, step in (("pm_klines_1h", "1h", HOUR_MS), ("pm_klines_1d", "1d", DAY_MS)):
                last = self.store.latest(table)
                start = int(last["open_ms"]) + step if last else now_ms - max_back
                start = max(start, now_ms - max_back)
                if start < now_ms - step:
                    kl = [c for c in self.ex.get_klines(inst.id, interval, start, now_ms) if c.open_ms + step <= now_ms]
                    self.store.insert_many_ignore(table, ({"open_ms": c.open_ms, "open": c.open, "high": c.high, "low": c.low,
                                                           "close": c.close, "volume": c.volume} for c in kl))
            last = self.store.latest("pm_funding")
            start = int(last["fund_ts_ms"]) + 1 if last else now_ms - max_back
            fh = self.ex.get_funding_history(inst.id, max(start, now_ms - max_back), now_ms)
            self.store.insert_many_ignore("pm_funding", ({"fund_ts_ms": ts, "rate": r} for ts, r in fh))
            last = self.store.latest("bn_funding")
            bstart = int(last["fund_ts_ms"]) + 1 if last else now_ms - 7 * DAY_MS
            try:
                self.store.insert_many_ignore("bn_funding", ({"fund_ts_ms": ts, "rate": r, "mark": m}
                                                             for ts, r, m in self.bn.funding(bstart, now_ms)))
            except Exception as e:  # noqa: BLE001
                log.warning("binance funding backfill failed: %s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("market data logging failed: %s", e)
            self.alert("warning: market data", f"market data logging failed: {e}", dedupe_key=f"mdlog:{self.today()}")

    # ================================================================ commands
    def cmd_decide(self) -> dict[str, Any]:
        cfg = self.cfg
        now = self.now()
        day_d = utc_day(now)
        day = day_d.isoformat()
        rr = self.reconcile()
        self.log_market_data()
        ws, we = entry_window(now, cfg.schedule.entry_window_start_hkt, cfg.schedule.entry_window_end_hkt)
        intent = self.rec.intent(day)
        out: dict[str, Any] = {"reconcile": rr.actions}
        if now < ws:
            out["result"] = "before entry window; nothing to do"
            return out
        if now > we:
            if intent is None or (intent.get("enter_direction") and not self._entry_resolved(day)):
                self.store.insert("decisions", utc_day=day, score=None, direction=None, action="missed",
                                  reason="decide ran after the entry window", data={"run_hkt": fmt_hkt(now)})
                self.alert("missed", f"decide ran at {fmt_hkt(now)}, after the entry window "
                           f"({cfg.schedule.entry_window_end_hkt} HKT); no late entry", dedupe_key=f"missed_window:{day}")
            if intent is not None:
                out["actions"] = self.execute_intent(day, intent, allow_entry=False, window_open=False)
            out["result"] = "missed entry window"
            return out
        if intent is None or (intent.get("action") == "paused" and not self.paused_reason()):
            inputs = self.gather_inputs(day_d)
            try:
                intent = self.make_decision(day_d, inputs, rr)
            except InsufficientData as e:
                self.store.insert("decisions", utc_day=day, score=None, direction=None, action="error",
                                  reason=f"insufficient data: {e}", data={})
                raise EngineError(f"insufficient market data for decision: {e}") from e
            self.rec.write_intent(day, intent)   # written BEFORE any order is placed
            log.info("decision: %s", self.decision_text(intent))
        out["plan"] = intent
        out["actions"] = self.execute_intent(day, intent, allow_entry=True, window_open=True)
        self.update_shadow()
        return out

    def _entry_resolved(self, day: str) -> bool:
        return any(e["step"] == "entry" and e["status"] in ("done", "failed", "missed", "blocked")
                   for e in self.rec.intent_events(day))

    def cmd_manage(self) -> dict[str, Any]:
        now = self.now()
        day = utc_day(now).isoformat()
        rr = self.reconcile()
        self.log_market_data()
        actions = list(rr.actions)
        intent = self.rec.intent(day)
        if intent is not None:
            actions += self.execute_intent(day, intent, allow_entry=False,
                                           window_open=self._in_window(now))
        inst = rr.instrument
        acct = self.ex.get_account()
        pos = acct.position(inst.id)
        orders = self.ex.get_open_orders(inst.id)
        mark = self.ex.get_ticker(inst.id).mark
        sl = next((o.trigger_price for o in orders if o.tpsl_kind == "sl" and o.status in ACTIVE_TRIGGER_STATUSES), None)
        tp = next((o.trigger_price for o in orders if o.tpsl_kind == "tp" and o.status in ACTIVE_TRIGGER_STATUSES), None)
        data = {"state": self.rec.display_state(), "mark": mark,
                "position": pos.__dict__ if pos else None, "unrealized_pnl": pos.unrealized_pnl if pos else 0.0,
                "sl": sl, "tp": tp, "dist_to_sl": (abs(mark - sl) if (pos and sl) else None),
                "dist_to_tp": (abs(tp - mark) if (pos and tp) else None),
                "liquidation_price": pos.liquidation_price if pos else None, "actions": actions,
                "equity": rr.equity}
        self.store.insert("manage_log", state=data["state"], data=data)
        self.update_shadow()
        return data

    def _in_window(self, now: datetime) -> bool:
        ws, we = entry_window(now, self.cfg.schedule.entry_window_start_hkt, self.cfg.schedule.entry_window_end_hkt)
        return ws <= now <= we

    def cmd_pause(self, source: str = "cli") -> str:
        self.rec.set_state(add_reason="manual_pause", note=f"pause ({source})")
        self.alert("paused", f"new entries stopped ({source}); position and SL/TP kept")
        return "paused"

    def cmd_kill(self, source: str = "cli") -> str:
        self.rec.set_state(add_reason="manual_kill", note=f"kill ({source})")
        self._kill_close_tried = True
        closed, clean = self.close_position(reason="manual_kill")
        msg = "position closed" if closed else "CLOSE FAILED"
        self.alert("killed", f"kill ({source}): {msg}{'' if clean else ', leftover orders remain'}; bot paused")
        return msg

    def cmd_resume(self) -> str:
        acct = self.ex.get_account()
        eq = self.equity(acct)
        if not eq["valid"]:
            raise EngineError("cannot resume: equity is unreadable")
        st = self.rec.state()
        prev = self.store.latest("equity_log")
        net_funded = (prev["data"] or {}).get("net_funded") if prev and isinstance(prev["data"], dict) else None
        keep_floor = False
        floor_reset = False
        if "equity_floor" in st["pause_reasons"]:
            trig = self.store.latest("alerts", "kind = ?", ["KILL SWITCH: equity floor"])
            if trig is None or trig["config_version"] == self.cfg.config_version:
                keep_floor = True
            else:
                floor_reset = True
                net_funded = eq["equity"]        # new config version accepted the loss: new funded baseline
        self.rec.set_state(clear_reasons=True, note="resume (user confirmed): pause, kill switches cleared; peak reset")
        if keep_floor:
            self.rec.set_state(add_reason="equity_floor", note="equity floor stays: needs a new config version")
        self.store.insert("equity_log", equity=eq["equity"], wallet=eq["wallet"], upnl=eq["upnl"], peak=eq["equity"],
                          drawdown_pct=0.0, data={**eq, "peak_reset": True, "net_funded": net_funded,
                                                  "floor_reset": floor_reset})
        msg = f"pause cleared; drawdown peak reset to {eq['equity']:.2f}; losing-streak count restarted"
        if keep_floor:
            msg += ". EQUITY FLOOR STOP REMAINS: it can only be cleared by a new config version"
        self.alert("resumed" if not keep_floor else "resume: equity floor still active", msg)
        return "resumed" if not keep_floor else "equity floor still active"

    # ================================================================ text
    def decision_text(self, p: dict[str, Any]) -> str:
        d = {1: "LONG", -1: "SHORT", 0: "none"}
        s = (f"{p['utc_day']} score {p['score']:+.2f} ({d[p['direction']]}), tier {p['tier_fraction']:.2f}, "
             f"action {p['action']}")
        if p.get("close_reason"):
            s += f", close ({p['close_reason']})"
        if p.get("enter_direction"):
            s += f", enter {d[p['enter_direction']]} at {p['enter_fraction']:.2f} of risk budget"
        if p.get("gates_triggered"):
            s += f", gates: {', '.join(p['gates_triggered'])}"
        if p.get("entry_blocked"):
            s += f", blocked: {'; '.join(p['entry_blocked'])}"
        if p.get("notes"):
            s += f" ({'; '.join(p['notes'])})"
        return s

    def status_text(self) -> str:
        st = self.rec.state()
        lines = [f"state: {self.rec.display_state()}", f"pause reasons: {', '.join(st['pause_reasons']) or '-'}"]
        try:
            inst = self.instrument()
            acct = self.ex.get_account()
            pos = acct.position(inst.id)
            eq = self.equity(acct)
            lines.append(f"equity: {eq['equity']:.2f} (wallet {eq['wallet']:.2f}, uPnL {eq['upnl']:.2f}, source {eq['source']})")
            if pos:
                orders = self.ex.get_open_orders(inst.id)
                sl = [o.trigger_price for o in orders if o.tpsl_kind == "sl" and o.status in ACTIVE_TRIGGER_STATUSES]
                tp = [o.trigger_price for o in orders if o.tpsl_kind == "tp" and o.status in ACTIVE_TRIGGER_STATUSES]
                lines.append(f"position: {pos.size} {inst.symbol} @ {pos.entry_price} | uPnL {pos.unrealized_pnl:.2f} | "
                             f"liq {pos.liquidation_price} | SL {sl or 'MISSING'} | TP {tp or '-'} | "
                             f"cum funding {pos.cumulative_funding}")
            else:
                lines.append("position: flat")
        except Exception as e:  # noqa: BLE001
            lines.append(f"exchange unavailable: {e}")
        last_eq = self.store.latest("equity_log")
        if last_eq:
            k = (last_eq["data"] or {}).get("kill", {})
            lines.append(f"peak {last_eq['peak']:.2f}, drawdown {last_eq['drawdown_pct']:.2f}% "
                         f"(kill at {self.cfg.risk.kill_drawdown_pct}%), losing streak "
                         f"{k.get('losing_streak_pct', 0) or 0:.2f}% (kill at {self.cfg.risk.kill_losing_streak_pct}%)")
        dec = self.store.latest("decisions")
        if dec:
            lines.append(f"last decision {dec['utc_day']}: {dec['action']} score {dec['score']} - {dec['reason']}")
        return "\n".join(lines)

    # ================================================================ shadow
    def update_shadow(self) -> None:
        if not bool(self.cfg.shadow.enabled):
            return
        try:
            from perpbot.shadow import update_shadow

            update_shadow(self)
        except Exception as e:  # noqa: BLE001
            log.warning("shadow update failed: %s", e)
