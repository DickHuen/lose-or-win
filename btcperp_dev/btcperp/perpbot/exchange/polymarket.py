"""Live Polymarket Perps adapter built on the official SDK (polymarket-client, pinned).

- Public market data: `polymarket.AsyncPublicClient` (GET /v1/info/*).
- Account reads and signed commands: `polymarket.perps.PerpsSession`, constructed
  directly from the proxy credentials (proxy private key + secret). The owner
  (main wallet) key is never used or needed by this bot.
- Signed commands (createOrders, cancelOrders, updateLeverage) go over the SDK's
  authenticated WebSocket session; the WebSocket is opened only when the first
  signed command is sent.
- Never used by the bot: the cancel-all endpoint and the auto-cancel (dead-man) switch.

The SDK is async; a private event loop runs in a background thread so the
engine can stay synchronous and the WebSocket heartbeat keeps running.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable, TypeVar

import httpx

from perpbot.exchange.base import (
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
from perpbot.timeutil import HOUR_MS

log = logging.getLogger("perpbot.exchange")
T = TypeVar("T")

_MAX_PAGE_ITEMS = 50_000


def _f(x: Any) -> float:
    if x is None:
        return 0.0
    return float(x) if not isinstance(x, Decimal) else float(x)


def _ms(dt: Any) -> int:
    if dt is None:
        return 0
    if isinstance(dt, datetime):
        return int(dt.timestamp() * 1000)
    return int(dt)


def order_from_sdk(o: Any) -> Order:
    tpsl = getattr(o, "tp_sl", None)
    return Order(
        id=int(o.id), instrument_id=int(o.instrument_id), side=str(o.side), price=_f(o.price),
        quantity=_f(o.quantity), tif=str(o.time_in_force), reduce_only=bool(o.reduce_only), status=str(o.status),
        filled_quantity=_f(o.filled_quantity), resting_quantity=_f(o.resting_quantity),
        client_order_id=o.client_order_id,
        tpsl_kind=(tpsl.kind if tpsl else None), tpsl_scope=(tpsl.scope if tpsl else None),
        trigger_price=(_f(tpsl.trigger_price) if tpsl else None),
        parent_order_id=(int(tpsl.parent_order_id) if tpsl and tpsl.parent_order_id is not None else None),
        created_ms=_ms(o.created_at), updated_ms=_ms(o.updated_at))


def fill_from_sdk(f: Any) -> Fill:
    return Fill(
        trade_id=int(f.trade_id), order_id=int(f.order_id), instrument_id=int(f.instrument_id), side=str(f.side),
        price=_f(f.price), quantity=_f(f.quantity), taker=bool(f.taker), fee=_f(f.fee), fee_asset=str(f.fee_asset),
        previous_size=_f(f.previous_size), previous_entry_price=_f(f.previous_entry_price), pnl=_f(f.pnl),
        liquidation=bool(f.liquidation), ts_ms=_ms(f.timestamp), client_order_id=f.client_order_id)


def instrument_from_sdk(i: Any) -> Instrument:
    return Instrument(
        id=int(i.id), symbol=str(i.symbol), base_asset=str(i.base_asset), quote_asset=str(i.quote_asset),
        category=str(i.category), quantity_decimals=int(i.quantity_decimals), price_decimals=int(i.price_decimals),
        min_notional=_f(i.min_notional), max_market_notional=_f(i.max_market_notional),
        max_limit_notional=_f(i.max_limit_notional), max_leverage=int(i.max_leverage),
        isolated_only=bool(i.isolated_only), price_bounds=_f(i.price_bounds), max_order_count=int(i.max_order_count),
        funding_interval=str(i.funding_interval),
        risk_tiers=[(_f(t.lower_bound), int(t.max_leverage)) for t in i.risk_tiers])


class PolymarketExchange(Exchange):
    def __init__(self, cfg: Any, secrets: Any) -> None:
        self._cfg = cfg
        self._pm = cfg.polymarket
        self._secrets = secrets
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="polymarket-sdk-loop", daemon=True)
        self._thread.start()
        self._public: Any = None
        self._session: Any = None
        self._session_open = False
        self._http = httpx.Client(timeout=float(self._pm.http_timeout_seconds))
        self._closed = False

    # ------------------------------------------------------------ plumbing
    def _run(self, factory: Callable[[], Awaitable[T]], timeout: float | None = None) -> T:
        if self._closed:
            raise ExchangeError("exchange client closed")

        async def runner() -> T:
            return await factory()

        fut = asyncio.run_coroutine_threadsafe(runner(), self._loop)
        try:
            return fut.result(timeout or float(self._pm.command_timeout_seconds))
        except concurrent.futures.TimeoutError as e:
            fut.cancel()
            raise ExchangeError("Polymarket call timed out (outcome unknown)") from e
        except Exception as e:  # noqa: BLE001
            raise self._translate(e) from e

    @staticmethod
    def _translate(e: BaseException) -> ExchangeError:
        from polymarket import errors as pe

        if isinstance(e, ExchangeError):
            return e
        if isinstance(e, pe.RequestRejectedError):
            return OrderRejected(str(e), restriction=getattr(e, "restriction", None), code=getattr(e, "code", None))
        if isinstance(e, pe.RateLimitError):
            return ExchangeError(f"rate limited (retry_after={getattr(e, 'retry_after', None)})")
        if isinstance(e, pe.UserInputError):
            return OrderRejected(f"SDK input validation: {e}", code="user_input")
        return ExchangeError(f"{type(e).__name__}: {e}")

    async def _public_client(self) -> Any:
        if self._public is None:
            from polymarket import AsyncPublicClient

            self._public = AsyncPublicClient()
        return self._public

    async def _get_session(self, open_ws: bool) -> Any:
        from polymarket.models.perps import PerpsCredentials
        from polymarket.perps import PerpsSession

        if self._session is None:
            expires = self._secrets.proxy_expires_at or datetime(2100, 1, 1, tzinfo=UTC)
            creds = PerpsCredentials(proxy=self._secrets.proxy_address, private_key=self._secrets.proxy_private_key,
                                     secret=self._secrets.proxy_secret, expires_at=expires)
            self._session = PerpsSession(chain_id=int(self._pm.chain_id), credentials=creds,
                                         rest_url=self._pm.rest_url, ws_url=self._pm.ws_url,
                                         logger=logging.getLogger("perpbot.sdk"))
        if open_ws and not self._session_open:
            await self._session.open()
            self._session_open = True
        return self._session

    async def _drain(self, paginator: Any) -> list[Any]:
        out = []
        async for item in paginator.iter_items():
            out.append(item)
            if len(out) >= _MAX_PAGE_ITEMS:
                break
        return out

    def _auth_headers(self) -> dict[str, str]:
        # Header names from the official SDK (credential_headers).
        return {"POLYMARKET-PROXY": self._secrets.proxy_address, "POLYMARKET-SECRET": self._secrets.proxy_secret}

    # ------------------------------------------------------------ public
    def get_instruments(self) -> list[Instrument]:
        async def go() -> list[Instrument]:
            c = await self._public_client()
            return [instrument_from_sdk(i) for i in await c.fetch_perps_instruments()]
        return self._run(go)

    def get_ticker(self, instrument_id: int) -> Ticker:
        async def go() -> Ticker:
            c = await self._public_client()
            t = await c.fetch_perps_ticker(instrument_id=instrument_id)
            return Ticker(int(t.instrument_id), _f(t.mark_price), _f(t.index_price), _f(t.last_price), _f(t.mid_price),
                          _f(t.funding_rate), _f(t.open_interest), _ms(t.next_funding), _ms(t.timestamp) or None)
        return self._run(go)

    def get_book(self, instrument_id: int, depth: int) -> Book:
        async def go() -> Book:
            c = await self._public_client()
            b = await c.fetch_perps_book(instrument_id=instrument_id, depth=depth)
            return Book([(_f(l.price), _f(l.quantity)) for l in b.bids], [(_f(l.price), _f(l.quantity)) for l in b.asks],
                        _ms(b.timestamp))
        return self._run(go)

    def get_klines(self, instrument_id: int, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        step = {"1h": HOUR_MS, "4h": 4 * HOUR_MS, "1d": 24 * HOUR_MS}[interval]

        async def go() -> list[Candle]:
            c = await self._public_client()
            items = await self._drain(c.list_perps_candles(instrument_id=instrument_id, interval=interval,
                                                           start=start_ms, end=end_ms))
            return [Candle(_ms(k.timestamp), _f(k.open), _f(k.high), _f(k.low), _f(k.close), _f(k.volume),
                           _ms(k.timestamp) + step) for k in items]
        return self._run(go, timeout=120)

    def get_funding_history(self, instrument_id: int, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
        async def go() -> list[tuple[int, float]]:
            c = await self._public_client()
            items = await self._drain(c.list_perps_funding_history(instrument_id=instrument_id, start=start_ms, end=end_ms))
            return [(_ms(r.timestamp), _f(r.funding_rate)) for r in items]
        return self._run(go, timeout=120)

    def get_fee_schedule(self) -> list[dict[str, Any]]:
        async def go() -> list[dict[str, Any]]:
            c = await self._public_client()
            return [{"category": e.category, "taker_fee_rate": _f(e.taker_fee_rate), "maker_fee_rate": _f(e.maker_fee_rate)}
                    for e in await c.fetch_perps_fees()]
        return self._run(go)

    def get_geoblock(self) -> dict[str, Any]:
        try:
            r = self._http.get(self._pm.geoblock_url)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ExchangeError(f"geoblock check failed: {e}") from e
        return {"blocked": data.get("blocked"), "country": data.get("country"), "region": data.get("region")}

    def get_server_time_raw(self) -> Any:
        """GET /v1/info/time (docs: sync against it before signing). Raw body; shape not in the SDK."""
        try:
            r = self._http.get(self._pm.rest_url.rstrip("/") + "/v1/info/time")
            r.raise_for_status()
            try:
                return r.json()
            except ValueError:
                return r.text[:200]
        except httpx.HTTPError as e:
            raise ExchangeError(f"server time read failed: {e}") from e

    # ------------------------------------------------------------ account reads
    def get_account(self) -> AccountSnapshot:
        async def go() -> AccountSnapshot:
            s = await self._get_session(open_ws=False)
            pf = await s.fetch_portfolio()
            bals = await s.fetch_balances()
            positions = [Position(int(p.instrument_id), _f(p.size), _f(p.entry_price), int(p.leverage), bool(p.cross),
                                  _f(p.liquidation_price), _f(p.unrealized_pnl), _f(p.cumulative_funding),
                                  _f(p.initial_margin), _f(p.maintenance_margin), _f(p.position_value))
                         for p in pf.positions]
            return AccountSnapshot([Balance(b.asset, _f(b.balance), _f(b.value)) for b in bals], positions,
                                   _f(pf.margin.total_account_value), _f(pf.withdrawable), bool(pf.in_liquidation),
                                   _ms(pf.timestamp))
        return self._run(go)

    def get_account_config(self, instrument_id: int) -> AccountConfig | None:
        async def go() -> AccountConfig | None:
            s = await self._get_session(open_ws=False)
            for c in await s.fetch_account_config(instrument_id=instrument_id):
                if int(c.instrument_id) == instrument_id:
                    return AccountConfig(int(c.instrument_id), int(c.leverage), bool(c.cross))
            return None
        return self._run(go)

    def get_open_orders(self, instrument_id: int) -> list[Order]:
        async def go() -> list[Order]:
            s = await self._get_session(open_ws=False)
            return [order_from_sdk(o) for o in await s.fetch_open_orders(instrument_id=instrument_id)]
        return self._run(go)

    def get_orders(self, *, order_id: int | None = None, client_order_id: str | None = None,
                   instrument_id: int | None = None, start_ms: int | None = None, end_ms: int | None = None) -> list[Order]:
        async def go() -> list[Order]:
            s = await self._get_session(open_ws=False)
            return [order_from_sdk(o) for o in await s.fetch_orders(
                order_id=order_id, client_order_id=client_order_id, instrument_id=instrument_id,
                start=start_ms, end=end_ms)]
        return self._run(go)

    def get_fills(self, start_ms: int, end_ms: int | None = None) -> list[Fill]:
        async def go() -> list[Fill]:
            s = await self._get_session(open_ws=False)
            end = end_ms if end_ms is not None else int(time.time() * 1000)
            items = await self._drain(s.list_fills(start=start_ms, end=end, sort="asc"))
            return [fill_from_sdk(f) for f in items]
        return self._run(go, timeout=120)

    def get_funding_payments(self, instrument_id: int, start_ms: int, end_ms: int | None = None) -> list[FundingPayment]:
        async def go() -> list[FundingPayment]:
            s = await self._get_session(open_ws=False)
            end = end_ms if end_ms is not None else int(time.time() * 1000)
            items = await self._drain(s.list_funding_payments(instrument_id=instrument_id, start=start_ms, end=end))
            return [FundingPayment(int(p.id), int(p.instrument_id), _f(p.size), _f(p.funding_rate), _f(p.funding),
                                   _ms(p.timestamp)) for p in items]
        return self._run(go, timeout=120)

    def get_flows(self, start_ms: int) -> list[Flow]:
        async def go() -> list[Flow]:
            s = await self._get_session(open_ws=False)
            out: list[Flow] = []
            for d in await self._drain(s.list_deposits(start=start_ms)):
                out.append(Flow(f"dep:{d.hash}", "deposit", _f(d.amount), str(d.status),
                                _ms(d.confirmed_at or d.created_at)))
            for w in await self._drain(s.list_withdrawals(start=start_ms)):
                ts = getattr(w, "confirmed_at", None) or getattr(w, "created_at", None)
                out.append(Flow(f"wd:{w.withdrawal_id}", "withdrawal", _f(w.amount) + _f(getattr(w, "fee", 0)),
                                str(w.status), _ms(ts)))
            return out
        return self._run(go, timeout=120)

    def get_proxy_key_info(self) -> ProxyKeyInfo | None:
        # GET /v1/account/credentials (path and headers from the official SDK validate_credentials).
        from polymarket.models.perps import PerpsCredentialsInfo

        try:
            r = self._http.get(self._pm.rest_url.rstrip("/") + "/v1/account/credentials", headers=self._auth_headers())
            if r.status_code >= 400:
                raise OrderRejected(f"credentials check HTTP {r.status_code}: {r.text[:200]}")
            info = PerpsCredentialsInfo.parse_response(r.json())
        except (httpx.HTTPError, ValueError) as e:
            raise ExchangeError(f"credentials check failed: {e}") from e
        except Exception as e:  # noqa: BLE001
            raise self._translate(e) from e
        for k in info.keys:
            if k.proxy.lower() == self._secrets.proxy_address.lower():
                return ProxyKeyInfo(info.address, k.proxy, _ms(k.expires_at))
        return ProxyKeyInfo(info.address, self._secrets.proxy_address, None)

    # ------------------------------------------------------------ signed commands
    def update_leverage(self, instrument_id: int, leverage: int, cross: bool) -> AccountConfig:
        async def go() -> AccountConfig:
            s = await self._get_session(open_ws=True)
            r = await s.update_leverage(instrument_id=instrument_id, leverage=leverage, cross_margin=cross)
            return AccountConfig(int(r.instrument_id), int(r.leverage), bool(r.cross_margin))
        return self._run(go)

    def place_order(self, *, instrument_id: int, side: str, quantity: str, tif: str, price: str | None,
                    reduce_only: bool, client_order_id: str, tp_trigger: str | None = None,
                    sl_trigger: str | None = None) -> PlaceResult:
        from polymarket import errors as pe
        from polymarket.models.perps import PerpsTpSlTrigger

        async def go() -> PlaceResult:
            s = await self._get_session(open_ws=True)
            kwargs: dict[str, Any] = dict(instrument_id=instrument_id, side=side, quantity=quantity,
                                          time_in_force=tif, reduce_only=reduce_only, client_order_id=client_order_id)
            if price is not None:
                kwargs["price"] = price
            if tp_trigger is not None:
                kwargs["take_profit"] = PerpsTpSlTrigger(trigger_price=tp_trigger)
            if sl_trigger is not None:
                kwargs["stop_loss"] = PerpsTpSlTrigger(trigger_price=sl_trigger)
            try:
                placement = await s.place_order(**kwargs)
            except pe.TimeoutError:
                # Acks were OK (else RequestRejectedError) but no order update arrived in time.
                return PlaceResult(accepted=True, client_order_id=client_order_id, outcome_unknown=False,
                                   error="order update wait timed out; status must be polled")
            except (pe.TransportError, pe.ConnectionLostError) as e:
                return PlaceResult(accepted=False, client_order_id=client_order_id, outcome_unknown=True, error=str(e))
            order = order_from_sdk(placement.order)
            tp_id = sl_id = None
            if placement.tp_sl is not None:
                tp_id = int(placement.tp_sl.take_profit.order_id) if placement.tp_sl.take_profit else None
                sl_id = int(placement.tp_sl.stop_loss.order_id) if placement.tp_sl.stop_loss else None
            return PlaceResult(accepted=True, order_id=order.id, client_order_id=client_order_id,
                               tp_order_id=tp_id, sl_order_id=sl_id, order=order)

        try:
            return self._run(go)
        except OrderRejected as e:
            return PlaceResult(accepted=False, client_order_id=client_order_id, error=str(e), restriction=e.restriction)
        except ExchangeError as e:
            return PlaceResult(accepted=False, client_order_id=client_order_id, outcome_unknown=True, error=str(e))

    def place_position_tpsl(self, *, instrument_id: int, tp_trigger: str | None, sl_trigger: str | None) -> PlaceResult:
        from polymarket.models.perps import PerpsPositionTpSlTrigger

        async def go() -> PlaceResult:
            s = await self._get_session(open_ws=True)
            r = await s.place_position_tp_sl(
                instrument_id=instrument_id,
                take_profit=PerpsPositionTpSlTrigger(trigger_price=tp_trigger) if tp_trigger else None,
                stop_loss=PerpsPositionTpSlTrigger(trigger_price=sl_trigger) if sl_trigger else None)
            return PlaceResult(accepted=True,
                               tp_order_id=int(r.take_profit.order_id) if r.take_profit else None,
                               sl_order_id=int(r.stop_loss.order_id) if r.stop_loss else None)
        try:
            return self._run(go)
        except OrderRejected as e:
            return PlaceResult(accepted=False, error=str(e), restriction=e.restriction)
        except ExchangeError as e:
            return PlaceResult(accepted=False, outcome_unknown=True, error=str(e))

    def cancel_orders(self, order_ids: list[int]) -> list[CancelResult]:
        if not order_ids:
            return []

        async def go() -> list[CancelResult]:
            s = await self._get_session(open_ws=True)
            res = await s.cancel_orders(order_ids=[int(i) for i in order_ids])
            out = []
            for oid, r in zip(order_ids, res):
                out.append(CancelResult(int(r.order_id) if r.order_id is not None else int(oid), r.status == "ok", r.error))
            return out
        return self._run(go)

    def probe_proxy_withdrawal(self, *, owner: str, amount_base_units: int) -> dict[str, Any]:
        """Smoketest (a): sign a Withdraw with the PROXY key for the owner account; expected to be rejected.
        Destination is the owner's own wallet. Typed data built by the official SDK helper."""
        from eth_account import Account

        from polymarket._internal.actions.perps.signing import build_perps_withdraw_typed_data, random_perps_salt

        st = self._cfg.smoketest
        ts_s = int(time.time())
        salt = random_perps_salt()
        typed = build_perps_withdraw_typed_data(
            chain_id=int(self._pm.chain_id), deposit_contract=st.perps_deposit_contract, account=owner,
            token=st.collateral_token, amount=amount_base_units, to=owner, salt=salt, timestamp_s=ts_s)
        signed = Account.from_key(self._secrets.proxy_private_key).sign_typed_data(full_message=typed)
        body = {"op": {"type": "withdraw", "args": {"account": owner, "token": st.collateral_token,
                                                     "amount": str(amount_base_units), "to": owner}},
                "salt": salt, "sig": "0x" + bytes(signed.signature).hex(), "ts": ts_s}
        try:
            r = self._http.post(self._pm.rest_url.rstrip("/") + "/v1/account/withdraw", json=body)
            try:
                payload: Any = r.json()
            except ValueError:
                payload = r.text[:300]
            return {"http_status": r.status_code, "response": payload}
        except httpx.HTTPError as e:
            return {"http_status": None, "response": f"transport error: {e}"}

    def close(self) -> None:
        if self._closed:
            return

        async def shutdown() -> None:
            if self._session is not None:
                try:
                    await self._session.close()
                except Exception:  # noqa: BLE001
                    log.debug("session close failed", exc_info=True)
            if self._public is not None:
                try:
                    await self._public.close()
                except Exception:  # noqa: BLE001
                    log.debug("public client close failed", exc_info=True)

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), self._loop).result(15)
        except Exception:  # noqa: BLE001
            log.debug("shutdown failed", exc_info=True)
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._http.close()
