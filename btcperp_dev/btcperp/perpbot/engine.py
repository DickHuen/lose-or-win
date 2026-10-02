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
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Sequence

from perpbot import analysis
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
    mark_vs_book,
    parse_server_time_ms,
)
from perpbot.indicators import Candle
from perpbot.records import Records, client_order_id
from perpbot.risk import (
    DrawdownState,
    bold_plan,
    compute_size,
    drawdown,
    estimate_liquidation,
    liquidation_ok,
    losing_streak,
    quantize_price,
    quantize_qty,
    risk_pct_for_trade,
    size_weighted_expectancy,
    trade_leverage,
    validate_order,
    validate_price,
)
from perpbot.storage import Store
from perpbot.strategy import (
    InsufficientData,
    bracket_prices,
    compute_score,
    H4_MS,
    HOUR_MS,
    ROLLING_PERIOD_MS,
    base_bar_ms,
    day_features,
    hold_for_bold,
    key_ms,
    live_rolling_features,
    period_key,
    period_start,
    plan_for,
    restrict_to_close,
    sc_day,
    shifted_daily,
)
from perpbot.telegram import Telegram, parse_command
from perpbot.timeutil import (
    DAY_MS,
    HOUR_MS,
    MINUTE_MS,
    Clock,
    day_start_ms,
    entry_window,
    fmt_hkt,
    fmt_utc,
    from_ms,
    hkt_at,
    to_ms,
    utc_day,
)

log = logging.getLogger("perpbot.engine")


KILL_SWITCH_REASONS = ("kill_drawdown", "kill_losing_streak")
REASON_HELP = {
    "manual_pause": "you paused new entries (Unpause.bat removes it)",
    "manual_kill": "you closed the position with Kill (Resume.bat clears it)",
    "kill_drawdown": "drawdown kill switch (Resume.bat + RESET-PEAK)",
    "kill_losing_streak": "losing-streak kill switch (Resume.bat + RESET-PEAK)",
    "equity_floor": "equity floor hard stop (needs a new config version with the new baseline)",
    "adopted_over_budget": "an adopted position was over the risk budget (Resume.bat clears it)",
    "permanent_floor": "permanent floor on all capital ever funded (only a new config version with "
                       "risk.permanent_floor_reset_for = the trigger date can restart it)",
    "live_review": "live expectancy below the backtest's review line (owner review with Claude, then Resume.bat)",
}
# pause reasons that keep the heartbeat failing while active (review v1.3.0 V4)
HARD_STOP_REASONS = ("kill_drawdown", "kill_losing_streak", "equity_floor", "permanent_floor", "live_review")
CRITICAL_ALERT_KINDS = frozenset({
    "ERROR", "SL re-place FAILED", "CLOSE FAILURE", "KILL SWITCH: drawdown", "KILL SWITCH: losing streak",
    "KILL SWITCH: equity floor", "KILL SWITCH: permanent floor", "kill close retry", "calendar expired", "clock skew",
    "late decision failed", "equity unreadable", "liquidation check", "wallet mismatch",
    "adopted position over risk budget", "deposit/withdrawal while holding", "live review", "proxy key expiry"})


def describe_reasons(reasons: list[str]) -> str:
    return "; ".join(REASON_HELP.get(r, r) for r in reasons)


class NeedsConfirmation(Exception):
    """A command needs an extra typed confirmation (exit code 6)."""

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
                 telegram: Telegram, clock: Clock, secrets: Any, sleep: Callable[[float], None] = time.sleep,
                 notifier: Callable[[str, str], Any] | None = None, defer_notifications: bool = False) -> None:
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
        self.notifier = notifier
        self.defer_notifications = defer_notifications      # review v1.2.0 item 6: the CLI sends after the run
        self.outbox: list[tuple[str, str, str]] = []
        self._inst: Instrument | None = None
        self._kill_close_tried = False

    # ================================================================ utilities
    def now(self) -> datetime:
        return self.clock.now()

    def today(self) -> str:
        return utc_day(self.now()).isoformat()

    # ---------------------------------------------------------------- decision periods (v1.5.0)
    @property
    def rolling(self) -> bool:
        """strategy.cadence rolling_4h / rolling_1h (v1.9.0): one decision per 4-hour / 1-hour period instead of
        per UTC day."""
        return str(self.cfg.strategy.cadence) in ROLLING_PERIOD_MS

    @property
    def period_ms(self) -> int:
        return ROLLING_PERIOD_MS[str(self.cfg.strategy.cadence)] if self.rolling else DAY_MS

    def period(self, now: datetime | None = None) -> tuple[int, str, date]:
        """The decision period containing `now`: (start T in UTC ms, key, UTC day of T). Daily: T = 00:00 UTC and
        key 'YYYY-MM-DD'; rolling_4h: T = the 4h boundary and key 'YYYY-MM-DDTHH:00'."""
        now = now or self.now()
        if self.rolling:
            t = period_start(to_ms(now), self.period_ms)
            return t, period_key(t), sc_day(t)
        d = utc_day(now)
        return day_start_ms(d), d.isoformat(), d

    def decide_window(self, per: tuple[int, str, date], now: datetime) -> tuple[datetime, datetime]:
        """Entry window for `decide`. Daily: 08:30-09:30 HKT of now's HKT date (v1.4 behaviour);
        rolling_4h: T + period_entry_start_minutes .. T + period_entry_end_minutes."""
        sc = self.cfg.schedule
        if self.rolling:
            return (from_ms(per[0] + int(sc.period_entry_start_minutes) * MINUTE_MS),
                    from_ms(per[0] + int(sc.period_entry_end_minutes) * MINUTE_MS))
        return entry_window(now, sc.entry_window_start_hkt, sc.entry_window_end_hkt)

    def window_bounds(self, per: tuple[int, str, date]) -> tuple[datetime, datetime]:
        """Entry window of the period itself (late decisions: events at its start; manage: late after its end)."""
        sc = self.cfg.schedule
        if self.rolling:
            return (from_ms(per[0] + int(sc.period_entry_start_minutes) * MINUTE_MS),
                    from_ms(per[0] + int(sc.period_entry_end_minutes) * MINUTE_MS))
        return hkt_at(per[2], sc.entry_window_start_hkt), hkt_at(per[2], sc.entry_window_end_hkt)

    def alert(self, kind: str, text: str, dedupe_key: str | None = None) -> None:
        """Store an alert for the owner (shown on the dashboard); the Windows notification (and Telegram,
        only if enabled in config) is sent after the run when `defer_notifications` is set, so a slow
        notification can never delay trading actions such as re-placing a stop-loss."""
        if dedupe_key and self.rec.alert_sent(dedupe_key):
            return
        msg = f"[btcperp] {kind}: {text}\n({fmt_hkt(self.now())})"
        self.store.insert("alerts", kind=kind, dedupe_key=dedupe_key, sent=0, text=msg[:4000])
        log.warning("ALERT %s: %s", kind, text)
        if self.defer_notifications:
            self.outbox.append((kind, text, msg))
        else:
            self._deliver(kind, text, msg)

    def notify(self, kind: str, text: str) -> None:
        """A desktop notification only (no alert row, no Telegram): e.g. the 4-hourly analysis."""
        if self.defer_notifications:
            self.outbox.append((kind, text, ""))
        else:
            self._deliver(kind, text, "")

    def _deliver(self, kind: str, text: str, msg: str, *, desktop: bool = True) -> None:
        try:
            if msg:                                           # "" = notify(): desktop only
                self.tg.send(msg)
        except Exception:  # noqa: BLE001
            log.warning("telegram send failed", exc_info=True)
        if desktop and self.notifier is not None:
            try:
                self.notifier(kind, text)
            except Exception:  # noqa: BLE001
                log.warning("desktop notification failed", exc_info=True)

    MAX_TOASTS_PER_RUN = 5

    def flush_notifications(self) -> int:
        """Send queued notifications (called by the CLI after the run and after the lock is released).
        At most MAX_TOASTS_PER_RUN desktop pop-ups, then one summary; every alert is on the dashboard."""
        items, self.outbox = self.outbox, []
        for i, (kind, text, msg) in enumerate(items):
            self._deliver(kind, text, msg, desktop=i < self.MAX_TOASTS_PER_RUN)
        extra = len(items) - self.MAX_TOASTS_PER_RUN
        if extra > 0 and self.notifier is not None:
            try:
                self.notifier("more alerts", f"{extra} more alerts - see the dashboard")
            except Exception:  # noqa: BLE001
                log.warning("desktop notification failed", exc_info=True)
        return len(items)

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
        if chosen.max_leverage < self.cfg.risk.leverage and not self.per_trade_leverage:
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
        """An entry already happened in this decision period (`day` = period key: a UTC day, or a 4h period)."""
        start = key_ms(day) if "T" in day else day_start_ms(datetime.fromisoformat(day).date())
        smoke = self.smoketest_coids()
        if any(f.is_opening and f.client_order_id not in smoke for f in self.stored_fills(start)):
            return True
        for r in self.store.query("SELECT data FROM trades WHERE event='open' AND ts_ms >= ?", [start]):
            d = r["data"]
            same = d.get("entry_period") == day or ("T" not in day and d.get("entry_utc_day") == day)
            if same and d.get("live", True):
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
                found = self.orders_by_coid(self.coid(f"entry:{a}", day))
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
            "entry_utc_day": entry_day, "entry_period": self.period(from_ms(entry_ts))[1], "sl_price": sl, "tp_price": tp,
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
        if bool(self.cfg.bold.enabled):    # v1.7.0: a bet risks max_loss_fraction of the equity before it (uPnL excluded)
            base = (eq - float(pos.unrealized_pnl or 0.0)) if eq else 0.0
            budget = base * float(self.cfg.bold.max_loss_fraction)
            budget_txt = f"{float(self.cfg.bold.max_loss_fraction) * 100:.0f}% of equity, bold mode"
        elif self.cfg.risk.notional_multiple_full_tier:   # v1.8.0: the full-tier position's loss at this stop
            mult = float(self.cfg.risk.notional_multiple_full_tier)
            base = (eq - float(pos.unrealized_pnl or 0.0)) if eq else 0.0
            budget = base * mult * sl_dist / pos.entry_price if pos.entry_price else 0.0
            budget_txt = f"position x{mult:g} equity at this stop"
        else:
            budget = eq * float(self.cfg.risk.risk_per_trade_pct) / 100.0 if eq else 0.0
            budget_txt = f"{self.cfg.risk.risk_per_trade_pct}% of equity"
        if budget and trade["initial_risk_usd"] > budget * 1.01:
            self.rec.set_state(add_reason="adopted_over_budget", note="adopted position risk above budget")
            self.alert("adopted position over risk budget",
                       f"risk at SL {trade['initial_risk_usd']:.2f} > budget {budget:.2f} "
                       f"({budget_txt}). Bot paused; SL/TP kept. Resume only after you confirm.")
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
                self.tg.send("Commands: /pause /kill /status. Resume only on the bot computer (Resume.bat).")
        return actions

    # ================================================================ kill switches
    def check_permanent_floor_config(self, prev_data: dict[str, Any]) -> float:
        """Review v1.3.0 F1: a new config may never lower the permanent floor. v1.7.0: except once, by the owner's
        explicit decision, in the config version named in risk.permanent_floor_lowered_in; the lower value then
        becomes the new maximum, so a later config cannot lower it again without naming its own version."""
        pct = float(self.cfg.risk.permanent_floor_pct_of_cumulative_funded)
        seen = prev_data.get("permanent_floor_pct_max")
        if seen is not None and pct < float(seen):
            if (self.cfg.risk.permanent_floor_lowered_in or "") == self.cfg.config_version:
                self.alert("permanent floor lowered", f"config {self.cfg.config_version} lowers the permanent floor from "
                           f"{seen}% to {pct}% (owner decision, risk.permanent_floor_lowered_in)",
                           dedupe_key=f"perm_floor_lowered_ok:{self.cfg.config_version}")
                return pct
            self.alert("KILL SWITCH: permanent floor", f"config {self.cfg.config_version} lowers the permanent floor from "
                       f"{seen}% to {pct}%: refused, the bot does not trade with this config",
                       dedupe_key=f"perm_floor_lowered:{self.cfg.config_version}")
            raise EngineError(f"config lowers risk.permanent_floor_pct_of_cumulative_funded from {seen} to {pct}")
        return max(pct, float(seen)) if seen is not None else pct

    def kill_switch_check(self, acct: AccountSnapshot, *, allow_actions: bool) -> dict[str, Any]:
        inst = self.instrument()
        eq = self.equity(acct)
        prev = self.store.latest("equity_log")
        prev_data = prev["data"] if prev and isinstance(prev["data"], dict) else {}
        perm_pct_max = self.check_permanent_floor_config(prev_data)
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
        cf_prev = prev_data.get("cum_funded")
        cum_funded = (float(cf_prev) + flow_adj) if cf_prev is not None else None     # never re-based (F1)
        evaluate = bool(eq["valid"]) and not pending          # review C8 / C9
        limit = float(self.cfg.risk.kill_drawdown_pct)
        if evaluate:
            dd = drawdown(eq["equity"], prev_peak, limit)
            if net_funded is None:
                net_funded = eq["equity"]                        # first valid equity = funded capital baseline
            if cum_funded is None:
                cum_funded = net_funded
            floor = net_funded * float(self.cfg.risk.equity_floor_pct_of_net_funded) / 100.0
            floor_hit = eq["equity"] < floor
            perm_floor = cum_funded * float(self.cfg.risk.permanent_floor_pct_of_cumulative_funded) / 100.0
            perm_hit = eq["equity"] < perm_floor
        else:
            perm_floor, perm_hit = None, False
            peak = prev_peak if prev_peak is not None else 0.0
            dd = DrawdownState(eq["equity"] or 0.0, peak, float(prev["drawdown_pct"] or 0.0) if prev else 0.0, False)
            floor, floor_hit = None, False
        st = self.rec.state()
        streak_trades = self.rec.closed_trades(since_ms=self.rec.last_resume_ms())
        streak = losing_streak(streak_trades, float(self.cfg.risk.kill_losing_streak_pct),
                               float(self.cfg.risk.losing_streak_tie_pct))
        all_trades = self.rec.closed_trades()
        exp, n_exp = size_weighted_expectancy(all_trades, int(self.cfg.risk.expectancy_window_trades))
        # review v1.3.0 S9: live review line from the backtest (rolling expectancy below its 5th percentile)
        rv_floor = self.cfg.risk.live_review_expectancy_floor_r
        rv_n = int(self.cfg.risk.live_review_window_trades)
        rv_checked = int(prev_data.get("live_review_checked_n") or 0)
        rv_exp, _ = size_weighted_expectancy(all_trades, rv_n)
        rv_hit = (rv_floor is not None and len(all_trades) >= int(self.cfg.risk.live_review_min_trades)
                  and len(all_trades) > rv_checked and rv_exp is not None and rv_exp < float(rv_floor))
        status = {"evaluated": evaluate, "drawdown_pct": dd.drawdown_pct, "drawdown_triggered": dd.triggered,
                  "equity_floor": floor, "equity_floor_hit": floor_hit, "net_funded": net_funded,
                  "losing_streak_trades": streak.losing_trades, "losing_streak_pct": streak.loss_pct,
                  "losing_streak_triggered": streak.triggered, "expectancy_r": exp, "expectancy_n": n_exp,
                  "pending_flows": len(pending), "paused": st["paused"], "pause_reasons": st["pause_reasons"],
                  "cum_funded": cum_funded, "permanent_floor": perm_floor, "permanent_floor_hit": perm_hit,
                  "live_review_expectancy_r": rv_exp, "live_review_floor_r": rv_floor}
        self.store.insert("equity_log", equity=eq["equity"], wallet=eq["wallet"], upnl=eq["upnl"],
                          peak=dd.peak if (evaluate or prev_peak is not None) else None, drawdown_pct=dd.drawdown_pct,
                          data={**eq, "flow_adjustment": flow_adj, "net_funded": net_funded, "kill": status,
                                "cum_funded": cum_funded, "permanent_floor_pct_max": perm_pct_max,
                                "live_review_checked_n": len(all_trades)})
        needs_close = False
        if perm_hit and "permanent_floor" not in st["pause_reasons"]:
            self.rec.set_state(add_reason="permanent_floor", note="permanent floor hard stop")
            self.alert("KILL SWITCH: permanent floor",
                       f"equity {eq['equity']:.2f} is below {self.cfg.risk.permanent_floor_pct_of_cumulative_funded}% of all "
                       f"capital ever funded ({cum_funded:.2f}; cumulative result {eq['equity'] - cum_funded:+.2f}). Closing "
                       f"and stopping for good: only you can restart it, in a new config version that sets "
                       f"risk.permanent_floor_reset_for to today's date.")
            needs_close = True
        if rv_hit and "live_review" not in st["pause_reasons"]:
            self.rec.set_state(add_reason="live_review", note="live review line")
            self.alert("live review", f"rolling {rv_n}-trade expectancy {rv_exp:.3f} R is below the backtest's 5th "
                       f"percentile {rv_floor} R after {len(all_trades)} live trades: new entries paused; position and "
                       f"SL/TP kept. Review with Claude before Resume.bat.")
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
        closing = [r for r in ("permanent_floor", "equity_floor", "kill_drawdown", "manual_kill") if r in reasons_now]
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
                       f"({days_left:.1f} days). Create a new proxy key and put it in .env (START_HERE.md).",
                       dedupe_key=f"key_expiry:{self.today()}")

    # ================================================================ orders
    def orders_by_coid(self, coid: str) -> list[Order]:
        """Non-trigger orders for a client order id. v1.5.5: live, the exchange's client-order-id lookup found
        nothing for an accepted order, so fall back to the order ids known locally: the one the placement returned
        (`orders` log) and the one on any fill carrying this client order id (a rejected bracket can still fill)."""
        found = [o for o in self.ex.get_orders(client_order_id=coid) if not o.is_trigger]
        if found:
            return found
        ids = sorted({int(r["order_id"]) for r in self.store.query(
            "SELECT order_id FROM orders WHERE client_order_id=? AND order_id IS NOT NULL "
            "UNION SELECT order_id FROM fills WHERE client_order_id=? AND order_id IS NOT NULL", (coid, coid))})
        return [o for oid in ids for o in self.ex.get_orders(order_id=oid) if not o.is_trigger]

    @staticmethod
    def _order_final(o: Order) -> bool:
        return (o.status in FILLED_STATUSES or o.status in NOT_FILLED_TERMINAL
                or (o.status == "partial" and o.tif in ("ioc", "fok")))

    def confirm_order(self, coid: str, res: PlaceResult, purpose: str) -> Order | None:
        """The order's final status (filled / not filled). v1.5.4 live: a FOK that did not fill was accepted, but
        GET /v1/account/orders?client_order_id= returned nothing. So: 1) the exchange's own order update received
        with the placement; 2) poll by order id (proven live: found a cancelled order); 3) by client order id."""
        if not res.accepted and not res.outcome_unknown:
            return None
        placed = res.order
        if placed is not None and self._order_final(placed):
            self.log_order(purpose, "status", coid, placed.id, placed.status,
                           {"filled_quantity": placed.filled_quantity, "check": 0, "source": "placement update"})
            return placed
        oid = res.order_id or (placed.id if placed is not None else None)
        last: Order | None = None
        attempts = int(self.cfg.polymarket.order_status_poll_attempts)
        for i in range(attempts):
            found: list[Order] = []
            for query in ([{"order_id": oid}] if oid else []) + [{"client_order_id": coid}]:
                try:
                    found = [o for o in self.ex.get_orders(**query) if not o.is_trigger]
                except ExchangeError as e:
                    log.warning("order status read failed (%s %s): %s", coid, query, e)
                    found = []
                if found:
                    break
            if found:
                last = found[0]
                self.log_order(purpose, "status", coid, last.id, last.status,
                               {"filled_quantity": last.filled_quantity, "check": i + 1})
                if self._order_final(last):
                    return last
            self.sleep(float(self.cfg.polymarket.order_status_poll_interval_seconds))
        if last is not None and last.status in ACTIVE_ORDER_STATUSES and last.tif in ("ioc", "fok"):
            # an IOC/FOK order should never rest; cancel it by id to be safe
            self.ex.cancel_orders([last.id])
            self.log_order(purpose, "cancel", coid, last.id, "cancel_requested", {"reason": "non-terminal after polling"})
        return last

    @property
    def per_trade_leverage(self) -> bool:
        """v1.9.0: sizing by position (risk.notional_multiple_full_tier) picks the leverage for each trade;
        risk.leverage is then the maximum. Risk-% sizing and bold mode use risk.leverage itself."""
        return bool(self.cfg.risk.notional_multiple_full_tier) and not bool(self.cfg.bold.enabled)

    def plan_trade_leverage(self, plan: dict[str, Any], inst: Instrument, equity: float) -> tuple[int, float, str | None]:
        """(leverage, tier fraction to size with, note) for a position-sized entry (risk.trade_leverage)."""
        r = self.cfg.risk
        mult = float(r.notional_multiple_full_tier)
        want = mult * float(plan.get("enter_fraction") or 0.0)
        mark = float(plan.get("mark") or 0.0)
        # +1%: the entry is priced off the book (a short's bid is a little below the mark), so the pre-trade
        # liquidation check at the entry price still passes at a boundary leverage
        sl_pct = 1.01 * float(self.cfg.exits.sl_atr_multiple) * float(plan.get("atr") or 0.0) / mark if mark > 0 else 0.0
        lev, got, note = trade_leverage(multiple=want, sl_pct=sl_pct, max_leverage=int(r.leverage),
                                        margin_use_pct=float(r.max_margin_use_pct),
                                        liq_multiple=float(r.liq_min_sl_multiple), inst=inst, notional=equity * want,
                                        mmr_divisor=float(r.liq_estimate_mmr_divisor))
        return lev, got / mult, note

    def ensure_leverage(self, inst: Instrument, leverage: int | None = None) -> tuple[bool, str]:
        lev = int(self.cfg.risk.leverage) if leverage is None else int(leverage)
        cross = bool(self.cfg.risk.cross_margin)
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
        per_trade = self.per_trade_leverage
        if not per_trade:
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
        lev, fraction = int(self.cfg.risk.leverage), float(plan["enter_fraction"])
        if per_trade:                                   # v1.9.0: the lowest leverage the position needs
            lev, fraction, note = self.plan_trade_leverage(plan, inst, eq["equity"])
            plan["trade_leverage"], plan["position_note"] = lev, note
            if lev < 1:
                self.rec.intent_event(day, "entry", "failed", {"reason": note})
                self.alert("entry rejected", f"no position possible: {note}")
                return False
            ok, why = self.ensure_leverage(inst, lev)
            if not ok:
                self.rec.intent_event(day, "entry", "failed", {"reason": why})
                self.alert("entry blocked", f"account config is not {lev}x isolated and could not be set: {why}. No trade.")
                return False
            if note:
                self.rec.intent_event(day, "entry", "position_cut", {"note": note, "leverage": lev})
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
            bold = bool(self.cfg.bold.enabled)
            if bold:                                    # v1.7.0: one all-in bet, SL/TP from the bet's own rules
                b = self.cfg.bold
                size, sl_p, tp_p, liq_est = bold_plan(
                    equity=eq["equity"], price=ref, direction=d, notional_multiple=float(b.notional_multiple),
                    leverage=int(self.cfg.risk.leverage), target_multiple=float(b.target_multiple),
                    max_loss_fraction=float(b.max_loss_fraction), fee_rate=float(self.cfg.shadow.fee_rate_estimate),
                    liq_buffer_pct=float(b.liq_buffer_pct), inst=inst,
                    mmr_divisor=float(self.cfg.risk.liq_estimate_mmr_divisor))
            else:
                size = compute_size(equity=eq["equity"], risk_pct=risk_pct, fraction=fraction,
                                    price=ref, atr=atr, sl_atr_multiple=float(self.cfg.exits.sl_atr_multiple),
                                    notional_cap_pct=float(self.cfg.risk.notional_cap_pct_equity),
                                    leverage=lev, inst=inst,
                                    raise_to_min=bool(self.cfg.risk.raise_to_min_notional),
                                    notional_multiple=self.cfg.risk.notional_multiple_full_tier)
            if not size.ok:
                self.rec.intent_event(day, "entry", "failed", {"reason": size.reject_reason, "size": size.to_dict()})
                self.alert("entry rejected", f"order violates limits: {size.reject_reason}")
                return False
            if not bold:
                sl_p, tp_p = bracket_prices(d, ref, atr, float(self.cfg.exits.sl_atr_multiple),
                                            float(self.cfg.exits.tp_atr_multiple))
            sl_q = quantize_price(sl_p, inst.price_decimals, "nearest")
            tp_q = quantize_price(tp_p, inst.price_decimals, "nearest")
            try:
                validate_price(inst, limit)
                validate_price(inst, sl_q)
                validate_price(inst, tp_q)
                validate_order(inst, qty=size.qty, price=float(limit), leverage=lev, market=False)
            except Exception as e:  # noqa: BLE001
                self.rec.intent_event(day, "entry", "failed", {"reason": str(e)})
                self.alert("entry rejected", f"order violates instrument rules: {e}")
                return False
            if not bold:
                liq_est = estimate_liquidation(ref, d, lev, inst, size.notional,
                                               float(self.cfg.risk.liq_estimate_mmr_divisor))
            if not bold and not liquidation_ok(ref, liq_est, size.sl_distance, float(self.cfg.risk.liq_min_sl_multiple)):
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
                   "risk_pct_budget": risk_pct, "ramp": ramp, "equity": eq, "attempt": attempt, "liq_estimate": liq_est,
                   "leverage": lev}
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
            found = self.orders_by_coid(coid)
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
            "entry_utc_day": day[:10], "entry_period": day, "sl_price": sl_price, "tp_price": tp_price,
            "sl_order_id": sl_id, "tp_order_id": tp_id,
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
        bold = bool(self.cfg.bold.enabled)
        mult = self.cfg.risk.notional_multiple_full_tier
        t_lev, t_note = plan.get("trade_leverage"), plan.get("position_note")
        risk_txt = (f"BOLD all-in: {float(self.cfg.bold.max_loss_fraction) * 100:.0f}% of equity at the SL, "
                    f"x{float(self.cfg.bold.target_multiple):g} at the TP" if bold else
                    f"risk {trade['initial_risk_usd']:.2f} ({size.risk_pct_used:.3f}% equity)"
                    f"{f', position x{size.effective_leverage:.2f} equity' if mult else ''}"
                    f"{f' at {t_lev}x' if t_lev else ''}{f' ({t_note})' if t_note else ''}"
                    f"{' [ramp]' if ramp and not mult else ''}")
        self.alert("open", f"{'LONG' if d > 0 else 'SHORT'} {qty} {inst.symbol} @ {entry_price:.2f} | SL {sl_s} TP {tp_s} | "
                   f"{risk_txt} | score {plan.get('score')}")
        liq_mult = float(self.cfg.risk.liq_min_sl_multiple)
        if self.per_trade_leverage and self.cfg.risk.liq_after_fill_sl_multiple is not None:
            liq_mult = float(self.cfg.risk.liq_after_fill_sl_multiple)    # v1.9.0: the plan used liq_min_sl_multiple
        if bold and sl_dist > 0:      # v1.7.0: the exchange's liquidation must lie beyond the SL by liq_buffer_pct
            liq_mult = (sl_dist + entry_price * float(self.cfg.bold.liq_buffer_pct) / 100.0) / sl_dist
        if pos is not None and not liquidation_ok(entry_price, pos.liquidation_price, sl_dist, liq_mult,
                                                  isolated=not pos.cross):
            self.alert("liquidation check", f"liquidation price {pos.liquidation_price!r} is missing or closer than "
                       f"{liq_mult:.2f}x SL distance {sl_dist:.2f}; closing")
            self.close_position(reason="liq_check")
            return False
        if pos is not None:
            self.ensure_protection(inst, self.rec.open_trade() or trade, pos, orders)
        return True

    # ================================================================ decision inputs
    def gather_inputs(self, per: tuple[int, str, date]) -> dict[str, Any]:
        cfg = self.cfg
        now_ms = to_ms(self.now())
        inst = self.instrument()
        day_d = per[2]
        h1: list[Candle] = []
        if self.rolling:
            # daily candles ending at T are built from 4h candles: 1,000 days plus the 3-day-rule history
            days = int(cfg.binance.daily_candles_to_load) + int(cfg.strategy.opposite_days_rule) + 2
            if base_bar_ms(str(cfg.strategy.cadence)) == HOUR_MS:     # v1.9.0 rolling_1h: from 1h candles (~25 pages)
                h1 = self.bn.klines_range("1h", per[0] - days * DAY_MS, now_ms)
                h4 = self.bn.klines("4h", int(cfg.binance.h4_candles_to_load), now_ms)
            else:
                h4 = self.bn.klines_range("4h", per[0] - days * DAY_MS, now_ms)
            daily: list[Candle] = []
        else:
            daily = self.bn.klines("1d", int(cfg.binance.daily_candles_to_load), now_ms)
            h4 = self.bn.klines("4h", int(cfg.binance.h4_candles_to_load), now_ms)
        fstart = day_start_ms(day_d) - int(cfg.binance.funding_days_to_load) * DAY_MS
        funding = self.bn.funding(fstart, now_ms)
        self.store_binance(daily, h4, funding, h1)
        ticker = self.ex.get_ticker(inst.id)
        book = self.ex.get_book(inst.id, int(cfg.polymarket.book_depth))
        ok, dev = mark_vs_book(ticker.mark, book, inst.price_bounds)
        if not ok:                                   # v1.5.3: never size or place SL/TP from another market's price
            raise ExchangeError(f"mark {ticker.mark} does not match the {inst.symbol} order book "
                                f"({book.bids[:1]} / {book.asks[:1]}, deviation {dev}): exchange data inconsistent")
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
        return {"daily": daily, "h4": h4, "h1": h1, "funding": funding, "ticker": ticker, "book": book,
                "pm_funding": pm_funding, "geoblock": geo}

    def store_binance(self, daily: list[Candle], h4: list[Candle], funding: list[tuple[int, float, float]],
                      h1: Sequence[Candle] = ()) -> None:
        self.store.insert_many_ignore("bn_klines_1d", ({"open_ms": c.open_ms, "open": c.open, "high": c.high, "low": c.low,
                                                        "close": c.close, "volume": c.volume} for c in daily))
        self.store.insert_many_ignore("bn_klines_4h", ({"open_ms": c.open_ms, "open": c.open, "high": c.high, "low": c.low,
                                                        "close": c.close, "volume": c.volume} for c in h4))
        self.store.insert_many_ignore("bn_funding", ({"fund_ts_ms": ts, "rate": r, "mark": m} for ts, r, m in funding))
        if h1:
            self.store.insert_many_ignore("bn_klines_1h", ({"open_ms": c.open_ms, "open": c.open, "high": c.high,
                                                            "low": c.low, "close": c.close, "volume": c.volume} for c in h1))

    def decision_blocks(self, day_d: Any) -> list[str]:
        """Reasons that block NEW positions today but still allow closes (review v1.2.0 items 9 and 15)."""
        out: list[str] = []
        expired = self.calendar.expired_types(day_d)
        if expired:
            out.append(f"economic calendar coverage ended for {', '.join(expired)}: no new positions until an updated "
                       f"calendar.yaml is installed")
            self.alert("calendar expired", out[-1], dedupe_key=f"calendar_expired:{day_d.isoformat()}")
        skew, err = self.clock_skew_ms()
        limit_ms = float(self.cfg.schedule.max_clock_skew_seconds) * 1000.0
        if skew is None:
            out.append(f"exchange server time unreadable ({err}): clock not verified, no new positions")
        elif abs(skew) > limit_ms:
            out.append(f"this computer's clock differs from the exchange by {skew / 1000:+.1f}s "
                       f"(limit {self.cfg.schedule.max_clock_skew_seconds}s): no new positions")
            self.alert("clock skew", out[-1] + ". Sync the Windows clock (Settings > Time > Sync now).",
                       dedupe_key=f"clock_skew:{day_d.isoformat()}")
        return out

    def clock_skew_ms(self) -> tuple[float | None, str]:
        err = ""
        for _ in range(2):
            try:
                server = parse_server_time_ms(self.ex.get_server_time_raw())
                if server is not None:
                    return float(server - to_ms(self.now())), ""
                err = "unrecognised response"
            except ExchangeError as e:
                err = str(e)
        return None, err

    def next_decision_hkt(self, per: tuple[int, str, date]) -> str:
        if self.rolling:
            return fmt_hkt(from_ms(per[0] + self.period_ms + int(self.cfg.schedule.period_entry_start_minutes) * MINUTE_MS))[:16]
        return fmt_hkt(hkt_at(per[2] + timedelta(days=1), self.cfg.schedule.entry_window_start_hkt))[:16]

    def make_decision(self, per: tuple[int, str, date], inputs: dict[str, Any], rr: ReconcileResult, *,
                      late: bool = False) -> dict[str, Any]:
        """The period's decision (daily: the UTC day; rolling_4h: the 4h period). `late=True` (decide or manage
        after the entry window, no intent yet): the same rules on the same data (closed at T, events at the window
        start), close part only."""
        cfg = self.cfg
        now = self.now()
        t_ms, day, day_d = per
        at = self.window_bounds(per)[0] if late else now
        active = self.calendar.active_windows(at, cfg.gates.event_anchor_hkt, float(cfg.gates.event_post_release_hours))
        ev_list = [{"type": e.type, "release_utc": fmt_utc(e.release_utc), "window_start_utc": fmt_utc(st),
                    "window_end_utc": fmt_utc(en), "note": e.note} for e, st, en in active]
        funding = [(ts, r) for ts, r, _ in inputs["funding"]]
        if self.rolling:
            f = live_rolling_features(cfg, t_ms, inputs["h4"], funding, ev_list, cadence=str(cfg.strategy.cadence),
                                      base=inputs.get("h1"))
        else:
            f = day_features(cfg, day_d, inputs["daily"], inputs["h4"], funding, ev_list)
        sc, fstat, dirs = f.score, f.funding, f.directions
        e_fast, e_slow, h4_open = f.h4_fast, f.h4_slow, f.h4_open_ms
        trade = rr.trade
        pos_dir = rr.position.direction if rr.position else 0
        entry_day = datetime.fromisoformat(trade["entry_utc_day"][:10]).date() if (trade and trade.get("entry_utc_day")) else day_d
        entry_key = (trade.get("entry_period") or trade.get("entry_utc_day")) if trade else day
        paused = self.paused_reason()
        geo = inputs.get("geoblock") or {}
        region_blocked = geo.get("blocked") is True
        entered = self.entered_today(day)
        blocks = [] if late else self.decision_blocks(day_d)
        plan, ctx = plan_for(f, cfg, position_dir=pos_dir, entry_day=entry_day, entered_today=entered,
                             paused_reason=paused, entry_block="; ".join(blocks) or None, entry_key=entry_key)
        streak = ctx.opposite_streak
        if region_blocked and plan.enter_direction:
            restrict_to_close(plan, f"region blocked by geoblock ({geo.get('country')}/{geo.get('region')})", pos_dir)
        if late:
            restrict_to_close(plan, "late decision after the entry window: close rules only, no late entry", pos_dir)
        if bool(cfg.bold.enabled) and bool(cfg.bold.hold_until_tp_sl):
            hold_for_bold(plan, pos_dir)                 # v1.7.0: an open bet ends only at its TP or SL
        if self.rolling:
            bar = base_bar_ms(str(cfg.strategy.cadence))
            src = inputs.get("h1") if bar == HOUR_MS else inputs["h4"]
            used = shifted_daily(sorted(src or [], key=lambda c: c.open_ms), t_ms, 3, bar_ms=bar)
        else:
            used = [c for c in inputs["daily"] if c.open_ms <= sc.candle_open_ms]
        g_regime, g_h4, g_fund, g_event = f.g_regime, f.g_h4, f.g_funding, f.g_event
        caps = f.caps
        ticker, book = inputs["ticker"], inputs["book"]
        gates_triggered = f.gates_triggered()
        pm_rates = [r for _, r in inputs["pm_funding"]]
        plan_d = plan.to_dict()
        plan_d.update({
            "utc_day": day, "score": sc.score, "direction": sc.direction, "abs_score": sc.abs_score,
            "tier_fraction": sc.tier_fraction, "caps": caps, "gates_triggered": gates_triggered, "atr": sc.atr,
            "mark": ticker.mark, "decision_ts_ms": to_ms(now), "position_dir_at_decision": pos_dir,
            "funding_percentile": fstat.percentile, "event_active": g_event.triggered, "late": late,
            "entry_blocks": blocks, "cadence": f.cadence, "period_utc": fmt_utc(from_ms(t_ms)),
        })
        if self.per_trade_leverage and plan.enter_direction:     # v1.9.0: shown in the analysis; re-planned at entry
            try:
                lev, frac, note = self.plan_trade_leverage(plan_d, self.instrument(),
                                                           float((rr.equity or {}).get("equity") or 0.0))
                plan_d["sizing"] = {"multiple": frac * float(cfg.risk.notional_multiple_full_tier), "leverage": lev,
                                    "note": note}
            except Exception:  # noqa: BLE001 - display only
                log.warning("sizing preview failed", exc_info=True)
        spread = book.best_ask - book.best_bid if (book.bids and book.asks) else None
        decision = {
            "utc_day": day, "decision_hkt": fmt_hkt(now), "decision_utc": fmt_utc(now),
            "score": sc.to_dict(),
            "inputs": {
                "daily_candles_used": [[c.open_ms, c.open, c.high, c.low, c.close] for c in used],
                "daily_candles_count": sc.candles_used, "cadence": f.cadence, "period_utc": fmt_utc(from_ms(t_ms)),
                "h4_candles_fetched": len(inputs["h4"]), "h1_candles_fetched": len(inputs.get("h1") or []),
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
                "direction_history": dirs, "score_history": f.scores, "opposite_streak": streak,
                "opposite_rule": ctx.opposite_days_rule, "flip_confirmed": ctx.flip_confirmed, "position_dir": pos_dir,
                "entry_day": entry_day.isoformat(), "entry_key": entry_key, "entered_today": entered, "geoblock": geo,
            },
            "size_tier": sc.tier_fraction,
            "gates": {g.name: {"triggered": g.triggered, "cap": g.cap, "blocks_entry": g.blocks_entry, "detail": g.detail}
                      for g in (g_regime, g_h4, g_fund, g_event)},
            "paused_reason": paused,
            "plan": plan_d,
        }
        reason = "; ".join(plan.notes + plan.entry_blocked) or plan.action
        try:                                                  # v1.5.0: readable analysis (dashboard + notification)
            ramp = self.rec.live_trades_opened() < int(cfg.risk.ramp_trades)
            decision["analysis"] = analysis.render(decision, cfg, equity=(rr.equity or {}).get("equity"), ramp=ramp,
                                                   next_hkt=self.next_decision_hkt(per), position=trade)
            quiet = bool(cfg.notifications.analysis_toast_only_actions) and plan.action in ("hold", "none", "paused")
            if bool(cfg.notifications.analysis_toast) and not late and not quiet:   # v1.9.0: hourly = quiet holds
                self.notify("分析", analysis.short_line(decision))
        except Exception:  # noqa: BLE001 - the analysis text must never stop a decision
            log.warning("analysis text failed", exc_info=True)
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
            orders = self.orders_by_coid(s["coid"])
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
            if not mark_vs_book(t.mark, b, inst.price_bounds)[0]:
                log.warning("market data skipped: mark %s does not match the order book", t.mark)
                return
            depth_bid = sum(q for _, q in b.bids)
            depth_ask = sum(q for _, q in b.asks)
            snap = {"mark": t.mark, "index": t.index, "last": t.last, "funding_rate": t.funding_rate,
                    "open_interest": t.open_interest, "best_bid": b.bids[0][0] if b.bids else None,
                    "best_ask": b.asks[0][0] if b.asks else None,
                    "spread": (b.asks[0][0] - b.bids[0][0]) if (b.bids and b.asks) else None,
                    "depth_bid_qty": depth_bid, "depth_ask_qty": depth_ask, "book": {"bids": b.bids, "asks": b.asks}}
            try:
                snap["binance_price"] = self.bn.price()
                if snap["binance_price"] and t.mark:        # review v1.3.0 R3: Polymarket mark vs Binance spot
                    snap["basis_bps"] = (t.mark / snap["binance_price"] - 1) * 1e4
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
        now = self.now()
        per = self.period(now)
        day = per[1]
        rr = self.reconcile()
        self.log_market_data()
        ws, we = self.decide_window(per, now)
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
                           f"(ended {fmt_hkt(we)}); no late entry", dedupe_key=f"missed_window:{day}")
            if intent is None and rr.position is not None:
                intent = self.late_decision(per, rr)            # review v1.2.0 item 2: close rules still apply
            if intent is not None:
                out["actions"] = self.execute_intent(day, intent, allow_entry=False, window_open=False)
            out["result"] = "missed entry window"
            return out
        if intent is None or (intent.get("action") == "paused" and not self.paused_reason()):
            inputs = self.gather_inputs(per)
            try:
                intent = self.make_decision(per, inputs, rr)
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

    def late_decision(self, per: tuple[int, str, date], rr: ReconcileResult) -> dict[str, Any]:
        """No decision was made in this period's entry window but a position is open: evaluate the period's close
        rules (reverse signal, 3-day rule, crowded funding) on the data closed at its start. Never enters."""
        day = per[1]
        inputs = self.gather_inputs(per)
        try:
            plan = self.make_decision(per, inputs, rr, late=True)
        except InsufficientData as e:
            self.store.insert("decisions", utc_day=day, score=None, direction=None, action="error",
                              reason=f"insufficient data (late decision): {e}", data={})
            raise EngineError(f"insufficient market data for the late decision: {e}") from e
        self.rec.write_intent(day, plan)
        self.alert("late decision", f"no decision in the entry window of {day}; evaluated its close rules late "
                   f"(no entry): {self.decision_text(plan)}", dedupe_key=f"late_decision:{day}")
        return plan

    def _entry_resolved(self, day: str) -> bool:
        return any(e["step"] == "entry" and e["status"] in ("done", "failed", "missed", "blocked")
                   for e in self.rec.intent_events(day))

    def cmd_manage(self) -> dict[str, Any]:
        now = self.now()
        per = self.period(now)
        day = per[1]
        rr = self.reconcile()
        self.log_market_data()
        actions = list(rr.actions)
        intent = self.rec.intent(day)
        if (intent is None and rr.position is not None
                and now > self.window_bounds(per)[1]):
            try:
                intent = self.late_decision(per, rr)
            except Exception as e:  # noqa: BLE001 - protection below must still run
                log.error("late decision failed: %s", e)
                self.alert("late decision failed", f"{e}; today's close rules were not evaluated, only the "
                           f"exchange SL/TP protect the position", dedupe_key=f"late_decision_failed:{day}")
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
        ws, we = self.decide_window(self.period(now), now)
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

    def cmd_unpause(self) -> str:
        """Remove only the manual pause (review v1.2.0 item 7). Kill switches and the equity floor stay."""
        st = self.rec.state()
        if "manual_pause" in st["pause_reasons"]:
            self.rec.set_state(remove_reasons=("manual_pause",), note="unpause: manual pause removed")
            msg = "manual pause removed"
        else:
            msg = "no manual pause was active"
        rest = self.rec.state()["pause_reasons"]
        if rest:
            msg += f"; STILL PAUSED by: {', '.join(rest)} ({describe_reasons(rest)})"
        self.alert("unpaused" if not rest else "unpause: still paused", msg)
        return msg

    def _reset_allowed(self, alert_kind: str, cfg_date: Any, used_key: str) -> str | None:
        """Review v1.3.0 F2: a floor reset in config is bound to ONE trigger date, from a newer config version,
        and can be used once. Returns the trigger's HKT date when allowed."""
        trig = self.store.latest("alerts", "kind = ?", [alert_kind])
        if trig is None or trig["config_version"] == self.cfg.config_version or cfg_date is None:
            return None
        day = str(trig["ts_hkt"])[:10]
        if str(cfg_date) != day:
            return None
        if self.store.count("equity_log", "data LIKE ?", [f'%"{used_key}": "{day}"%']):
            return None
        return day

    def cmd_resume(self, reset_peak: bool = False) -> str:
        """Clear pauses. A kill switch (drawdown / losing streak) is cleared only with reset_peak. The equity floor
        needs a newer config version with `risk.equity_floor_reset_baseline_usd` (<= current equity) and
        `risk.equity_floor_reset_for` = the trigger's date (single use). The permanent floor needs a newer config
        version with `risk.permanent_floor_reset_for` = the trigger's date (single use)."""
        st = self.rec.state()
        reasons = list(st["pause_reasons"])
        kill = [r for r in reasons if r in KILL_SWITCH_REASONS]
        if kill and not reset_peak:
            raise NeedsConfirmation(
                f"kill switch active ({', '.join(kill)}). Resuming it resets the drawdown peak to today's equity and "
                f"restarts the losing-streak count. Confirm with RESET-PEAK (resume --reset-peak).")
        prev = self.store.latest("equity_log")
        prev_data = prev["data"] if prev and isinstance(prev["data"], dict) else {}
        perm_pct_max = self.check_permanent_floor_config(prev_data)
        net_funded = prev_data.get("net_funded")
        cum_funded = prev_data.get("cum_funded")
        rk = self.cfg.risk
        keep: list[str] = []
        notes: list[str] = []
        floor_day = perm_day = None
        if "equity_floor" in reasons:
            floor_day = self._reset_allowed("KILL SWITCH: equity floor", rk.equity_floor_reset_for, "floor_reset_for") \
                if rk.equity_floor_reset_baseline_usd is not None else None
            if floor_day is None:
                keep.append("equity_floor")
        if "permanent_floor" in reasons:
            perm_day = self._reset_allowed("KILL SWITCH: permanent floor", rk.permanent_floor_reset_for, "perm_reset_for")
            if perm_day is None:
                keep.append("permanent_floor")
        eq = None
        if reset_peak or floor_day or perm_day:
            eq = self.equity(self.ex.get_account())
            if not eq["valid"]:
                raise EngineError("cannot resume: equity is unreadable")
        if floor_day and float(rk.equity_floor_reset_baseline_usd) > float(eq["equity"]):
            notes.append(f"equity_floor_reset_baseline_usd {rk.equity_floor_reset_baseline_usd} is above the current "
                         f"equity {eq['equity']:.2f}: refused")
            keep.append("equity_floor")
            floor_day = None
        cum_before = cum_funded
        if floor_day:
            net_funded = float(rk.equity_floor_reset_baseline_usd)
        if perm_day:
            cum_funded = float(eq["equity"])                 # the owner accepted the loss in a dated config
        if reset_peak:
            note = "resume (user confirmed RESET-PEAK): pauses and kill switches cleared; peak reset; streak restarted"
        else:
            note = "clear pauses (no kill switch active): peak and losing-streak count unchanged"
        self.rec.set_state(clear_reasons=True, note=note)
        for r in keep:
            self.rec.set_state(add_reason=r, note=f"{r} stays: {REASON_HELP.get(r, '')}")
        if eq is not None:
            peak = eq["equity"] if reset_peak else (float(prev["peak"]) if prev and prev["peak"] is not None else eq["equity"])
            self.store.insert("equity_log", equity=eq["equity"], wallet=eq["wallet"], upnl=eq["upnl"], peak=peak,
                              drawdown_pct=0.0 if reset_peak else (prev["drawdown_pct"] if prev else 0.0),
                              data={**eq, "peak_reset": reset_peak, "net_funded": net_funded, "floor_reset": bool(floor_day),
                                    "floor_reset_for": floor_day, "perm_reset_for": perm_day, "cum_funded": cum_funded,
                                    "cum_funded_before_reset": cum_before, "permanent_floor_pct_max": perm_pct_max,
                                    "live_review_checked_n": prev_data.get("live_review_checked_n")})
        msg = "pauses cleared"
        if reset_peak:
            msg += f"; drawdown peak reset to {eq['equity']:.2f}; losing-streak count restarted"
        if floor_day:
            msg += f"; equity floor re-based on the configured funded capital {net_funded:.2f} (trigger {floor_day})"
        if perm_day:
            msg += f"; permanent floor restarted by config (trigger {perm_day})"
        cur = float(eq["equity"]) if eq is not None else (float(prev["equity"]) if prev and prev["equity"] else None)
        if cum_before is not None and cur is not None:
            msg += f". Cumulative result since the first funding: {cur - float(cum_before):+.2f} on {float(cum_before):.2f}"
        msg += "".join(f". {n}" for n in notes)
        if keep:
            msg += f". STILL STOPPED: {', '.join(keep)} ({describe_reasons(keep)})"
        self.alert("resumed" if not keep else "resume: stop still active", msg)
        return "resumed" if not keep else f"{', '.join(keep)} still active"

    def pause_reasons_text(self) -> str:
        st = self.rec.state()
        lines = [f"state: {self.rec.display_state()}"]
        if not st["pause_reasons"]:
            lines.append("no pause is active")
        for r in st["pause_reasons"]:
            lines.append(f"- {r}: {REASON_HELP.get(r, 'see the dashboard alerts')}")
        return "\n".join(lines)

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

    def cmd_snapshot(self) -> dict[str, Any]:
        """Read-only exchange snapshot for the dashboard: no orders, no state changes, no kill checks."""
        inst = self.instrument()
        acct = self.ex.get_account()
        pos = acct.position(inst.id)
        eq = self.equity(acct)
        orders = self.ex.get_open_orders(inst.id)
        mark = self.ex.get_ticker(inst.id).mark
        data = {"mark": mark, "position": pos.__dict__ if pos else None,
                "equity": {k: eq.get(k) for k in ("equity", "wallet", "upnl", "source", "valid")},
                "sl": [o.trigger_price for o in orders if o.tpsl_kind == "sl" and o.status in ACTIVE_TRIGGER_STATUSES],
                "tp": [o.trigger_price for o in orders if o.tpsl_kind == "tp" and o.status in ACTIVE_TRIGGER_STATUSES],
                "open_orders": len(orders), "symbol": inst.symbol}
        self.store.insert("dash_snapshots", data=data)
        return data

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
        if not bool(self.cfg.shadow.enabled) or self.rolling:      # shadow variants replay daily decisions only
            return
        try:
            from perpbot.shadow import update_shadow

            update_shadow(self)
        except Exception as e:  # noqa: BLE001
            log.warning("shadow update failed: %s", e)
