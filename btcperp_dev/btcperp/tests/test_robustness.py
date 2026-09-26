"""Robustness paths: hidden order status, lagging fills, smoketest fills, shorts, SDK parsing."""

from datetime import date

import pytest

from perpbot.records import Records

from conftest import hkt

D1, D2 = date(2026, 10, 5), date(2026, 10, 6)


def test_fill_confirmed_by_position_when_status_hidden(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.ex.hide_orders_from_status = True
    w.decide()
    assert w.pos() > 0 and len(w.fok_calls()) == 1
    t = Records(w.store).open_trade()
    assert t is not None and t["entry_fees"] > 0


def test_smoketest_fill_same_day_does_not_block_first_entry(world):
    w = world(hkt(2026, 10, 5, 3, 0))
    eng = w.engine()
    coid = eng.coid("smoketest:x:entry")
    eng.log_order("smoketest", "request", coid, None, None, {})
    w.ex.place_order(instrument_id=1, side="BUY", quantity="0.001", tif="fok", price=None, reduce_only=False,
                     client_order_id=coid)
    close = eng.coid("smoketest:x:close")
    eng.log_order("smoketest", "request", close, None, None, {})
    w.ex.place_order(instrument_id=1, side="SELL", quantity="0.001", tif="ioc", price=None, reduce_only=True,
                     client_order_id=close)
    assert w.pos() == 0
    w.bn.signal(D1, "strong_long")
    w.at(hkt(2026, 10, 5, 8, 30)).decide()
    assert w.pos() > 0


def test_incomplete_close_is_corrected_when_fills_arrive(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.decide()
    t = Records(w.store).open_trade()
    w.ex.hide_fills = True
    w.ex.set_mark(t["sl_price"] - 1)
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    c = Records(w.store).closed_trades()[0]
    assert c["incomplete"] and c["gross_pnl"] == 0 and c["net_pnl"] == pytest.approx(-c["entry_fees"])
    w.ex.hide_fills = False
    w.at(hkt(2026, 10, 5, 16, 30)).manage()
    closed = Records(w.store).closed_trades()
    assert len(closed) == 1
    assert not closed[0]["incomplete"] and closed[0]["net_pnl"] < 0 and closed[0]["correction"]
    assert closed[0]["exit_reason"] == "SL"


def test_short_trade_full_cycle(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_short")
    w.decide()
    assert w.pos() < 0
    t = Records(w.store).open_trade()
    assert t["tp_price"] < t["entry_price"] < t["sl_price"]
    w.ex.set_mark(t["tp_price"] - 1)
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    c = Records(w.store).closed_trades()[0]
    assert c["exit_reason"] == "TP" and c["net_pnl"] > 0 and c["direction"] == -1
    assert c["mfe"] >= t["entry_price"] - t["tp_price"] - 2


def test_no_cancel_all_or_auto_cancel_used():
    from pathlib import Path

    src = "".join(p.read_text() for p in (Path(__file__).resolve().parent.parent / "perpbot").rglob("*.py"))
    for forbidden in ("cancel_all_orders(", "arm_auto_cancel(", "disarm_auto_cancel(", "/v1/trade/orders/all",
                      "update_margin("):
        assert forbidden not in src


# ------------------------------------------------------------------ SDK wire parsing
def test_sdk_models_map_to_bot_types():
    from polymarket.models.perps import PerpsFill, PerpsInstrument, PerpsOrder, PerpsPortfolio

    from perpbot.exchange.polymarket import fill_from_sdk, instrument_from_sdk, order_from_sdk

    o = order_from_sdk(PerpsOrder.parse_response({
        "oid": 11, "iid": 1, "buy": False, "p": "99000.5", "qty": "0.0100", "tif": "fok", "po": False, "ro": True,
        "status": "filled", "rest": "0", "fill": "0.0100", "cts": 1_780_000_000_000, "uts": 1_780_000_000_500,
        "coid": "a" * 32}))
    assert (o.id, o.side, o.reduce_only, o.status, o.filled_quantity) == (11, "SELL", True, "filled", 0.01)
    trig = order_from_sdk(PerpsOrder.parse_response({
        "oid": 12, "iid": 1, "buy": False, "p": "0", "qty": "0.01", "tif": "ioc", "po": False, "ro": True,
        "status": "armed", "rest": "0", "fill": "0", "cts": 1, "uts": 1,
        "tpsl": {"kind": "sl", "scope": "order", "trp": "97000", "parent_oid": 11}}))
    assert trig.tpsl_kind == "sl" and trig.trigger_price == 97000 and trig.parent_order_id == 11 and trig.is_active
    f = fill_from_sdk(PerpsFill.parse_response({
        "tid": 5, "oid": 11, "iid": 1, "side": "short", "p": "99000", "qty": "0.01", "taker": True, "fee": "0.5",
        "fea": "USDC", "psz": "0.01", "pep": "100000", "pnl": "-10", "liq": False, "ts": 1_780_000_000_000}))
    assert f.is_reducing and not f.is_opening and f.pnl == -10 and f.fee == 0.5
    inst = instrument_from_sdk(PerpsInstrument.parse_response({
        "instrument_id": 1, "category": "crypto", "symbol": "BTC-PERP", "base_asset": "BTC", "quote_asset": "USD",
        "funding_interval": "1h", "quantity_decimals": 4, "price_decimals": 2, "price_bounds": "0.05",
        "liquidation_fee": "0.01", "max_order_count": 200, "min_notional": "1", "max_market_notional": "1000000",
        "max_limit_notional": "5000000", "max_leverage": 20, "isolated_only": False,
        "risk_tiers": [{"lower_bound": "0", "max_leverage": 20}]}))
    assert inst.symbol == "BTC-PERP" and inst.risk_tiers == [(0.0, 20)] and inst.quantity_decimals == 4
    pf = PerpsPortfolio.parse_response({
        "positions": [{"instrument_id": 1, "symbol": "BTC-PERP", "size": "-0.01", "entry_price": "100000", "leverage": 3,
                       "cross": False, "initial_margin": "333", "maintenance_margin": "25", "position_value": "1000",
                       "liquidation_price": "130000", "unrealized_pnl": "-5", "return_on_equity": "0",
                       "cumulative_funding": "0.2"}],
        "margin": {"total_account_value": "10000", "total_initial_margin": "333", "total_maintenance_margin": "25",
                   "total_position_value": "1000"}, "withdrawable": "9000", "in_liquidation": False,
        "timestamp": 1_780_000_000_000})
    assert float(pf.positions[0].size) == -0.01


def test_sdk_error_translation():
    from polymarket import errors as pe

    from perpbot.exchange.base import OrderRejected
    from perpbot.exchange.polymarket import PolymarketExchange

    e = PolymarketExchange._translate(pe.RequestRejectedError("Trading is currently cancel-only.", status=503,
                                                              restriction="cancel_only"))
    assert isinstance(e, OrderRejected) and e.restriction == "cancel_only"
    assert not isinstance(PolymarketExchange._translate(pe.TransportError("x")), OrderRejected)


def test_polymarket_adapter_starts_and_closes_without_network(cfg):
    from types import SimpleNamespace

    from perpbot.exchange.polymarket import PolymarketExchange

    ex = PolymarketExchange(cfg, SimpleNamespace(proxy_address="0x0", proxy_private_key="", proxy_secret="",
                                                 proxy_expires_at=None))
    ex.close()
    ex.close()
    with pytest.raises(Exception):
        ex.get_instruments()


def test_sdk_bracket_wire_format_matches_docs():
    """createOrders body produced by the SDK: keyed order fields and grp='order' for brackets."""
    from polymarket._internal.actions.perps.trading import create_orders_op, to_command_body_op, to_raw_tp_sl_order
    from polymarket.models.perps import PerpsTpSlTrigger

    entry = [1, True, "100010.00", "0.0100", "fok", False, None, "b" * 32, None]
    sl = to_raw_tp_sl_order(buy=False, instrument_id=1, kind="sl", quantity="0.0100",
                            trigger=PerpsTpSlTrigger(trigger_price="97000.00"))
    body = to_command_body_op(create_orders_op([entry, sl], group="order"))
    assert body["type"] == "createOrders" and body["grp"] == "order"
    assert body["args"][0] == {"iid": 1, "buy": True, "po": False, "qty": "0.0100", "tif": "fok", "p": "100010.00",
                               "c": "b" * 32}
    assert body["args"][1]["ro"] is True and body["args"][1]["tr"] == {"tpsl": "sl", "trp": "97000.00", "market": True}
