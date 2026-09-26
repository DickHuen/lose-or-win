"""In-memory exchange for unit tests and `selftest`. Never touches the network."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Any

from perpbot.exchange.base import (
    ACTIVE_ORDER_STATUSES,
    AccountConfig,
    AccountSnapshot,
    Balance,
    Book,
    CancelResult,
    Exchange,
    ExchangeError,
    Fill,
    Flow,
    FundingPayment,
    Instrument,
    Order,
    OrderRejected,
    PlaceResult,
    Position,
    ProxyKeyInfo,
    Ticker,
)
from perpbot.indicators import Candle


def default_instrument() -> Instrument:
    return Instrument(id=1, symbol="BTC-PERP", base_asset="BTC", quote_asset="USD", category="crypto",
                      quantity_decimals=4, price_decimals=2, min_notional=1.0, max_market_notional=1_000_000.0,
                      max_limit_notional=5_000_000.0, max_leverage=20, isolated_only=False, price_bounds=0.05,
                      max_order_count=200, funding_interval="1h", risk_tiers=[(0.0, 20), (1_000_000.0, 10)])


class MockExchange(Exchange):
    def __init__(self, *, clock: Any, instrument: Instrument | None = None, balance: float = 10_000.0,
                 mark: float = 100_000.0, spread: float = 10.0, fee_rate: float = 0.0005) -> None:
        self.clock = clock
        self.inst = instrument or default_instrument()
        self.cash = balance
        self.mark = mark
        self.spread = spread
        self.fee_rate = fee_rate
        self.pos_size = 0.0
        self.pos_entry = 0.0
        self.pos_cum_funding = 0.0
        self.leverage_cfg: dict[int, AccountConfig] = {self.inst.id: AccountConfig(self.inst.id, 10, True)}
        self.orders: dict[int, Order] = {}
        self.fills: list[Fill] = []
        self.funding: list[FundingPayment] = []
        self.flows: list[Flow] = []
        self.klines: dict[str, list[Candle]] = {"1h": [], "4h": [], "1d": []}
        self.funding_history: list[tuple[int, float]] = []
        self._next_oid = 1000
        self._next_tid = 1
        self._next_fid = 1
        self.used_coids: set[str] = set()
        # behaviour switches / failure injection
        self.fok_outcomes: deque[bool] = deque()     # queued FOK outcomes (True fill, False unfilled)
        self.server_skew_ms = 0                       # exchange clock minus our clock
        self.server_time_fails = False
        self.auto_cancel_leftovers = True
        self.cancel_only = False
        self.position_tpsl_fails = False
        self.bracket_without_sl = False              # accept bracket but drop the SL leg
        self.reject_sl_row = False                   # SL row rejected (whole command reported rejected), entry fills
        self.reduce_only_fails = False
        self.update_leverage_fails = False
        self.place_timeout_after_exec = False        # order executes but the client sees a transport error
        self.liq_price_override: float | None = None
        self.raise_on: dict[str, Exception] = {}
        self.hide_orders_from_status = False         # /v1/account/orders does not show our order
        self.lag_position_reads = 0                  # next N get_account calls hide the position (stale read)
        self.position_tpsl_unknown = False           # position TP/SL is placed but the client sees outcome unknown
        self.hide_fills = False                      # fills endpoint lags
        self.proxy_info = ProxyKeyInfo("0xOWNER", "0xPROXY", None)
        self.geoblock = {"blocked": False, "country": "HK", "region": "HK"}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    # ------------------------------------------------------------ helpers
    def _now_ms(self) -> int:
        return int(self.clock.now().timestamp() * 1000)

    def _check_raise(self, name: str) -> None:
        exc = self.raise_on.pop(name, None)
        if exc is not None:
            raise exc

    @property
    def bid(self) -> float:
        return self.mark - self.spread / 2

    @property
    def ask(self) -> float:
        return self.mark + self.spread / 2

    def _oid(self) -> int:
        self._next_oid += 1
        return self._next_oid

    def _liq_price(self) -> float:
        if self.pos_size == 0:
            return 0.0
        if self.liq_price_override is not None:
            return self.liq_price_override
        lev = self.leverage_cfg[self.inst.id].leverage
        frac = 1.0 / lev - 1.0 / (2 * self.inst.max_leverage)
        return self.pos_entry * (1 - frac) if self.pos_size > 0 else self.pos_entry * (1 + frac)

    def _execute(self, side: str, qty: float, price: float, order: Order, liquidation: bool = False) -> None:
        signed = qty if side == "BUY" else -qty
        prev = self.pos_size
        prev_entry = self.pos_entry
        pnl = 0.0
        new = prev + signed
        if prev == 0 or (prev > 0) == (signed > 0):
            # open / increase
            self.pos_entry = (abs(prev) * prev_entry + qty * price) / (abs(prev) + qty) if (abs(prev) + qty) else 0.0
        else:
            closed = min(abs(signed), abs(prev))
            pnl = (price - prev_entry) * closed * (1 if prev > 0 else -1)
            if abs(signed) > abs(prev):
                self.pos_entry = price
            elif abs(new) < 1e-12:
                self.pos_entry = 0.0
        if abs(new) < 1e-12:
            new = 0.0
            self.pos_cum_funding = 0.0
        self.pos_size = new
        fee = price * qty * self.fee_rate
        self.cash += pnl - fee
        self.fills.append(Fill(self._next_tid, order.id, self.inst.id, "long" if side == "BUY" else "short", price, qty,
                               True, fee, "USDC", prev, prev_entry, pnl, liquidation, self._now_ms(),
                               order.client_order_id))
        self._next_tid += 1
        order.filled_quantity += qty
        order.status = "filled"
        order.updated_ms = self._now_ms()
        if self.pos_size == 0:
            self._on_flat()

    def _on_flat(self) -> None:
        if not self.auto_cancel_leftovers:
            return
        for o in self.orders.values():
            if o.is_trigger and o.status in ("untriggered", "armed"):
                o.status = "position_closed"

    def _active_triggers(self) -> list[Order]:
        return [o for o in self.orders.values() if o.is_trigger and o.status in ("armed", "untriggered")]

    # ------------------------------------------------------------ test controls
    def set_mark(self, mark: float, trigger: bool = True) -> None:
        self.mark = mark
        if trigger:
            self.run_triggers()

    def run_triggers(self) -> None:
        for o in sorted(self._active_triggers(), key=lambda x: x.id):
            if o.status not in ("armed", "untriggered") or self.pos_size == 0:
                continue
            long_pos = self.pos_size > 0
            hit = False
            if o.tpsl_kind == "sl":
                hit = self.mark <= o.trigger_price if long_pos else self.mark >= o.trigger_price
            elif o.tpsl_kind == "tp":
                hit = self.mark >= o.trigger_price if long_pos else self.mark <= o.trigger_price
            if hit:
                o.status = "triggered"
                qty = abs(self.pos_size)
                exit_order = Order(self._oid(), self.inst.id, "SELL" if long_pos else "BUY", o.trigger_price, qty, "ioc",
                                   True, "accepted", 0.0, 0.0, None, created_ms=self._now_ms())
                exit_order.parent_order_id = o.id
                exit_order.tpsl_kind = None
                self.orders[exit_order.id] = exit_order
                # record the trigger order id on the fill so the engine can classify the exit
                self._execute(exit_order.side, qty, o.trigger_price, exit_order)
                self.fills[-1] = replace(self.fills[-1], order_id=o.id)

    def apply_funding(self, rate: float) -> None:
        if self.pos_size == 0:
            return
        amount = -self.pos_size * self.mark * rate
        self.cash += amount
        self.pos_cum_funding += amount
        self.funding.append(FundingPayment(self._next_fid, self.inst.id, self.pos_size, rate, amount, self._now_ms()))
        self._next_fid += 1

    # ------------------------------------------------------------ public
    def get_instruments(self) -> list[Instrument]:
        self._check_raise("get_instruments")
        return [self.inst]

    def get_ticker(self, instrument_id: int) -> Ticker:
        self._check_raise("get_ticker")
        return Ticker(self.inst.id, self.mark, self.mark, self.mark, self.mark, 0.0000125, 100.0, self._now_ms() + 3_600_000,
                      self._now_ms())

    def get_book(self, instrument_id: int, depth: int) -> Book:
        self._check_raise("get_book")
        bids = [(self.bid - i * 5, 1.0 + i) for i in range(depth)]
        asks = [(self.ask + i * 5, 1.0 + i) for i in range(depth)]
        return Book(bids, asks, self._now_ms())

    def get_klines(self, instrument_id: int, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        self._check_raise("get_klines")
        return [c for c in self.klines.get(interval, []) if start_ms <= c.open_ms <= end_ms]

    def get_funding_history(self, instrument_id: int, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        return [x for x in self.funding_history if start_ms <= x[0] <= end_ms]

    def get_fee_schedule(self) -> list[dict[str, Any]]:
        return [{"category": "crypto", "taker_fee_rate": self.fee_rate, "maker_fee_rate": self.fee_rate / 2}]

    def get_geoblock(self) -> dict[str, Any]:
        return dict(self.geoblock)

    def get_server_time_raw(self) -> Any:
        if self.server_time_fails:
            raise ExchangeError("server time read failed (mock)")
        return {"time": self._now_ms() + self.server_skew_ms}

    # ------------------------------------------------------------ account
    def get_account(self) -> AccountSnapshot:
        self._check_raise("get_account")
        positions = []
        upnl = 0.0
        if self.lag_position_reads > 0 and self.pos_size != 0:
            self.lag_position_reads -= 1
            return AccountSnapshot([Balance("USDC", self.cash, self.cash)], [], self.cash, self.cash, False, self._now_ms())
        if self.pos_size != 0:
            upnl = (self.mark - self.pos_entry) * self.pos_size
            lev = self.leverage_cfg[self.inst.id]
            positions.append(Position(self.inst.id, self.pos_size, self.pos_entry, lev.leverage, lev.cross,
                                      self._liq_price(), upnl, self.pos_cum_funding,
                                      abs(self.pos_size) * self.pos_entry / lev.leverage, 0.0,
                                      abs(self.pos_size) * self.mark))
        return AccountSnapshot([Balance("USDC", self.cash, self.cash)], positions, self.cash + upnl, self.cash, False,
                               self._now_ms())

    def get_account_config(self, instrument_id: int) -> AccountConfig | None:
        self._check_raise("get_account_config")
        return self.leverage_cfg.get(instrument_id)

    def get_open_orders(self, instrument_id: int) -> list[Order]:
        self._check_raise("get_open_orders")
        return [replace(o) for o in self.orders.values() if o.instrument_id == instrument_id and o.status in ACTIVE_ORDER_STATUSES]

    def get_orders(self, *, order_id: int | None = None, client_order_id: str | None = None,
                   instrument_id: int | None = None, start_ms: int | None = None, end_ms: int | None = None) -> list[Order]:
        self._check_raise("get_orders")
        if self.hide_orders_from_status:
            return []
        out = []
        for o in self.orders.values():
            if order_id is not None and o.id != order_id:
                continue
            if client_order_id is not None and o.client_order_id != client_order_id:
                continue
            out.append(replace(o))
        return out

    def get_fills(self, start_ms: int, end_ms: int | None = None) -> list[Fill]:
        self._check_raise("get_fills")
        if self.hide_fills:
            return []
        return [f for f in self.fills if f.ts_ms >= start_ms and (end_ms is None or f.ts_ms <= end_ms)]

    def get_funding_payments(self, instrument_id: int, start_ms: int, end_ms: int | None = None) -> list[FundingPayment]:
        return [p for p in self.funding if p.ts_ms >= start_ms and (end_ms is None or p.ts_ms <= end_ms)]

    def get_proxy_key_info(self) -> ProxyKeyInfo | None:
        self._check_raise("get_proxy_key_info")
        return self.proxy_info

    def get_flows(self, start_ms: int) -> list[Flow]:
        return [f for f in self.flows if f.ts_ms >= start_ms]

    # ------------------------------------------------------------ commands
    def update_leverage(self, instrument_id: int, leverage: int, cross: bool) -> AccountConfig:
        self.calls.append(("update_leverage", {"leverage": leverage, "cross": cross}))
        self._check_raise("update_leverage")
        if self.update_leverage_fails:
            raise OrderRejected("leverage update rejected")
        self.leverage_cfg[instrument_id] = AccountConfig(instrument_id, leverage, cross)
        return self.leverage_cfg[instrument_id]

    def place_order(self, *, instrument_id: int, side: str, quantity: str, tif: str, price: str | None,
                    reduce_only: bool, client_order_id: str, tp_trigger: str | None = None,
                    sl_trigger: str | None = None) -> PlaceResult:
        self.calls.append(("place_order", dict(side=side, quantity=quantity, tif=tif, price=price, reduce_only=reduce_only,
                                               coid=client_order_id, tp=tp_trigger, sl=sl_trigger)))
        self._check_raise("place_order")
        if self.cancel_only:
            return PlaceResult(False, client_order_id=client_order_id,
                               error="Trading is currently cancel-only.", restriction="cancel_only")
        if client_order_id in self.used_coids:
            return PlaceResult(False, client_order_id=client_order_id, error="duplicate_order")
        if reduce_only and self.reduce_only_fails:
            return PlaceResult(False, client_order_id=client_order_id, error="reduce-only order rejected")
        self.used_coids.add(client_order_id)
        qty = float(quantity)
        px = float(price) if price is not None else None
        order = Order(self._oid(), instrument_id, side, px or 0.0, qty, tif, reduce_only, "accepted", 0.0, 0.0,
                      client_order_id, created_ms=self._now_ms(), updated_ms=self._now_ms())
        self.orders[order.id] = order
        tp_id = sl_id = None
        if reduce_only:
            if self.pos_size == 0 or (self.pos_size > 0) == (side == "BUY"):
                order.status = "reduce_only_invalid"
                return PlaceResult(True, order.id, client_order_id, order=replace(order))
            qty = min(qty, abs(self.pos_size))
        exec_price = self.ask if side == "BUY" else self.bid
        marketable = px is None or (px >= exec_price if side == "BUY" else px <= exec_price)
        if tif == "fok":
            fill = self.fok_outcomes.popleft() if self.fok_outcomes else True
            fill = fill and marketable
            if fill:
                self._execute(side, qty, exec_price, order)
            else:
                order.status = "fok_unfilled"
        elif tif == "ioc":
            if marketable:
                self._execute(side, qty, exec_price, order)
            else:
                order.status = "ioc_no_fill"
        else:
            if marketable:
                self._execute(side, qty, exec_price, order)
            else:
                order.status = "open"
                order.resting_quantity = qty
        row_rejected = None
        if tp_trigger or sl_trigger:
            exit_side = "SELL" if side == "BUY" else "BUY"
            parent_ok = order.status == "filled"
            for kind, trig in (("tp", tp_trigger), ("sl", sl_trigger)):
                if trig is None:
                    continue
                if kind == "sl" and self.bracket_without_sl:
                    continue
                long_entry = side == "BUY"
                wrong_side = (float(trig) >= self.mark) if (kind == "sl") == long_entry else (float(trig) <= self.mark)
                if wrong_side or (kind == "sl" and self.reject_sl_row):
                    row_rejected = f"{kind} row rejected" + (": trigger on the wrong side of mark" if wrong_side else "")
                    continue
                t = Order(self._oid(), instrument_id, exit_side, 0.0, qty, "ioc", True,
                          "armed" if parent_ok else "parent_cancelled", 0.0, 0.0, None, kind, "order", float(trig),
                          order.id, self._now_ms(), self._now_ms())
                self.orders[t.id] = t
                if kind == "tp":
                    tp_id = t.id
                else:
                    sl_id = t.id
        if self.place_timeout_after_exec:
            self.place_timeout_after_exec = False
            return PlaceResult(False, client_order_id=client_order_id, outcome_unknown=True, error="transport error")
        if row_rejected:
            # like SDK 0.11.0: any rejected row raises for the whole command, even if the entry row filled
            return PlaceResult(False, client_order_id=client_order_id, error=row_rejected)
        return PlaceResult(True, order.id, client_order_id, tp_id, sl_id, replace(order))

    def place_position_tpsl(self, *, instrument_id: int, tp_trigger: str | None, sl_trigger: str | None) -> PlaceResult:
        self.calls.append(("place_position_tpsl", {"tp": tp_trigger, "sl": sl_trigger}))
        self._check_raise("place_position_tpsl")
        if self.cancel_only:
            return PlaceResult(False, error="Trading is currently cancel-only.", restriction="cancel_only")
        if self.position_tpsl_fails:
            return PlaceResult(False, error="position tp/sl rejected")
        if self.pos_size == 0:
            return PlaceResult(False, error="No open Perps position")
        long_pos = self.pos_size > 0
        ids: dict[str, int] = {}
        for kind, trig in (("tp", tp_trigger), ("sl", sl_trigger)):
            if trig is None:
                continue
            tr = float(trig)
            crossed = (self.mark <= tr if long_pos else self.mark >= tr) if kind == "sl" else (
                self.mark >= tr if long_pos else self.mark <= tr)
            if crossed:
                return PlaceResult(False, error="trigger already crossed by mark")
            if any(o.tpsl_kind == kind and o.tpsl_scope == "position" for o in self._active_triggers()):
                return PlaceResult(False, error=f"position {kind} already exists")
            o = Order(self._oid(), instrument_id, "SELL" if long_pos else "BUY", 0.0, 0.0, "ioc", True, "armed", 0.0, 0.0,
                      None, kind, "position", tr, None, self._now_ms(), self._now_ms())
            self.orders[o.id] = o
            ids[kind] = o.id
        if self.position_tpsl_unknown:
            return PlaceResult(False, outcome_unknown=True, error="transport error after send")
        return PlaceResult(True, tp_order_id=ids.get("tp"), sl_order_id=ids.get("sl"))

    def cancel_orders(self, order_ids: list[int]) -> list[CancelResult]:
        self.calls.append(("cancel_orders", {"ids": list(order_ids)}))
        self._check_raise("cancel_orders")
        out = []
        for oid in order_ids:
            o = self.orders.get(oid)
            if o is None or o.status not in ACTIVE_ORDER_STATUSES:
                out.append(CancelResult(oid, False, "order_not_found"))
            else:
                o.status = "cancelled"
                out.append(CancelResult(oid, True))
        return out

    def probe_proxy_withdrawal(self, *, owner: str, amount_base_units: int) -> dict[str, Any]:
        return {"http_status": 401, "response": {"status": "err", "error": "invalid signature"}}

    def close(self) -> None:
        pass


class FailingExchange(MockExchange):
    """Mock whose every call raises (for error-path tests)."""

    def get_account(self) -> AccountSnapshot:
        raise ExchangeError("network down")
