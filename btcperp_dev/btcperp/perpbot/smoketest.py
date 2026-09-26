"""Live smoketest at minimum size. Run by Grok Bot BEFORE going live (never from the dev laptop).

Steps: read prices, region check, credentials, 3x isolated, place+cancel one order,
open+close one minimum position with bracket SL/TP, and live answers to API_NOTES (a)-(e)
plus the equity / fee checks the bot relies on. Writes data/smoketest/smoketest_<ts>.json.
"""

from __future__ import annotations

import json
import math
import traceback
from decimal import Decimal
from typing import Any, Callable

from perpbot.exchange.base import ACTIVE_TRIGGER_STATUSES, FILLED_STATUSES
from perpbot.paths import Paths
from perpbot.risk import quantize_price
from perpbot.strategy import compute_score
from perpbot.timeutil import DAY_MS, HOUR_MS, to_ms, utc_day

ZH = {
    "instruments": "讀取合約規格", "prices": "讀取價格/K線/資金費率", "region": "地區檢查",
    "credentials": "代理金鑰與錢包", "account": "帳戶與權益", "leverage": "設定3倍逐倉",
    "place_cancel": "掛單並撤單", "open_bracket": "開最小倉位（附止損/止盈）",
    "b_position_sl_with_bracket": "(b) 括號止損與倉位止損能否並存", "close": "減倉平倉",
    "c_leftovers_after_close": "(c) 平倉後剩餘訂單是否自動清除", "d_funding_after_close": "(d) 平倉後能否讀取累計資金費",
    "a_proxy_withdraw": "(a) 代理金鑰能否提款", "e_cancel_only": "(e) 只可撤單模式下的下單回應",
    "equity_formula": "權益計算方式核對", "telegram": "Telegram 通知（已停用）", "server_time": "伺服器時間同步",
}


def _dec(d: Decimal) -> str:
    return format(d, "f")


def run_smoketest(engine: Any, paths: Paths, *, allow_trading: bool = True) -> tuple[bool, list[dict[str, Any]]]:
    ex, cfg = engine.ex, engine.cfg
    results: list[dict[str, Any]] = []
    ctx: dict[str, Any] = {}
    stop = {"trading": False}

    def step(name: str, fn: Callable[[], Any], critical: bool = False, trading: bool = False) -> Any:
        if trading and (stop["trading"] or not allow_trading):
            results.append({"step": name, "zh": ZH.get(name, name), "ok": None, "detail": "skipped"})
            return None
        try:
            detail = fn()
            ok = True
            if isinstance(detail, tuple):
                ok, detail = detail
            results.append({"step": name, "zh": ZH.get(name, name), "ok": ok, "detail": detail})
            if not ok and critical:
                stop["trading"] = True
            return detail
        except Exception as e:  # noqa: BLE001
            results.append({"step": name, "zh": ZH.get(name, name), "ok": False,
                            "detail": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]})
            if critical:
                stop["trading"] = True
            return None

    now = engine.now()
    now_ms = to_ms(now)

    def s_inst() -> Any:
        inst = engine.instrument()
        ctx["inst"] = inst
        return inst.to_dict()
    step("instruments", s_inst, critical=True)

    def s_prices() -> Any:
        inst = ctx["inst"]
        t = ex.get_ticker(inst.id)
        b = ex.get_book(inst.id, int(cfg.polymarket.book_depth))
        k1h = ex.get_klines(inst.id, "1h", now_ms - DAY_MS, now_ms)
        k1d = ex.get_klines(inst.id, "1d", now_ms - 30 * DAY_MS, now_ms)
        fh = ex.get_funding_history(inst.id, now_ms - DAY_MS, now_ms)
        daily = engine.bn.klines("1d", int(cfg.binance.daily_candles_to_load), now_ms)
        h4 = engine.bn.klines("4h", int(cfg.binance.h4_candles_to_load), now_ms)
        bf = engine.bn.funding(now_ms - int(cfg.binance.funding_days_to_load) * DAY_MS, now_ms)
        sc = compute_score(daily, utc_day(now), cfg.strategy)
        ctx["atr"] = sc.atr
        ctx["book"] = b
        return {"mark": t.mark, "index": t.index, "polymarket_funding_rate": t.funding_rate, "best_bid": b.best_bid,
                "best_ask": b.best_ask, "pm_1h_klines_24h": len(k1h), "pm_1d_klines_30d": len(k1d),
                "pm_funding_prints_24h": len(fh), "binance_daily": len(daily), "binance_4h": len(h4),
                "binance_funding_prints": len(bf), "binance_price": engine.bn.price(),
                "today_score": sc.score, "atr14": sc.atr}
    step("prices", s_prices, critical=True)

    def s_time() -> Any:
        raw = ex.get_server_time_raw()
        local = to_ms(engine.now())
        server = None
        if isinstance(raw, (int, float)):
            server = int(raw)
        elif isinstance(raw, dict):
            for v in raw.values():
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 1_000_000_000:
                    server = int(v if v > 10_000_000_000 else v * 1000)
                    break
        skew = (server - local) if server is not None else None
        ok = skew is None or abs(skew) < 30_000
        return ok, {"raw": raw, "local_ms": local, "skew_ms": skew,
                    "note": "request signatures use this computer's clock; keep it NTP-synced"}
    step("server_time", s_time)

    def s_region() -> Any:
        g = ex.get_geoblock()
        return (g.get("blocked") is False), g
    step("region", s_region, critical=True)

    def s_creds() -> Any:
        info = ex.get_proxy_key_info()
        owner_ok = info is not None and info.owner_address.lower() == engine.secrets.wallet_address.lower()
        days = ((info.expires_at_ms - now_ms) / DAY_MS) if (info and info.expires_at_ms) else None
        return (owner_ok and (days is None or days > 0)), {
            "owner_matches_PM_WALLET_ADDRESS": owner_ok, "proxy": info.proxy if info else None,
            "expires_in_days": days}
    step("credentials", s_creds, critical=True)

    def s_account() -> Any:
        inst = ctx["inst"]
        acct = ex.get_account()
        eq = engine.equity(acct)
        ctx["wallet_before"] = eq["wallet"]
        ctx["tav_before"] = acct.total_account_value
        pos = acct.position(inst.id)
        orders = ex.get_open_orders(inst.id)
        ok = pos is None and eq["equity"] > 0
        return ok, {"equity": eq, "position": pos.__dict__ if pos else None, "open_orders": len(orders),
                    "note": "" if pos is None else "an open position exists: trading steps skipped"}
    step("account", s_account, critical=True)

    def s_leverage() -> Any:
        ok, why = engine.ensure_leverage(ctx["inst"])
        c = ex.get_account_config(ctx["inst"].id)
        return ok, {"result": why, "account_config": c.__dict__ if c else None,
                    "auto_margin_topup": "no such setting in the API (only manual updateMargin, never used)"}
    step("leverage", s_leverage, critical=True, trading=True)

    def min_qty(price: float) -> Decimal:
        inst = ctx["inst"]
        step_q = Decimal(1).scaleb(-inst.quantity_decimals)
        need = Decimal(str(math.ceil(inst.min_notional * 1.2 / price / float(step_q)))) * step_q
        return max(need, step_q)

    tag = now.strftime("%Y%m%d%H%M%S")

    def s_place_cancel() -> Any:
        inst = ctx["inst"]
        b = ex.get_book(inst.id, int(cfg.polymarket.book_depth))
        px = quantize_price(b.best_bid * (1 - float(cfg.smoketest.resting_order_offset_pct) / 100), inst.price_decimals, "down")
        qty = min_qty(float(px))
        coid = engine.coid(f"smoketest:{tag}:rest")
        engine.log_order("smoketest", "request", coid, None, None, {"side": "BUY", "qty": _dec(qty), "price": _dec(px), "tif": "gtc"})
        res = ex.place_order(instrument_id=inst.id, side="BUY", quantity=_dec(qty), tif="gtc", price=_dec(px),
                             reduce_only=False, client_order_id=coid)
        engine.log_order("smoketest", "response", coid, res.order_id, str(res.accepted), {"error": res.error})
        if not res.accepted:
            return False, {"error": res.error, "restriction": res.restriction}
        engine.sleep(1.0)
        o = [x for x in ex.get_orders(client_order_id=coid) if not x.is_trigger]
        oid = res.order_id or (o[0].id if o else None)
        if oid is None:
            return False, "order id not found"
        c = ex.cancel_orders([oid])
        engine.sleep(1.0)
        after = [x for x in ex.get_orders(order_id=oid)]
        st = after[0].status if after else None
        return (bool(c and c[0].ok) and st in ("cancelled",)), {"order_id": oid, "status_before": o[0].status if o else None,
                                                                "cancel": [r.__dict__ for r in c], "status_after": st}
    step("place_cancel", s_place_cancel, critical=True, trading=True)

    def s_open() -> Any:
        inst = ctx["inst"]
        b = ex.get_book(inst.id, int(cfg.polymarket.book_depth))
        ask = b.best_ask
        px = quantize_price(ask * (1 + float(cfg.exits.entry_slippage_bps) / 1e4), inst.price_decimals, "down")
        qty = min_qty(ask)
        atr = ctx.get("atr") or ask * 0.02
        sl = quantize_price(ask - float(cfg.exits.sl_atr_multiple) * atr, inst.price_decimals, "nearest")
        tp = quantize_price(ask + float(cfg.exits.tp_atr_multiple) * atr, inst.price_decimals, "nearest")
        coid = engine.coid(f"smoketest:{tag}:entry")
        ctx["entry_ts"] = to_ms(engine.now())
        engine.log_order("smoketest", "request", coid, None, None, {"side": "BUY", "qty": _dec(qty), "price": _dec(px),
                                                                   "tif": "fok", "tp": _dec(tp), "sl": _dec(sl)})
        res = ex.place_order(instrument_id=inst.id, side="BUY", quantity=_dec(qty), tif="fok", price=_dec(px),
                             reduce_only=False, client_order_id=coid, tp_trigger=_dec(tp), sl_trigger=_dec(sl))
        engine.log_order("smoketest", "response", coid, res.order_id, str(res.accepted),
                         {"error": res.error, "tp_order_id": res.tp_order_id, "sl_order_id": res.sl_order_id})
        o = engine.confirm_order(coid, res, "smoketest")
        acct = ex.get_account()
        pos = acct.position(inst.id)
        orders = ex.get_open_orders(inst.id)
        trig = [{"id": x.id, "kind": x.tpsl_kind, "scope": x.tpsl_scope, "status": x.status, "trigger": x.trigger_price,
                 "parent": x.parent_order_id} for x in orders if x.is_trigger]
        ctx.update({"qty": qty, "sl": sl, "entry_order": o, "tp_id": res.tp_order_id, "sl_id": res.sl_order_id,
                    "bracket_ids": [t["id"] for t in trig]})
        eq = engine.equity(acct)
        ctx["after_entry"] = {"wallet": eq["wallet"], "upnl": eq["upnl"], "total_account_value": acct.total_account_value,
                              "initial_margin": pos.initial_margin if pos else None}
        filled = o is not None and o.status in FILLED_STATUSES and pos is not None
        has_sl = any(t["kind"] == "sl" and t["status"] in ACTIVE_TRIGGER_STATUSES for t in trig)
        return (filled and has_sl), {"order_status": o.status if o else None, "position": pos.__dict__ if pos else None,
                                     "triggers": trig, "sl_present": has_sl, "liquidation_price": pos.liquidation_price if pos else None}
    step("open_bracket", s_open, critical=True, trading=True)

    def s_b() -> Any:
        inst = ctx["inst"]
        sl2 = quantize_price(float(ctx["sl"]) * 0.999, inst.price_decimals, "nearest")
        res = ex.place_position_tpsl(instrument_id=inst.id, tp_trigger=None, sl_trigger=_dec(sl2))
        orders = ex.get_open_orders(inst.id)
        sls = [{"id": x.id, "scope": x.tpsl_scope, "status": x.status, "trigger": x.trigger_price}
               for x in orders if x.tpsl_kind == "sl"]
        if res.accepted and res.sl_order_id:
            ex.cancel_orders([res.sl_order_id])
        both = res.accepted and len([s for s in sls if s["status"] in ACTIVE_TRIGGER_STATUSES]) >= 2
        return True, {"position_sl_accepted": res.accepted, "error": res.error, "sl_orders_seen": sls,
                      "answer_b": "YES, both can exist" if both else ("NO: " + str(res.error) if not res.accepted
                                                                         else "accepted but only one SL visible")}
    step("b_position_sl_with_bracket", s_b, trading=True)

    def s_close() -> Any:
        inst = ctx["inst"]
        coid = engine.coid(f"smoketest:{tag}:close")
        engine.log_order("smoketest", "request", coid, None, None, {"side": "SELL", "qty": _dec(ctx["qty"]), "tif": "ioc",
                                                                   "reduce_only": True})
        res = ex.place_order(instrument_id=inst.id, side="SELL", quantity=_dec(ctx["qty"]), tif="ioc", price=None,
                             reduce_only=True, client_order_id=coid)
        engine.log_order("smoketest", "response", coid, res.order_id, str(res.accepted), {"error": res.error})
        o = engine.confirm_order(coid, res, "smoketest")
        pos = ex.get_account().position(inst.id)
        ctx["exit_ts"] = to_ms(engine.now())
        return pos is None, {"order_status": o.status if o else None, "position_after": pos.__dict__ if pos else None}
    step("close", s_close, critical=True, trading=True)

    def s_c() -> Any:
        inst = ctx["inst"]
        engine.sleep(2.0)
        orders = ex.get_open_orders(inst.id)
        left = [x for x in orders if x.is_active and (x.is_trigger or x.reduce_only)]
        statuses = {}
        for oid in ctx.get("bracket_ids", []):
            got = ex.get_orders(order_id=oid)
            statuses[oid] = got[0].status if got else None
        if left:
            ex.cancel_orders([x.id for x in left])
        return True, {"leftover_active_after_close": [x.id for x in left], "bracket_order_statuses": statuses,
                      "answer_c": "YES, cleared automatically" if not left else "NO, leftovers had to be cancelled by id"}
    step("c_leftovers_after_close", s_c, trading=True)

    def s_d() -> Any:
        inst = ctx["inst"]
        acct = ex.get_account()
        pos = acct.position(inst.id)
        pays = ex.get_funding_payments(inst.id, ctx["entry_ts"] - HOUR_MS)
        fills = [f for f in ex.get_fills(ctx["entry_ts"] - 60_000) if f.instrument_id == inst.id]
        eq = engine.equity(acct)
        gross = sum(f.pnl for f in fills)
        fees = sum(f.fee for f in fills)
        delta = eq["wallet"] - ctx.get("wallet_before", eq["wallet"])
        ctx["pnl_check"] = {"sum_fill_pnl": gross, "sum_fees": fees, "wallet_delta": delta,
                            "wallet_delta_matches_pnl_minus_fees": abs(delta - (gross - fees)) < max(1e-6, abs(fees) * 0.05),
                            "wallet_delta_matches_pnl_only": abs(delta - gross) < max(1e-6, abs(fees) * 0.05)}
        sem = [{"trade_id": f.trade_id, "side": f.side, "previous_size": f.previous_size, "quantity": f.quantity,
                "pnl": f.pnl, "fee": f.fee} for f in fills]
        return True, {"position_in_portfolio_after_close": pos.__dict__ if pos else None,
                      "fill_semantics": {"fills": sem, "note": "entry fill should have previous_size 0; exit fill "
                                         "previous_size should be +qty (signed) for a long; side shows whether "
                                         "'side' is trade direction (exit=short) or position side (exit=long)"},
                      "funding_payments_in_window": [p.__dict__ for p in pays],
                      "answer_d": ("cumulative_funding is only on open positions; after close use "
                                   "GET /v1/account/funding (payments listed: %d)" % len(pays)),
                      "fills": [f.__dict__ for f in fills], "pnl_check": ctx["pnl_check"]}
    step("d_funding_after_close", s_d, trading=True)

    def s_eq() -> Any:
        a = ctx.get("after_entry") or {}
        wb = ctx.get("wallet_before")
        margin = a.get("initial_margin")
        excl = None
        if wb is not None and margin:
            excl = abs((wb - a.get("wallet", wb)) - margin) < margin * 0.2
        return True, {"wallet_before": wb, "total_account_value_before": ctx.get("tav_before"), "after_entry": a,
                      "balance_excludes_isolated_margin": excl,
                      "note": "if True, the bot's 'auto' equity source uses total_account_value"}
    step("equity_formula", s_eq, trading=True)

    def s_a() -> Any:
        if not bool(cfg.smoketest.probe_proxy_withdrawal):
            return True, "disabled in config"
        r = ex.probe_proxy_withdrawal(owner=engine.secrets.wallet_address, amount_base_units=1)
        resp = r.get("response")
        ok_status = isinstance(resp, dict) and resp.get("status") == "ok"
        return (not ok_status), {"probe": r, "answer_a": "NO (rejected, as expected)" if not ok_status
                                 else "YES - PROXY KEY CAN WITHDRAW. Stop and tell the owner."}
    step("a_proxy_withdraw", s_a)

    step("e_cancel_only", lambda: (True, {
        "answer_e": "Not live-testable unless the exchange is in cancel-only mode. Documented/SDK behaviour: "
                    "REST 503 with error text containing 'cancel-only' (SDK RequestRejectedError.restriction="
                    "'cancel_only'); WebSocket commands return an ack {status:'err', error:...}. The bot treats "
                    "it as 'order not placed' (no entry; SL re-place failure -> close attempt -> alert)."}))

    def s_tg() -> Any:
        if not engine.tg.enabled:
            return True, "Telegram disabled by config; alerts are delivered through `python3 run.py alerts`"
        ok = engine.tg.send("btcperp smoketest: Telegram OK")
        engine.tg.get_updates(None)
        return ok, {"sent": ok}
    step("telegram", s_tg)

    all_ok = all(r["ok"] is not False for r in results) and not any(r["ok"] is None for r in results if allow_trading)
    out = paths.smoketest_dir / f"smoketest_{tag}.json"
    out.write_text(json.dumps({"ok": all_ok, "results": results}, indent=2, default=str), encoding="utf-8")
    return all_ok, results


def summary_text(ok: bool, results: list[dict[str, Any]]) -> str:
    lines = [f"SMOKETEST {'PASS' if ok else 'FAIL'}"]
    for r in results:
        mark = "PASS" if r["ok"] else ("SKIP" if r["ok"] is None else "FAIL")
        det = r["detail"]
        ans = ""
        if isinstance(det, dict):
            ans = next((str(v) for k, v in det.items() if k.startswith("answer_")), "")
        lines.append(f"[{mark}] {r['step']} / {r['zh']}" + (f" -> {ans}" if ans else ""))
    return "\n".join(lines)
