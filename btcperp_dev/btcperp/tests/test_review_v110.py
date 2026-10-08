"""Tests for the v1.1.0 review items (numbering follows REVIEW_v1.1.0.md)."""

import random
from datetime import date, timedelta

import pytest

from perpbot.exchange.base import Flow
from perpbot.indicators import ema
from perpbot.records import Records
from perpbot.timeutil import DAY_MS, to_ms

from conftest import P, hkt

D1, D2 = date(2026, 10, 5), date(2026, 10, 6)


def active(w, kind):
    return [o for o in w.ex.orders.values() if o.tpsl_kind == kind and o.status in ("armed", "untriggered")]


def enter_long(w):
    w.bn.signal(D1, "strong_long")
    w.at(hkt(2026, 10, 5, 8, 30)).decide()
    assert w.pos() > 0
    return Records(w.store).open_trade()


# ---------------------------------------------------------------- B1
def test_b1_entry_fills_but_sl_row_rejected_no_second_entry(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.ex.reject_sl_row = True                             # entry row fills, SL row rejected, SDK reports rejection
    w.decide()
    assert w.pos() > 0 and len(w.fok_calls()) == 1        # no second order
    assert Records(w.store).open_trade() is not None
    sls = active(w, "sl")
    assert len(sls) == 1 and sls[0].tpsl_scope == "position"   # protection re-placed as a position SL


def test_b1_unprotectable_sl_closes_single_entry(world, monkeypatch):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    import perpbot.engine as eng_mod

    real = eng_mod.bracket_prices
    monkeypatch.setattr(eng_mod, "bracket_prices", lambda d, ref, atr, s, t: (ref * 1.2, real(d, ref, atr, s, t)[1]))
    w.decide()                                            # SL price itself invalid -> cannot protect -> close
    assert len(w.fok_calls()) == 1 and w.pos() == 0
    assert Records(w.store).closed_trades()[0]["exit_reason"] in ("protection_failure", "liq_check")


def test_b1_rejected_and_stale_position_read_no_retry(world):
    w = world(hkt(2026, 10, 5, 8, 30), exits__entry_attempts=2)
    w.bn.signal(D1, "strong_long")
    w.ex.reject_sl_row = True
    w.ex.hide_orders_from_status = True
    orig_place = w.ex.place_order

    def place_then_lag(**kw):
        r = orig_place(**kw)
        w.ex.lag_position_reads = 1                       # the first read after the order is stale
        return r
    w.ex.place_order = place_then_lag
    w.decide()
    assert len(w.fok_calls()) == 1 and w.pos() > 0        # no retry; the late read recorded + protected it
    t = Records(w.store).open_trade()
    assert t is not None and len(active(w, "sl")) == 1


def test_b1_rejected_and_long_stale_read_defers_to_next_run(world):
    w = world(hkt(2026, 10, 5, 8, 30), exits__entry_attempts=2)
    w.bn.signal(D1, "strong_long")
    w.ex.reject_sl_row = True
    w.ex.hide_orders_from_status = True
    orig_place = w.ex.place_order

    def place_then_lag(**kw):
        r = orig_place(**kw)
        w.ex.lag_position_reads = 2
        return r
    w.ex.place_order = place_then_lag
    w.decide()
    assert len(w.fok_calls()) == 1 and w.tg.has("entry retry deferred")
    w.ex.place_order = orig_place
    w.ex.hide_orders_from_status = False
    w.at(hkt(2026, 10, 5, 8, 50)).decide()
    assert len(w.fok_calls()) == 1 and w.pos() > 0        # next run finds the position: recovered, no 2nd entry
    t = Records(w.store).open_trade()
    assert t is not None and not t["external"] and len(active(w, "sl")) == 1


def test_b1_true_fok_unfilled_still_retries_once(world):
    w = world(hkt(2026, 10, 5, 8, 30), exits__entry_attempts=2)
    w.bn.signal(D1, "strong_long")
    w.ex.fok_outcomes.extend([False, True])
    w.decide()
    assert w.pos() > 0 and len(w.fok_calls()) == 2


# ---------------------------------------------------------------- B2
def test_b2_kill_close_retried_every_run(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.ex.cash -= 1_800
    w.ex.reduce_only_fails = True
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert w.pos() > 0 and "kill_drawdown" in w.state()["pause_reasons"]
    assert w.tg.has("CLOSE FAILURE")
    w.ex.reduce_only_fails = False
    w.at(hkt(2026, 10, 5, 16, 30)).manage()
    assert w.pos() == 0
    assert w.tg.has("kill close retry")


def test_b2_manual_kill_close_retried(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.ex.reduce_only_fails = True
    w.engine().cmd_kill()
    assert w.pos() > 0
    w.ex.reduce_only_fails = False
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert w.pos() == 0 and "manual_kill" in w.state()["pause_reasons"]


# ---------------------------------------------------------------- C4
def test_c4_partial_order_sl_gets_position_sl(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    sl = active(w, "sl")[0]
    sl.quantity = sl.quantity / 2
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    scopes = sorted(o.tpsl_scope for o in active(w, "sl"))
    assert scopes == ["order", "position"]
    assert w.tg.has("stop-loss covers only")


# ---------------------------------------------------------------- C5
def test_c5_one_stale_read_books_and_cancels_nothing(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.ex.lag_position_reads = 1
    n_cancel = len([c for c in w.ex.calls if c[0] == "cancel_orders"])
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert Records(w.store).open_trade() is not None
    assert not Records(w.store).closed_trades()
    assert len([c for c in w.ex.calls if c[0] == "cancel_orders"]) == n_cancel
    assert len(active(w, "sl")) == 1 and len(active(w, "tp")) == 1


def test_c5_real_close_still_booked(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    t = enter_long(w)
    w.ex.set_mark(t["sl_price"] - 1)
    w.ex.hide_fills = True                                # no exit fills visible: needs two flat reads
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert Records(w.store).closed_trades()


# ---------------------------------------------------------------- C6
def test_c6_kill_switch_rechecked_between_flip_close_and_entry(world):
    w = world(hkt(2026, 10, 5, 8, 30), risk__kill_losing_streak_pct=5.0)
    t = enter_long(w)
    rec = Records(w.store)
    for i in range(2):
        rec.record_trade("open", f"L{i}", 1, {"trade_uid": f"L{i}", "direction": 1, "live": True, "entry_ts_ms": 0,
                                               "qty": 0.01, "entry_price": P})
        rec.record_trade("close", f"L{i}", 1, {"net_pnl": -225.0, "equity_at_entry": 10_000.0})
    w.ex.set_mark(t["entry_price"] - 55.0 / t["qty"], trigger=False)
    w.bn.signal(D2, "strong_short")
    w.at(hkt(2026, 10, 6, 8, 30)).decide()
    assert w.pos() == 0                                   # closed, but no short opened
    assert "kill_losing_streak" in w.state()["pause_reasons"]
    assert len(w.fok_calls()) == 1


# ---------------------------------------------------------------- C7
def test_c7_ema200_on_1000_candles_matches_full_history(cfg):
    rnd = random.Random(7)
    closes, px = [], 20_000.0
    for _ in range(3000):
        px *= 1 + rnd.gauss(0.0008, 0.03)
        closes.append(px)
    full = ema(closes, 200)[-1]
    short = ema(closes[-cfg.binance.daily_candles_to_load:], 200)[-1]
    assert abs(short - full) / full < 0.001
    assert cfg.binance.daily_candles_to_load == 1000


# ---------------------------------------------------------------- C8
def test_c8_sources_disagree_blocks_entries_but_not_kill(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    orig = w.ex.get_account

    def skewed():
        a = orig()
        a.balances[0].value -= 1_000                      # wallet excludes something -> sources disagree
        return a
    w.ex.get_account = skewed
    w.bn.signal(D1, "strong_long")
    w.decide()
    assert w.pos() == 0 and w.tg.has("equity sources disagree")
    row = w.store.latest("equity_log")
    assert row["data"]["source"] == "total_account_value" and row["data"]["kill"]["evaluated"]


def test_c8_unreadable_equity_skips_kill(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    orig = w.ex.get_account

    def broken():
        a = orig()
        a.total_account_value = 0.0
        return a
    w.ex.get_account = broken
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert w.pos() > 0 and not w.state()["paused"]
    assert w.tg.has("equity unreadable")
    assert w.store.latest("equity_log")["data"]["kill"]["evaluated"] is False


# ---------------------------------------------------------------- C9 / E flows
def test_c9_pending_withdrawal_skips_drawdown_and_blocks_entry(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.ex.flows.append(Flow("w1", "withdrawal", 2_000.0, "pending", to_ms(w.clock.now())))
    w.ex.cash -= 2_000
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert w.pos() > 0 and not w.state()["paused"]
    assert w.tg.has("pending deposit/withdrawal") and w.tg.has("while a position is open")
    w.ex.flows[0] = Flow("w1", "withdrawal", 2_000.0, "confirmed", to_ms(w.clock.now()))
    w.at(hkt(2026, 10, 5, 16, 30)).manage()
    assert not w.state()["paused"]                        # peak and funded capital adjusted, no false kill
    row = w.store.latest("equity_log")
    assert row["peak"] < 8_200 and row["data"]["net_funded"] == pytest.approx(8_000, rel=0.01)


# ---------------------------------------------------------------- D10
def test_d10_missing_liquidation_price_closes(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.ex.liq_price_override = 0.0
    w.bn.signal(D1, "strong_long")
    w.decide()
    assert w.pos() == 0 and w.tg.has("is missing or closer")


# ---------------------------------------------------------------- D12
def test_d12_adopted_position_day_ids_and_budget(world):
    w = world(hkt(2026, 10, 3, 8, 0))
    w.ex.place_order(instrument_id=1, side="BUY", quantity="0.2000", tif="ioc", price=None, reduce_only=False,
                     client_order_id="f" * 32)            # external 20k notional position, no SL
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    t = Records(w.store).open_trade()
    assert t["entry_utc_day"] == "2026-10-03"
    assert "adopted_over_budget" in w.state()["pause_reasons"]
    assert w.tg.has("over risk budget")


# ---------------------------------------------------------------- D13
def test_d13_unknown_sl_replace_is_rechecked_not_closed(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    for o in active(w, "sl"):
        o.status = "cancelled"
    w.ex.position_tpsl_unknown = True
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    assert w.pos() > 0 and len(active(w, "sl")) == 1
    assert w.tg.has("SL re-placed") and not w.tg.has("SL re-place FAILED")


# ---------------------------------------------------------------- D14
def test_d14_store_redacts_secrets(tmp_path):
    from datetime import datetime

    from perpbot.logging_setup import RedactFilter
    from perpbot.storage import Store
    from perpbot.timeutil import UTC, FixedClock

    s = Store(tmp_path / "r.db", FixedClock(datetime(2026, 10, 5, tzinfo=UTC)), "x", "x")
    s.redact = RedactFilter(["my-proxy-secret-xyz"]).redact
    s.insert("runs", command="decide", event="end", error="boom my-proxy-secret-xyz", data={"e": "my-proxy-secret-xyz"})
    assert s.count("runs", "error = 'boom ***'") == 1                # the row was written, redacted
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("r.db*"))    # db + WAL (rows live in the WAL first)
    assert b"my-proxy-secret-xyz" not in raw
    s.close()
    assert b"my-proxy-secret-xyz" not in (tmp_path / "r.db").read_bytes()


# ---------------------------------------------------------------- D16
def test_d16_shadow_r_includes_funding_and_slippage(cfg):
    from perpbot.shadow import SimTrade, _r, _slipped

    t = SimTrade("x", "2026-10-05", 1, 1.0, 100.0, 0, 2.0, 97.0, 106.0, exit_price=106.0, exit_ts_ms=10 * DAY_MS)
    no_fund = _r(t, 1.5, 0.0, [])
    with_fund = _r(t, 1.5, 0.0, [(DAY_MS, 0.001), (2 * DAY_MS, 0.001)])
    assert no_fund == pytest.approx(2.0) and with_fund == pytest.approx(2.0 - 0.2 / 3.0)
    assert _slipped(100.0, 1, cfg) == pytest.approx(100.1) and _slipped(100.0, -1, cfg) == pytest.approx(99.9)


# ---------------------------------------------------------------- E equity floor
def test_e_equity_floor_hard_stop_needs_new_config_version(world, cfg_dict, tmp_path):
    w = world(hkt(2026, 10, 5, 8, 30))
    enter_long(w)
    w.ex.cash -= 2_600                                    # -26%: below 75% of funded capital
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    st = w.state()
    assert w.pos() == 0 and "equity_floor" in st["pause_reasons"]
    assert w.engine().cmd_resume(reset_peak=True) == "equity_floor still active"
    assert w.state()["pause_reasons"] == ["equity_floor"]
    from perpbot.config import config_from_dict

    cfg_dict["config_version"] = "1.1.1-test"
    w.cfg = config_from_dict(cfg_dict)
    w.store.config_version = "1.1.1-test"
    assert w.engine().cmd_resume(reset_peak=True) == "equity_floor still active"      # v1.3.0: new version alone is not enough
    assert w.state()["pause_reasons"] == ["equity_floor"]
    cfg_dict["config_version"] = "1.1.2-test"
    cfg_dict["risk"]["equity_floor_reset_baseline_usd"] = 7_400        # owner states the new funded baseline ...
    w.cfg = config_from_dict(cfg_dict)
    w.store.config_version = "1.1.2-test"
    assert w.engine().cmd_resume(reset_peak=True) == "equity_floor still active"   # ... but not the trigger date
    cfg_dict["risk"]["equity_floor_reset_baseline_usd"] = 50_000        # above current equity: refused
    cfg_dict["risk"]["equity_floor_reset_for"] = "2026-10-05"
    w.cfg = config_from_dict(cfg_dict)
    assert w.engine().cmd_resume(reset_peak=True) == "equity_floor still active"
    cfg_dict["risk"]["equity_floor_reset_baseline_usd"] = 7_000
    w.cfg = config_from_dict(cfg_dict)
    assert w.engine().cmd_resume(reset_peak=True) == "resumed"
    assert not w.state()["paused"]
    eq = w.store.latest("equity_log")
    assert eq["data"]["net_funded"] == 7_000 and eq["data"]["floor_reset"] is True
    assert eq["data"]["floor_reset_for"] == "2026-10-05" and eq["data"]["cum_funded"] == pytest.approx(10_000, rel=0.01)
    w.at(hkt(2026, 10, 5, 16, 30)).manage()
    assert not w.state()["paused"]                        # new funded baseline, no immediate re-trigger


def test_e_parameters_decided_by_the_owner():
    """Review E parameters: confirmed by the owner 2026-09-30; on 2026-10-02 the owner chose 5% risk (v1.5.8) with a
    150% notional cap, then bold mode (v1.7.0): drawdown / losing-streak kills at 95% (effectively off) and both
    floors at 5%, lowered once in config 1.7.0."""
    from conftest import shipped_config
    from perpbot.config import config_from_dict

    cfg = config_from_dict(shipped_config())
    assert cfg.risk.notional_cap_pct_equity == 150
    assert cfg.risk.kill_losing_streak_pct == 95 and cfg.risk.kill_drawdown_pct == 95
    assert cfg.risk.equity_floor_pct_of_net_funded == 5
    assert cfg.risk.permanent_floor_pct_of_cumulative_funded == 5
    assert cfg.risk.permanent_floor_lowered_in == "1.10.0" and cfg.config_version == "2.3.0"   # v2.x keeps it
    text = (__import__("pathlib").Path(__file__).resolve().parent.parent / "config" / "config.yaml").read_text(encoding="utf-8")
    assert "PENDING USER DECISION" not in text and "committee" not in text


# ---------------------------------------------------------------- smoketest (A2, D17, G1, G3, G7)
def test_smoketest_new_steps_with_mock(world, tmp_path):
    from perpbot.paths import Paths
    from perpbot.smoketest import flowwatch, run_smoketest

    w = world(hkt(2026, 10, 5, 3, 0))
    w.ex.proxy_info = type(w.ex.proxy_info)("0xOWNER", "0xP", to_ms(w.clock.now() + timedelta(days=30)))
    paths = Paths(tmp_path / "root")
    paths.ensure()
    calls_before = len(w.ex.calls)
    ok, res = run_smoketest(w.engine(), paths)
    by = {r["step"]: r for r in res}
    assert ok, [r for r in res if r["ok"] is False]
    assert by["short_and_flip"]["ok"] and by["short_and_flip"]["detail"]["short_filled"]
    assert by["g1_bracket_partial_reject"]["detail"]["answer_g1"].startswith("entry row FILLED")
    assert by["close"]["detail"]["g3_account_read_after_close"]["flat"]
    assert "not tested live" in by["a_proxy_withdraw"]["detail"]["answer_a"]
    assert w.pos() == 0 and not [o for o in w.ex.orders.values() if o.is_active]
    assert len(w.ex.calls) > calls_before
    ok2, res2 = run_smoketest(w.engine(), paths, probe_withdrawal=True)
    assert "NO (rejected" in {r["step"]: r for r in res2}["a_proxy_withdraw"]["detail"]["answer_a"]
    out = flowwatch(w.engine(), paths, minutes=0.5, interval=15)
    assert out.exists()
