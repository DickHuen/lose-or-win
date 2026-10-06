"""End-to-end engine scenarios against the mock exchange (no network)."""

from datetime import date, timedelta

import pytest

from perpbot.records import Records
from perpbot.reports import Reporter

from conftest import P, hkt

D1, D2, D3, D4 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)


def enter_long(w):
    w.bn.signal(D1, "strong_long")
    w.at(hkt(2026, 10, 5, 8, 30)).decide()
    assert w.pos() > 0
    return Records(w.store).open_trade()


def active(w, kind):
    return [o for o in w.ex.orders.values() if o.tpsl_kind == kind and o.status in ("armed", "untriggered")]


# ------------------------------------------------------------------ entries
def test_0830_and_0850_both_fire_only_one_entry(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    out = w.decide()
    assert out["plan"]["action"] == "enter" and w.pos() > 0
    w.at(hkt(2026, 10, 5, 8, 50)).decide()
    assert len(w.fok_calls()) == 1
    assert w.store.count("intents", "utc_day='2026-10-05'") == 1
    t = Records(w.store).open_trade()
    assert t["direction"] == 1 and t["sl_price"] < t["entry_price"] < t["tp_price"]
    assert len(active(w, "sl")) == 1 and len(active(w, "tp")) == 1
    assert w.ex.leverage_cfg[1].leverage == 3 and w.ex.leverage_cfg[1].cross is False
    assert w.tg.has("open")


def test_0850_completes_when_0830_failed_before_any_order(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.bn.fail = True
    with pytest.raises(Exception):
        w.decide()
    assert w.pos() == 0 and w.store.count("intents") == 0
    w.bn.fail = False
    w.at(hkt(2026, 10, 5, 8, 50)).decide()
    assert w.pos() > 0 and len(w.fok_calls()) == 1


def test_decision_logged_before_order(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.decide()
    intent = w.store.latest("intents")
    first_order = w.store.query("SELECT MIN(id) AS id, MIN(ts_ms) AS ts FROM orders WHERE purpose='entry'")[0]
    assert intent is not None and intent["data"]["score"] > 50 and intent["target_direction"] == 1
    dec = w.store.latest("decisions")
    assert dec["data"]["inputs"]["book_top10"]["bids"] and dec["data"]["inputs"]["ema200"]
    assert intent["ts_ms"] <= first_order["ts"]


def test_missed_entry_window(world):
    w = world(hkt(2026, 10, 5, 9, 31))
    w.bn.signal(D1, "strong_long")
    out = w.decide()
    assert out["result"] == "missed entry window"
    assert w.pos() == 0 and not w.fok_calls()
    assert w.store.latest("decisions")["action"] == "missed"
    assert w.tg.has("missed")


def test_before_window_does_nothing(world):
    w = world(hkt(2026, 10, 5, 8, 29))
    w.bn.signal(D1, "strong_long")
    assert w.decide()["result"].startswith("before entry window")
    assert w.pos() == 0


def test_score_zero_flat_no_entry(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.decide()
    dec = w.store.latest("decisions")
    assert dec["score"] == 0.0 and dec["action"] == "none"
    assert w.pos() == 0 and not w.fok_calls()


def test_fok_not_filled_twice(world):
    w = world(hkt(2026, 10, 5, 8, 30), exits__entry_attempts=2)
    w.bn.signal(D1, "strong_long")
    w.ex.fok_outcomes.extend([False, False])
    w.decide()
    assert w.pos() == 0 and len(w.fok_calls()) == 2
    assert w.tg.has("FOK entry not filled after 2 attempts")
    w.at(hkt(2026, 10, 5, 8, 50)).decide()
    assert len(w.fok_calls()) == 2          # no third attempt today
    assert w.state()["position_state"] == "flat"


def test_fok_retry_once_then_fills(world):
    w = world(hkt(2026, 10, 5, 8, 30), exits__entry_attempts=2)
    w.bn.signal(D1, "strong_long")
    w.ex.fok_outcomes.extend([False, True])
    w.decide()
    assert w.pos() > 0 and len(w.fok_calls()) == 2
    coids = [c["coid"] for c in w.fok_calls()]
    assert len(set(coids)) == 2


def test_leverage_update_failure_blocks_trade(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.ex.update_leverage_fails = True
    w.decide()
    assert w.pos() == 0 and not w.fok_calls()
    assert w.tg.has("entry blocked")


def test_region_blocked_no_entry(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.ex.geoblock = {"blocked": True, "country": "US", "region": "NY"}
    w.decide()
    assert w.pos() == 0 and not w.fok_calls()
    assert "region blocked" in w.store.latest("decisions")["reason"]


def test_cancel_only_mode(world):
    w = world(hkt(2026, 10, 5, 8, 30), exits__entry_attempts=2)
    w.bn.signal(D1, "strong_long")
    w.ex.cancel_only = True
    w.decide()
    assert w.pos() == 0 and len(w.fok_calls()) == 1      # rejected -> no retry in the same run (review B1)
    assert w.tg.has("entry retry deferred")
    w.at(hkt(2026, 10, 5, 8, 50)).decide()
    assert w.pos() == 0 and len(w.fok_calls()) == 2      # next run re-checks, then the one retry
    assert w.tg.has("FOK entry not filled after 2 attempts")
    resp = w.store.query("SELECT data FROM orders WHERE purpose='entry' AND event='response'")
    assert all(r["data"]["restriction"] == "cancel_only" for r in resp)


def test_outcome_unknown_but_filled_is_confirmed_by_status_read(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.ex.place_timeout_after_exec = True
    w.decide()
    assert w.pos() > 0 and len(w.fok_calls()) == 1
    assert Records(w.store).open_trade() is not None


def test_crash_after_fill_is_recovered_not_reentered(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    eng = w.engine()

    def boom(*a, **k):
        raise RuntimeError("process killed")
    eng._record_entry = boom
    with pytest.raises(RuntimeError):
        eng.cmd_decide()
    assert w.pos() > 0 and Records(w.store).open_trade() is None
    w.at(hkt(2026, 10, 5, 8, 50)).decide()
    t = Records(w.store).open_trade()
    assert t is not None and t["adopted"] and not t["external"]
    assert len(w.fok_calls()) == 1


def test_ramp_switch_alert_after_ten_trades(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    rec = Records(w.store)
    for i in range(10):
        rec.record_trade("open", f"t{i}", 1, {"trade_uid": f"t{i}", "direction": 1, "live": True, "entry_ts_ms": 0,
                                               "qty": 0.01, "entry_price": P})
        rec.record_trade("close", f"t{i}", 1, {"net_pnl": 1.0})
    w.bn.signal(D1, "strong_long")
    w.decide()
    req = w.store.latest("orders", "purpose='entry' AND event='request'")["data"]
    assert req["ramp"] is False and req["risk_pct_budget"] == pytest.approx(1.5)
    assert w.tg.has("ramp complete")


def test_first_trade_uses_half_risk(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.decide()
    req = w.store.latest("orders", "purpose='entry' AND event='request'")["data"]
    assert req["ramp"] is True and req["risk_pct_budget"] == pytest.approx(0.75)
    t = Records(w.store).open_trade()
    assert t["initial_risk_usd"] == pytest.approx(10_000 * 0.0075, rel=0.02)


def test_post_trade_liquidation_check_closes(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.ex.liq_price_override = P - 3_500      # closer than 2 x SL distance (~3200)
    w.decide()
    assert w.pos() == 0
    assert w.tg.has("liquidation")
    closed = Records(w.store).closed_trades()
    assert closed and closed[0]["exit_reason"] == "liq_check"


# ------------------------------------------------------------------ reconcile / protection
def test_sl_missing_is_replaced(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    for o in active(w, "sl"):
        o.status = "cancelled"
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    sls = active(w, "sl")
    assert len(sls) == 1 and sls[0].tpsl_scope == "position"
    assert w.tg.has("SL missing") and w.tg.has("SL re-placed")
    assert w.pos() > 0


def test_sl_missing_and_replace_fails_closes(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    for o in active(w, "sl"):
        o.status = "cancelled"
    w.ex.position_tpsl_fails = True
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert w.pos() == 0
    assert w.tg.has("SL re-place FAILED")
    closes = [c[1] for c in w.ex.calls if c[0] == "place_order" and c[1]["reduce_only"]]
    assert closes and all(c["tif"] == "ioc" for c in closes)
    assert Records(w.store).closed_trades()[0]["exit_reason"] == "protection_failure"


def test_sl_missing_and_mark_beyond_sl_closes(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    t = enter_long(w)
    for o in active(w, "sl"):
        o.status = "cancelled"
    w.ex.set_mark(t["sl_price"] - 100, trigger=False)
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert w.pos() == 0


def test_close_failure_alerts(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    for o in active(w, "sl"):
        o.status = "cancelled"
    w.ex.position_tpsl_fails = True
    w.ex.reduce_only_fails = True
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert w.pos() > 0
    assert w.tg.has("CLOSE FAILURE")


def test_intraday_tp_close_is_booked(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    t = enter_long(w)
    w.clock.advance(hours=3)
    w.ex.set_mark(t["tp_price"] + 1)
    assert w.pos() == 0
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    c = Records(w.store).closed_trades()[0]
    assert c["exit_reason"] == "TP" and c["net_pnl"] > 0 and c["r_multiple"] > 1.5
    assert c["fees_total"] > 0
    assert w.tg.has("closed (TP)")
    # next entry only at the next 08:30
    w.bn.signal(D1, "strong_long")
    w.at(hkt(2026, 10, 5, 12, 31)).manage()
    assert w.pos() == 0


def test_intraday_sl_close_and_no_reentry_same_day(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    t = enter_long(w)
    w.ex.set_mark(t["sl_price"] - 1)
    w.at(hkt(2026, 10, 5, 8, 50)).decide()
    c = Records(w.store).closed_trades()[0]
    assert c["exit_reason"] == "SL" and c["net_pnl"] < 0
    assert w.pos() == 0 and len(w.fok_calls()) == 1


def test_leftover_orders_cancelled_by_id(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    t = enter_long(w)
    w.ex.auto_cancel_leftovers = False
    w.ex.oco_brackets = False               # v2.0.0 mock: brackets are OCO by default
    w.ex.set_mark(t["sl_price"] - 1)
    assert len(active(w, "tp")) == 1        # exchange left the TP behind
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert not active(w, "tp")
    cancels = [c[1]["ids"] for c in w.ex.calls if c[0] == "cancel_orders"]
    assert cancels and all(isinstance(i, int) for ids in cancels for i in ids)


def test_cumulative_funding_recorded(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.clock.advance(hours=1)
    w.ex.apply_funding(0.0001)
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    snap = w.store.latest("position_snapshots")
    assert snap["cumulative_funding"] < 0
    assert w.store.count("funding_payments") == 1


# ------------------------------------------------------------------ flips and rules
def test_flip_in_two_steps(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.bn.signal(D2, "strong_short")
    w.at(hkt(2026, 10, 6, 8, 30)).decide()
    assert w.pos() < 0
    calls = [c for c in w.ex.calls if c[0] == "place_order"]
    kinds = [(c[1]["tif"], c[1]["reduce_only"]) for c in calls]
    assert kinds[-2:] == [("ioc", True), ("fok", False)]      # reduce-only IOC close, then FOK bracket entry
    closed = Records(w.store).closed_trades()[0]
    assert closed["exit_reason"] == "flip"
    assert w.tg.has("flip: LONG -> SHORT")
    assert len(active(w, "sl")) == 1 and active(w, "sl")[0].side == "BUY"


def test_flip_interrupted_halfway_completed_by_0850(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.bn.signal(D2, "strong_short")
    w.ex.raise_on["get_account_config"] = RuntimeError("crash between close and entry")
    with pytest.raises(RuntimeError):
        w.at(hkt(2026, 10, 6, 8, 30)).decide()
    assert w.pos() == 0                       # closed, not re-entered
    w.at(hkt(2026, 10, 6, 8, 50)).decide()
    assert w.pos() < 0
    assert len(w.fok_calls()) == 2


def test_flip_interrupted_then_manage_stays_flat(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.bn.signal(D2, "strong_short")
    w.ex.raise_on["get_account_config"] = RuntimeError("crash")
    with pytest.raises(RuntimeError):
        w.at(hkt(2026, 10, 6, 8, 30)).decide()
    w.at(hkt(2026, 10, 6, 12, 30)).manage()
    assert w.pos() == 0 and len(w.fok_calls()) == 1
    assert w.tg.has("entry missed")


def test_weak_opposite_holds(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    t = enter_long(w)
    w.bn.signal(D2, "weak_short")
    w.at(hkt(2026, 10, 6, 8, 30)).decide()
    assert w.pos() > 0
    assert Records(w.store).open_trade()["sl_price"] == t["sl_price"]
    assert w.store.latest("decisions")["action"] == "hold"


def test_three_day_rule(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    for day, hh in ((D2, 6), (D3, 7)):
        w.bn.signal(day, "weak_short")
        w.at(hkt(2026, 10, hh, 8, 30)).decide()
        assert w.pos() > 0
    w.bn.signal(D4, "weak_short")
    w.at(hkt(2026, 10, 8, 8, 30)).decide()
    assert w.pos() < 0
    assert Records(w.store).closed_trades()[0]["exit_reason"] == "3day_rule"
    plan = w.store.latest("intents")["data"]
    assert plan["enter_fraction"] == 0.25


def test_funding_rule_closes_and_reenters_per_gates(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.bn.signal(D2, "weak_short")
    w.bn.set_funding(D2, 0.01)            # extreme high funding: longs are crowded
    w.at(hkt(2026, 10, 6, 8, 30)).decide()
    assert w.pos() < 0
    assert Records(w.store).closed_trades()[0]["exit_reason"] == "funding_rule"


def test_funding_gate_blocks_new_long(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.bn.set_funding(D1, 0.01)
    w.decide()
    assert w.pos() == 0
    assert "extreme funding" in w.store.latest("decisions")["reason"]


def test_event_window_blocks_entry_and_flip(world):
    events = [{"type": "CPI", "date": "2026-10-06", "time": "08:30"}]
    w = world(hkt(2026, 10, 5, 8, 30), events=events)
    enter_long(w)
    w.bn.signal(D2, "strong_short")
    w.at(hkt(2026, 10, 6, 8, 30)).decide()
    assert w.pos() > 0                                    # flip suppressed
    assert "flip suppressed" in w.store.latest("decisions")["reason"]
    w.bn.signal(D3, "strong_short")
    w.at(hkt(2026, 10, 7, 8, 30)).decide()
    assert w.pos() < 0                                    # window over -> flip


def test_event_window_blocks_new_position(world):
    w = world(hkt(2026, 12, 10, 8, 30), events=[{"type": "CPI", "date": "2026-12-10", "time": "08:30"}])
    w.bn.signal(date(2026, 12, 10), "strong_long")
    w.decide()
    assert w.pos() == 0
    assert "event window" in w.store.latest("decisions")["reason"]


# ------------------------------------------------------------------ kill switches and commands
def test_drawdown_kill_switch_closes_and_pauses(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.ex.cash -= 1_800                        # equity -18% vs peak (above the 75% equity floor)
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert w.pos() == 0
    st = w.state()
    assert st["paused"] and "kill_drawdown" in st["pause_reasons"]
    assert w.tg.has("KILL SWITCH: drawdown")
    w.bn.signal(D2, "strong_long")
    w.at(hkt(2026, 10, 6, 8, 30)).decide()
    assert w.pos() == 0 and len(w.fok_calls()) == 1
    w.engine().cmd_resume(reset_peak=True)
    assert not w.state()["paused"]
    w.at(hkt(2026, 10, 6, 8, 50)).decide()
    assert w.pos() > 0                        # resumed inside the window -> fresh decision


def test_losing_streak_kill_switch_keeps_position(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    rec = Records(w.store)
    for i in range(3):
        rec.record_trade("open", f"L{i}", 1, {"trade_uid": f"L{i}", "direction": 1, "live": True, "entry_ts_ms": 0,
                                               "qty": 0.01, "entry_price": P})
        rec.record_trade("close", f"L{i}", 1, {"net_pnl": -800.0, "equity_at_entry": 10_000.0})
    n_calls = len(w.ex.calls)
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    st = w.state()
    assert st["paused"] and "kill_losing_streak" in st["pause_reasons"]
    assert w.pos() > 0 and len(active(w, "sl")) == 1
    assert not [c for c in w.ex.calls[n_calls:] if c[0] == "place_order"]
    assert w.tg.has("KILL SWITCH: losing streak")


def test_pause_resume_kill_engine_commands(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.engine().cmd_pause()
    w.bn.signal(D1, "strong_long")
    w.decide()
    assert w.pos() == 0 and w.store.latest("decisions")["action"] == "paused"
    w.engine().cmd_resume()
    w.at(hkt(2026, 10, 5, 8, 50)).decide()
    assert w.pos() > 0
    w.engine().cmd_kill()
    assert w.pos() == 0 and w.state()["paused"]
    assert Records(w.store).closed_trades()[0]["exit_reason"] == "manual"


def test_telegram_commands(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.tg.push("/status")
    w.tg.push("/pause", chat="999")           # wrong chat: ignored
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert any(m.startswith("state:") for m in w.tg.sent)
    assert not w.state()["paused"]
    w.tg.push("/pause")
    w.at(hkt(2026, 10, 5, 16, 30)).manage()
    assert w.state()["paused"] and w.pos() > 0
    w.tg.push("/kill")
    w.at(hkt(2026, 10, 5, 20, 30)).manage()
    assert w.pos() == 0 and "manual_kill" in w.state()["pause_reasons"]
    n = len(w.tg.sent)
    w.at(hkt(2026, 10, 5, 20, 31)).manage()   # already processed updates are not re-run
    assert not any("killed" in m for m in w.tg.sent[n:])


def test_key_expiry_alert(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.secrets.proxy_expires_at = w.clock.now() + timedelta(days=4)
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert w.tg.has("proxy signer key expires")


def test_expectancy_warning(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    rec = Records(w.store)
    for i in range(25):
        rec.record_trade("open", f"e{i}", 1, {"trade_uid": f"e{i}", "direction": 1, "live": True, "entry_ts_ms": 0,
                                               "qty": 0.01, "entry_price": P, "initial_risk_usd": 10.0})
        rec.record_trade("close", f"e{i}", 1, {"net_pnl": 5.0 if i % 3 else -10.0, "equity_at_entry": 10_000.0})
    w.engine().cmd_resume()                   # isolate from the losing-streak switch
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert w.tg.has("expectancy")


def test_equity_crosscheck_uses_total_account_value(world, monkeypatch):
    w = world(hkt(2026, 10, 5, 8, 30))
    eng = w.engine()
    acct = w.ex.get_account()
    acct.total_account_value = acct.total_account_value + 2_000
    eq = eng.equity(acct)
    assert eq["source"] == "total_account_value" and eq["equity"] == pytest.approx(12_000)
    assert w.tg.has("equity cross-check")


def test_reports_generate(world, tmp_path):
    from perpbot.paths import Paths

    w = world(hkt(2026, 10, 5, 8, 30))
    t = enter_long(w)
    w.ex.set_mark(t["tp_price"] + 1)
    w.at(hkt(2026, 10, 5, 8, 45))
    paths = Paths(tmp_path / "root")
    paths.ensure()
    rep = Reporter(w.engine(), paths)
    text = rep.daily()
    assert "score" in text and "Gates" in text
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    text = Reporter(w.engine(), paths).weekly()
    assert "Trades: 1" in text and w.tg.docs[-1].endswith(".zip")
    out = Reporter(w.engine(), paths).monthly("2026-10")
    md = (paths.reports_dir / "monthly" / "monthly_2026-10.md").read_text(encoding="utf-8")
    assert "by_exit_reason" in md and f"code test / config {w.cfg.config_version}" in md and "shadow_vs_live" in md
    assert "monthly report" in out
