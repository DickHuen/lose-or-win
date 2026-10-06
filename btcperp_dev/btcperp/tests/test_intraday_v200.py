"""v2.0.0 intraday (owner 2026-10-06): closed-candle structure for direction, symmetric long / short, structure stops,
partial target + break-even + trail, invalidation / time / no-progress exits, a cost gate from the real book and the
account's fee rate, the incremental candle cache with integrity checks and Retry-After, one 15-minute task, the
backtest using the live functions, and upgrades that keep an older position untouched."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from perpbot import costs
from perpbot import intraday as idy
from perpbot import intraday_bt as ibt
from perpbot.candles import CandleCache, check
from perpbot.config import ConfigError, config_from_dict
from perpbot.datasources.binance import BinanceData, RateLimited
from perpbot.exchange.base import Book
from perpbot.indicators import Candle
from perpbot.records import Records

from conftest import shipped_intraday_config
from intraday_helpers import (H1, H4, M15, T0, IWorld, IntradayBinance, agg, background, bars_from_points, mirror,
                              split5)

I0 = 1440                                       # 15 days of sideways warm-up before the scripted pattern
UP = background(I0) + [(I0 + 12, 83_000), (I0 + 28, 84_500), (I0 + 44, 83_600), (I0 + 76, 86_600), (I0 + 88, 85_100),
                       (I0 + 89, 85_300)]
T_LONG = T0 + (I0 + 89) * M15                   # the 15m candle closing here turns up after a 50% pullback


def cfg_dict():
    return shipped_intraday_config()


def path(*tail):
    return bars_from_points(UP + list(tail))


def at(i):
    return T0 + (I0 + i) * M15


def p_cfg(**over):
    d = cfg_dict()
    d["intraday"].update(over)
    return idy.Params.from_cfg(config_from_dict(d))


def evaluate(m15, t, cost=lambda d, e: e * 0.0014, hist=None, p=None):
    return idy.evaluate(p or p_cfg(), t, m15, agg(m15, H1), agg(m15, H4), cost_unit_fn=cost,
                        history=hist or idy.History())


# ================================================================ config / schedule
def test_shipped_config_is_intraday_with_the_owner_risk_settings():
    cfg = config_from_dict(cfg_dict())
    assert cfg.config_version == "2.0.0" and cfg.intraday.enabled is True
    assert cfg.schedule.decide_times_hkt == [] and cfg.schedule.manage_times_hkt == []
    assert (cfg.schedule.intraday_every_minutes, cfg.schedule.intraday_offset_minutes) == (15, 1)
    r, s = cfg.risk, cfg.strategy                 # unchanged owner settings
    assert (r.notional_multiple_full_tier, r.leverage, r.max_margin_use_pct, r.liq_min_sl_multiple) == (20, 25, 92, 1.3)
    assert s.min_entry_abs_score == 20 and s.size_tiers == [[0, 0.15], [40, 0.30], [50, 0.50], [75, 1.00]]
    assert (r.kill_drawdown_pct, r.kill_losing_streak_pct, r.equity_floor_pct_of_net_funded,
            r.permanent_floor_pct_of_cumulative_funded) == (95, 95, 5, 5)
    ic = cfg.intraday
    assert (ic.max_cost_r, ic.tp1_r, ic.tp2_r, ic.tp1_fraction, ic.max_hold_hours) == (0.20, 1.0, 3.0, 0.5, 12)


@pytest.mark.parametrize("key,value", [("retrace_min", 0.9), ("tp2_r", 0.5), ("max_cost_r", 1.5), ("cost_book_depth", 7),
                                       ("no_progress_hours", 99), ("cache_backfill_days", 5)])
def test_intraday_config_is_validated(key, value):
    d = cfg_dict()
    d["intraday"][key] = value
    with pytest.raises(ConfigError):
        config_from_dict(d)


def test_intraday_refuses_the_old_hourly_task_list():
    d = cfg_dict()
    d["schedule"]["decide_times_hkt"] = ["00:30"]
    with pytest.raises(ConfigError, match="must be empty while intraday.enabled"):
        config_from_dict(d)


def test_one_repeating_task_replaces_the_hourly_tasks(tmp_path, monkeypatch):
    from datetime import datetime

    from perpbot import winsched

    cfg = config_from_dict(cfg_dict())
    specs = winsched.plan(cfg)
    decide = [s for s in specs if s.args == "decide"]
    assert [s.name for s in decide] == ["decide_15m"] and not [s for s in specs if s.args == "manage"]
    xml = winsched.task_xml(decide[0], tmp_path, tmp_path / "py.exe", 8.0, datetime(2026, 10, 6, 14, 7))
    assert "<Interval>PT15M</Interval>" in xml and "<Duration>P1D</Duration>" in xml
    assert "<StartBoundary>2026-10-06T14:16:00+08:00</StartBoundary>" in xml       # the next slot, not tomorrow
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml and "PT13M" in xml
    # registration: the old decide_HHMM / manage_HHMM tasks are deleted only after the new one registered
    calls = []
    monkeypatch.setattr(winsched, "_live", lambda: True)
    monkeypatch.setattr(winsched, "write_marker", lambda root: None)
    monkeypatch.setattr(winsched, "registered_names",
                        lambda: ["\\btcperp\\decide_0030", "\\btcperp\\manage_0210", "\\btcperp\\decide_15m"])

    def run(args):
        calls.append(args)
        return 0, "ok"
    monkeypatch.setattr(winsched, "_run", run)
    winsched.install(cfg, tmp_path, tmp_path / "tasks", dry_run=False)
    deleted = [a[3] for a in calls if a[:2] == ["schtasks", "/Delete"]]
    assert deleted == ["\\btcperp\\decide_0030", "\\btcperp\\manage_0210"]
    calls.clear()
    monkeypatch.setattr(winsched, "_run", lambda args: calls.append(args) or ((1, "bad xml") if "/Create" in args
                                                                               and "decide_15m" in args[3] else (0, "")))
    res = winsched.install(cfg, tmp_path, tmp_path / "tasks", dry_run=False)
    assert not [a for a in calls if a[:2] == ["schtasks", "/Delete"]]                 # old schedule kept
    assert any("KEPT" in r["output"] for r in res)


def test_audit_counts_every_15_minute_slot():
    from datetime import date

    from perpbot.schedule_audit import intraday_times, slots_for_day

    cfg = config_from_dict(cfg_dict())
    times = intraday_times(cfg)
    assert len(times) == 96 and times[:3] == ["00:01", "00:16", "00:31"] and times[-1] == "23:46"
    assert sum(1 for c, _ in slots_for_day(cfg, date(2026, 10, 6)) if c == "decide") == 96


# ================================================================ signal rules
def test_long_continuation_after_a_pullback():
    m15 = path((I0 + 120, 87_400))
    d = evaluate(m15, T_LONG)
    assert d.action == "enter" and d.direction == 1 and d.setup == "continuation"
    assert d.structure["trend"] == 1
    assert d.stop < d.entry_ref < d.tp1 < d.tp2 and d.r == pytest.approx(d.entry_ref - d.stop)
    assert d.cost_r <= 0.2 + 1e-9 and d.room >= d.r
    assert set(d.components) == {"setup", "trend", "context", "room", "cost"} and 20 <= d.score <= 100


def test_short_rules_are_the_exact_mirror_of_long():
    """Owner: 多空條件必須對稱. Reflect every price around a center (high <-> low): every decision flips direction
    and keeps setup, distance and score (price-level rules off: sl_min_pct 0, constant costs)."""
    m15 = path((I0 + 120, 87_400), (I0 + 160, 85_000))
    mm = mirror(m15, 85_000.0)
    p = p_cfg(sl_min_pct=0.0)
    n = 0
    for i in range(40, 160):
        a = evaluate(m15, at(i), cost=lambda d, e: 100.0, p=p)
        b = evaluate(mm, at(i), cost=lambda d, e: 100.0, p=p)
        assert (a.action, a.setup, a.direction) == (b.action, b.setup, -b.direction), i
        if a.action == "enter":
            n += 1
            assert a.r == pytest.approx(b.r) and a.score == pytest.approx(b.score)
            assert a.stop - a.entry_ref == pytest.approx(-(b.stop - b.entry_ref))
    assert n >= 1


def test_four_hour_background_never_blocks_a_short():
    """The 4h background is up (a long rally before), yet the 1h structure turned down: a short is allowed (score
    smaller, never blocked)."""
    m15 = path((I0 + 120, 87_400))
    mm = mirror(m15, 85_000.0)
    d = evaluate(mm, T_LONG)
    assert d.action == "enter" and d.direction == -1
    assert d.components["context"] in (0.0, 10.0, 25.0)


def test_range_means_no_trade():
    m15 = bars_from_points(background(I0 + 200))
    for i in range(0, 120, 7):
        d = evaluate(m15, at(i))
        assert d.action == "none"


def test_no_chasing_same_leg_and_cooldown():
    m15 = path((I0 + 120, 87_400))
    first = evaluate(m15, T_LONG)
    assert first.action == "enter"
    again = evaluate(m15, T_LONG, hist=idy.History(traded_legs={first.leg_id}))
    assert again.action == "none" and any("already traded" in s["reason"] for s in again.setups)
    cool = evaluate(m15, T_LONG, hist=idy.History(last_exit_ms=T_LONG - M15))
    assert cool.action == "none" and any("cooldown" in s["reason"] for s in cool.setups)
    full = evaluate(m15, T_LONG, hist=idy.History(entries_today=6))
    assert full.action == "none"


def test_costs_too_high_for_the_stop_means_no_entry_and_the_reason_is_kept():
    m15 = path((I0 + 120, 87_400))
    d = evaluate(m15, T_LONG, cost=lambda dd, e: e * 0.01)            # 1% round trip: needs a 5% stop -> refused
    assert d.action == "none"
    assert any("beyond the maximum" in s["reason"] or "costs" in s["reason"] for s in d.setups)


def test_no_room_to_the_next_level_means_no_entry():
    m15 = path((I0 + 120, 87_400))
    d = evaluate(m15, T_LONG, p=p_cfg(tp1_r=2.5, tp2_r=4.0, score_room_full_r=5.0))
    assert d.action == "none" and any(s["reason"].startswith("room") for s in d.setups)


REV = background(I0) + [(I0 + 12, 83_000), (I0 + 28, 85_000), (I0 + 44, 84_000), (I0 + 70, 87_000), (I0 + 84, 86_000),
                        (I0 + 100, 87_800), (I0 + 116, 84_800), (I0 + 124, 85_960), (I0 + 125, 85_700),
                        (I0 + 140, 84_200)]


def test_reversal_short_after_the_up_structure_breaks_and_its_mirror_long():
    """Up structure (higher highs and lows), a 1h close below the last higher low 86,000, a retest of that level from
    below that fails, and a 15m candle turning down: reversal short. The mirror image gives a reversal long."""
    m15 = bars_from_points(REV)
    before = [evaluate(m15, at(i)) for i in range(100, 125)]
    assert all(d.action == "none" for d in before)                        # no chasing the breakdown itself
    d = evaluate(m15, at(125))
    assert (d.action, d.setup, d.direction) == ("enter", "reversal", -1)
    assert d.structure["break_dir"] == -1 and d.structure["break_level"] == pytest.approx(85_980.0)
    assert d.stop > 85_980.0 and d.invalidation == pytest.approx(85_980.0)
    up = evaluate(mirror(m15, 85_000.0), at(125))
    assert (up.action, up.setup, up.direction) == ("enter", "reversal", 1)


def test_only_closed_candles_are_used():
    m15 = path((I0 + 120, 87_400))
    spike = list(m15)
    i = next(k for k, c in enumerate(spike) if c.open_ms == T_LONG)     # the candle still forming at T
    c = spike[i]
    spike[i] = Candle(c.open_ms, c.open, c.high + 5_000, c.low - 5_000, c.close, 1.0, c.close_ms)
    a, b = evaluate(m15, T_LONG), evaluate(spike, T_LONG)
    assert (a.action, a.r, a.stop) == (b.action, b.r, b.stop)


# ================================================================ exits (pure)
def test_exit_rules():
    p = p_cfg()
    s = idy.ManageState(direction=1, entry=100.0, r=10.0, entry_ms=0, invalidation=95.0, stage="initial", stop=90.0,
                        best=104.0, worst=96.0, two_legs=True, cost_unit=1.0)
    bar = Candle(0, 96, 97, 94, 94.5, 1, M15)
    assert idy.exit_signal(s, bar, H1, p)[0] == "invalidation"
    assert idy.exit_signal(s, None, int(12 * H1), p)[0] == "time_stop"
    assert idy.exit_signal(s, None, int(6 * H1), p)[0] == "no_progress"             # best +0.4 R after 6 h
    s.best = 106.0
    assert idy.exit_signal(s, None, int(6 * H1), p)[0] is None
    assert idy.next_stop(s, 3.0, 1.0, p, tp1_done=False) == ("initial", None)
    stage, stop = idy.next_stop(s, 3.0, 1.0, p, tp1_done=True)
    assert stage == "runner" and stop == pytest.approx(101.0)                       # break-even + costs
    s.stage, s.stop, s.best = "runner", 101.0, 112.0
    assert idy.next_stop(s, 3.0, 1.0, p, True)[1] == pytest.approx(106.0)           # 112 - 2 x ATR1h 3
    s.stop = 105.9
    assert idy.next_stop(s, 3.0, 1.0, p, True)[1] is None                           # step < 0.25 ATR15
    one = idy.ManageState(direction=-1, entry=100.0, r=10.0, entry_ms=0, invalidation=105.0, stage="initial",
                          stop=110.0, best=90.0, worst=101.0, two_legs=False, cost_unit=1.0)
    stage, stop = idy.next_stop(one, 3.0, 1.0, p, tp1_done=False)                   # single leg: 1 R move
    assert stage == "runner" and stop == pytest.approx(96.0)                        # trail 90 + 6 beats 99


def test_legs_and_sizing_respect_the_exchange_minimum():
    inst = ibt.instrument()
    cfg = config_from_dict(cfg_dict())
    sz = idy.position_size(cfg, 144.30, 0.5, 86_000.0, 600.0, inst)       # x10 of 144.30 at 11x
    assert sz["leverage"] == 11 and sz["qty"] == Decimal("0.01677")
    assert idy.split_legs(sz["qty"], 86_000.0, inst, 0.5) == [("B", Decimal("0.00839")), ("A", Decimal("0.00838"))]
    small = idy.position_size(cfg, 4.0, 0.15, 86_000.0, 600.0, inst)      # x3 of 4 USD = 12 USD: one leg only
    assert idy.split_legs(small["qty"], 86_000.0, inst, 0.5) == [("B", small["qty"])]


# ================================================================ costs
def test_cost_estimate_from_the_book_and_the_account_fee():
    book = Book(bids=[(85_000.0, 0.01), (84_990.0, 1.0)], asks=[(85_002.0, 0.01), (85_012.0, 1.0)])
    est = costs.from_book(book=book, qty=0.02, direction=1, fee_rate=0.0004, fee_source="x", stop_slippage_bps=5,
                          funding_rate_hourly=0.00001, funding_hold_hours=6)
    assert est.detail["entry_avg"] == pytest.approx(85_007.0) and est.detail["exit_avg"] == pytest.approx(84_995.0)
    assert est.funding_bps == pytest.approx(0.6) and est.depth_ok
    assert est.total_bps == pytest.approx(8 + est.entry_bps + est.exit_bps + 5 + 0.6)
    short = costs.from_book(book=book, qty=0.02, direction=-1, fee_rate=0.0004, fee_source="x", stop_slippage_bps=5,
                            funding_rate_hourly=0.00001, funding_hold_hours=6)
    assert short.funding_bps == 0.0                                       # a short receives positive funding
    thin = costs.from_book(book=book, qty=5.0, direction=1, fee_rate=0.0004, fee_source="x", stop_slippage_bps=5,
                           funding_rate_hourly=None, funding_hold_hours=6)
    assert not thin.depth_ok


def test_account_fee_rate_never_below_the_evidence():
    from types import SimpleNamespace as N

    fills = [N(taker=True, fee=0.045, price=90_000.0, quantity=0.001)] * 3
    rate, src = costs.account_fee_rate([{"category": "crypto", "taker_fee_rate": 0.0004}], "crypto", fills, 0.0005, 3)
    assert rate == pytest.approx(0.0005) and "fills" in src
    rate, src = costs.account_fee_rate([{"category": "crypto", "taker_fee_rate": 0.0004}], "crypto", fills[:2], 0.0005, 3)
    assert rate == pytest.approx(0.0004) and "schedule" in src
    assert costs.account_fee_rate(None, "crypto", [], 0.0005, 3)[0] == 0.0005


# ================================================================ candles: integrity, cache, Retry-After
def test_integrity_check_finds_gaps_stale_and_bad_candles():
    m15 = bars_from_points([(0, 100.0), (40, 110.0)])
    now = m15[-1].close_ms + 60_000
    assert check(m15, "15m", 30, now).ok
    gap = m15[:20] + m15[21:]
    assert not check(gap, "15m", 30, now).ok and "gap" in check(gap, "15m", 30, now).problems[0]
    assert "stale" in " ".join(check(m15[:-1], "15m", 30, now).problems)
    bad = m15[:-1] + [Candle(m15[-1].open_ms, 100, 90, 95, 100, 1, m15[-1].close_ms)]
    assert not check(bad, "15m", 30, now).ok
    assert not check(m15[-10:], "15m", 30, now).ok                       # too few


def test_cache_fetches_only_new_candles(tmp_path):
    from datetime import datetime, timezone

    from perpbot.storage import Store
    from perpbot.timeutil import FixedClock

    m15 = path((I0 + 120, 87_400))
    bn = IntradayBinance(m15)
    clock = FixedClock(datetime(2026, 9, 20, tzinfo=timezone.utc))
    store = Store(tmp_path / "c.db", clock, "t", "t")
    cache = CandleCache(store, bn, backfill_days=25)
    now = T_LONG + 60_000
    cache.update(("15m", "1h", "4h"), now)
    first = len(bn.requests)
    assert first >= 3 and cache.load("15m", now - 10 * M15, now)[-1].close_ms == T_LONG
    bn.requests.clear()
    cache.update(("15m", "1h", "4h"), now + M15)
    assert [r[0] for r in bn.requests] == ["15m"]                         # 1h / 4h: no new candle closed yet
    assert bn.requests[0][1] == T_LONG                                    # starts after the newest stored one


def test_binance_retry_after(cfg, monkeypatch):
    sleeps, calls = [], []

    def handler(status, retry_after):
        def h(req):
            calls.append(req.url.host)
            if len(calls) == 1:
                return httpx.Response(status, headers={"Retry-After": str(retry_after)})
            return httpx.Response(200, json=[[0, "1", "2", "0.5", "1.5", "10", 1]])
        return h

    now = [1_000_000]
    b = BinanceData(cfg, client=httpx.Client(transport=httpx.MockTransport(handler(429, 3))),
                    now_ms=lambda: now[0], sleep=sleeps.append)
    assert b.klines_since("15m", 0, 10_000_000) and sleeps == [3.0]        # short wait: once, then OK
    calls.clear()
    b = BinanceData(cfg, client=httpx.Client(transport=httpx.MockTransport(handler(429, 120))),
                    now_ms=lambda: now[0], sleep=sleeps.append)
    with pytest.raises(RateLimited) as e:
        b.klines_since("15m", 0, 10_000_000)
    assert e.value.until_ms == 1_000_000 + 120_000 and len(calls) == 1     # no other endpoint tried
    with pytest.raises(RateLimited, match="not sent"):
        b.klines_since("15m", 0, 10_000_000)
    assert len(calls) == 1
    calls.clear()
    b = BinanceData(cfg, client=httpx.Client(transport=httpx.MockTransport(handler(418, 600))),
                    now_ms=lambda: now[0], sleep=sleeps.append)
    with pytest.raises(RateLimited) as e:
        b.klines_since("15m", 0, 10_000_000)
    assert e.value.status == 418 and len(calls) == 1                      # a ban is never retried


def test_a_ban_is_remembered_by_the_next_runs(tmp_path):
    m15 = path((I0 + 120, 87_400))
    w = IWorld(tmp_path, cfg_dict(), m15)
    w.run_at(T_LONG - 4 * M15)
    w.bn.limit_status = 418
    w.run_at(T_LONG - 3 * M15)
    w.bn.requests.clear()
    w.bn.blocked_until_ms = 0                                             # a new process: only the database knows
    out = w.run_at(T_LONG - 2 * M15)
    assert w.bn.requests == [] and not out["data_ok"]                     # nothing sent before Retry-After ends
    assert "Binance paused us" in " ".join(out["data_problems"])


# ================================================================ live engine
def test_two_leg_entry_with_brackets(tmp_path):
    w = IWorld(tmp_path, cfg_dict(), path((I0 + 120, 87_400)), basis=15.0)
    out = w.run_at(T_LONG)
    assert out["decision"]["action"] == "enter", out
    foks = w.fok_calls()
    assert len(foks) == 2 and foks[0]["sl"] == foks[1]["sl"]
    t = Records(w.store).open_trade()
    assert t["intraday"] and t["two_legs"] and t["setup"] == "continuation" and t["stage"] == "initial"
    assert set(t["legs"]) == {"A", "B"} and float(foks[0]["tp"]) == t["tp2_price"] and float(foks[1]["tp"]) == t["tp1_price"]
    assert w.ex.pos_size == pytest.approx(t["legs"]["A"]["qty"] + t["legs"]["B"]["qty"])
    d = w.decisions()[-1]
    assert d["action"] == "enter" and d["data"]["intraday"]["decision"]["setup"] == "continuation"
    assert any("順勢回調" in line for line in d["data"]["analysis"])
    assert t["cost_r"] <= 0.2 + 1e-9
    # no position SL was added: the two leg stops together cover the position
    assert not [c for c in w.ex.calls if c[0] == "place_position_tpsl"]


def test_same_candle_run_twice_never_enters_twice(tmp_path):
    w = IWorld(tmp_path, cfg_dict(), path((I0 + 120, 87_400)))
    w.run_at(T_LONG)
    w.run_at(T_LONG, delay_s=200)
    assert len(w.fok_calls()) == 2                                        # the two legs of ONE entry


def test_tp1_then_break_even_then_trail_then_trail_stop(tmp_path):
    m15 = path((I0 + 100, 86_800), (I0 + 104, 86_500), (I0 + 130, 85_000))     # above TP1, below TP2
    w = IWorld(tmp_path, cfg_dict(), m15)
    w.run_at(T_LONG)
    t = Records(w.store).open_trade()
    tp1 = t["tp1_price"]
    for i in range(90, 101):
        w.run_at(at(i))
        t = Records(w.store).open_trade()
        if t and t.get("tp1_done") and t.get("stage") == "runner":
            break
    assert t["tp1_done"] and t["stage"] == "runner"
    assert t["sl_price"] >= t["entry_price"] + t["cost_unit"] - 1                 # break-even + costs
    assert w.ex.orders[t["legs"]["A"]["sl_order_id"]].status != "armed"         # leg A's stop is gone
    assert w.ex.orders[t["legs"]["B"]["sl_order_id"]].status == "armed"         # the backstop stays
    assert w.ex.pos_size == pytest.approx(t["legs"]["B"]["qty"])
    assert tp1 < 86_800 < t["tp2_price"]
    w.run_range(at(101), at(130))
    closed = Records(w.store).closed_trades()
    assert closed and closed[0]["exit_reason"] in ("TP1+trail_stop", "TP1+BE_stop")
    assert closed[0]["net_pnl"] > 0 and closed[0].get("mfe_r_15m") is not None


def test_invalidation_exit(tmp_path):
    # after entry, a 15m close back below the pullback low (before the wider stop) exits at market
    m15 = path((I0 + 92, 85_500), (I0 + 95, 85_020), (I0 + 110, 85_200))
    w = IWorld(tmp_path, cfg_dict(), m15)
    w.run_at(T_LONG)
    t = Records(w.store).open_trade()
    assert t and t["invalidation_bn"] is not None and t["sl_price"] < t["invalidation_bn"] + t["bn_offset"]
    w.run_range(at(90), at(100))
    c = Records(w.store).closed_trades()
    assert c and c[0]["exit_reason"] == "invalidation" and w.ex.pos_size == 0


def test_time_stop(tmp_path):
    # drifts sideways above the pullback low: no stop, no target; closed after 12 hours
    m15 = path((I0 + 92, 85_700), (I0 + 100, 85_500), (I0 + 108, 85_800), (I0 + 116, 85_450), (I0 + 124, 85_750),
               (I0 + 132, 85_450), (I0 + 140, 85_800))
    w = IWorld(tmp_path, cfg_dict(), m15)
    w.run_at(T_LONG)
    assert Records(w.store).open_trade()
    w.run_range(at(90), at(139))
    c = Records(w.store).closed_trades()
    assert c and c[0]["exit_reason"] in ("time_stop", "no_progress")
    assert c[0]["holding_hours"] <= 12.5


def test_stale_data_blocks_entries_but_not_management(tmp_path):
    m15 = path((I0 + 92, 85_700), (I0 + 100, 85_500), (I0 + 108, 85_800), (I0 + 116, 85_450), (I0 + 124, 85_750),
               (I0 + 132, 85_450), (I0 + 140, 85_800))
    w = IWorld(tmp_path, cfg_dict(), m15)
    w.run_at(T_LONG)
    assert Records(w.store).open_trade()
    w.bn.fail = True                                                      # Binance down from now on
    outs = w.run_range(at(90), at(139))
    assert all(not o["data_ok"] for o in outs[2:])
    c = Records(w.store).closed_trades()
    assert c and c[0]["exit_reason"] == "time_stop"                       # the clock-based exit still ran
    assert all(o["decision"]["action"] != "enter" for o in outs)


def test_cost_gate_uses_the_real_book_and_logs_the_rejection(tmp_path):
    w = IWorld(tmp_path, cfg_dict(), path((I0 + 120, 87_400)), fee_rate=0.004)     # 0.4% per side
    out = w.run_at(T_LONG)
    assert out["decision"]["action"] == "none" and not w.fok_calls()
    rec = w.decisions()[-1]["data"]["intraday"]
    assert any("cost" in s["reason"] or "maximum" in s["reason"] for s in rec["decision"]["setups"])
    assert rec["market"]["fee_rate"] == pytest.approx(0.004)


def test_event_blackout_blocks_new_entries(tmp_path):
    from perpbot.timeutil import from_ms

    rel = from_ms(T_LONG + 20 * 60_000).astimezone(__import__("zoneinfo").ZoneInfo("America/New_York"))
    ev = [{"type": "CPI", "date": rel.date().isoformat(), "time": rel.strftime("%H:%M")}]
    w = IWorld(tmp_path, cfg_dict(), path((I0 + 120, 87_400)), events=ev)
    out = w.run_at(T_LONG)
    assert out["decision"]["action"] == "blocked" and not w.fok_calls()
    assert any("event blackout" in r for r in out["decision"]["reasons"])


def test_late_run_does_not_enter(tmp_path):
    w = IWorld(tmp_path, cfg_dict(), path((I0 + 120, 87_400)))
    out = w.run_at(T_LONG, delay_s=11 * 60)
    assert out["decision"]["action"] == "blocked" and not w.fok_calls()


def test_position_from_before_the_upgrade_is_left_alone(tmp_path):
    """An open v1.10.0 position (no intraday record) keeps its own SL / TP; nothing is replaced, moved or closed,
    and no new entry is made while it is open."""
    w = IWorld(tmp_path, cfg_dict(), path((I0 + 120, 87_400), (I0 + 200, 86_000)))
    w.t_ms = T_LONG - 3 * H1
    w.clock.set(__import__("perpbot.timeutil", fromlist=["from_ms"]).from_ms(T_LONG - 3 * H1))
    w.ex.set_mark(85_500.0)
    res = w.ex.place_order(instrument_id=w.ex.inst.id, side="BUY", quantity="0.01", tif="fok", price=None,
                           reduce_only=False, client_order_id="legacy-entry", tp_trigger="89756", sl_trigger="84264")
    w.store.insert("trades", trade_uid="legacy", event="open", direction=1,
                   data={"trade_uid": "legacy", "direction": 1, "qty": 0.01, "entry_price": w.ex.pos_entry,
                         "entry_ts_ms": T_LONG - 3 * H1, "entry_utc_day": "2026-09-16", "sl_price": 84264.0,
                         "tp_price": 89756.0, "sl_order_id": res.sl_order_id, "tp_order_id": res.tp_order_id,
                         "atr": 400.0, "sl_distance": 1236.0, "initial_risk_usd": 12.36, "live": True})
    before = len(w.ex.calls)
    w.run_range(T_LONG, T_LONG + 60 * M15)
    new = [c for c in w.ex.calls[before:] if c[0] in ("place_order", "place_position_tpsl", "cancel_orders")]
    assert new == [] and w.ex.pos_size == pytest.approx(0.01)
    assert w.ex.orders[res.sl_order_id].status == "armed" and w.ex.orders[res.tp_order_id].status == "armed"
    assert any("before v2.0.0" in " ".join(o["manage"]) for o in [w.run_at(T_LONG + 61 * M15)])


# ================================================================ backtest = live
def test_backtest_uses_the_live_decisions(tmp_path):
    """Every stored live decision recomputed on the backtest's own data slicing gives the same result, and the
    replay check (cached candles) finds no difference."""
    m15 = path((I0 + 100, 87_800), (I0 + 104, 87_300), (I0 + 130, 85_000), (I0 + 170, 86_500))
    w = IWorld(tmp_path, cfg_dict(), m15)
    w.run_range(at(80), at(160))
    data = ibt.Data(split5(m15), m15, agg(m15, H1), agg(m15, H4), [])
    sim = ibt.Sim(w.cfg, data, "base", "owner", 100.0)
    n = 0
    for row in w.decisions():
        rec = row["data"]["intraday"]
        if not rec.get("replay"):
            continue
        rp = rec["replay"]
        at_ms = rp["now_ms"]
        p = sim.p
        logged = {int(k): v for k, v in rp["cost_per_unit_by_dir"].items()}
        h = rp["history"]
        again = idy.evaluate(p, rec["decision"]["t_ms"], sim.m15.upto(at_ms, p.need_15m()), sim.h1.upto(at_ms, p.need_1h()),
                             sim.h4.upto(at_ms, p.need_4h()), cost_unit_fn=lambda d, e: logged.get(d, e),
                             history=idy.History(set(h["traded_legs"]), h["last_exit_ms"], h["entries_today"]),
                             position_dir=rp["position_dir"])
        live = rec["decision"]
        assert (live["action"], live["direction"], live["setup"], live["leg_id"]) == \
            (again.action, again.direction, again.setup, again.leg_id)
        if live["r"] is not None:
            assert live["r"] == pytest.approx(again.r) and live["stop"] == pytest.approx(again.stop)
        n += 1
    assert n > 50
    rep = ibt.replay(w.store, w.cfg, 0)
    assert rep["checked"] > 50 and rep["different"] == []


def test_backtest_is_deterministic_and_reports_the_approximations():
    m15 = path((I0 + 100, 87_800), (I0 + 104, 87_300), (I0 + 130, 85_000), (I0 + 170, 86_500))
    cfg = config_from_dict(cfg_dict())
    data = ibt.Data(split5(m15), m15, agg(m15, H1), agg(m15, H4), [(T0 + k * 8 * H1, 0.0001) for k in range(60)])
    a = ibt.Sim(cfg, data, "base", "owner", 100.0).run(at(60), at(170))
    b = ibt.Sim(cfg, data, "base", "owner", 100.0).run(at(60), at(170))
    assert [(t.entry_ms, t.reason, round(t.net, 9)) for t in a["trades"]] == \
        [(t.entry_ms, t.reason, round(t.net, 9)) for t in b["trades"]]
    assert a["trades"] and a["trades"][0].setup == "continuation" and a["path_resolution"] == "5m"
    t = a["trades"][0]
    assert t.qty_a > 0 and t.qty_b > 0 and Decimal(str(t.qty_a)).as_tuple().exponent >= -5
    rep = ibt.run_all(cfg, data, at(60), at(170))
    text = ibt.summary_md(rep, cfg)
    assert "approximation" in text and "STOP is assumed first" in text and "zero/owner" in text
    assert set(rep["full"]) == {f"{s}/{z}" for s in ibt.SCENARIOS for z in ibt.SIZINGS}


def test_backtest_costs_change_the_outcome_not_the_rules():
    m15 = path((I0 + 100, 87_800), (I0 + 104, 87_300), (I0 + 130, 85_000), (I0 + 170, 86_500))
    cfg = config_from_dict(cfg_dict())
    data = ibt.Data([], m15, agg(m15, H1), agg(m15, H4), [])
    zero = ibt.Sim(cfg, data, "zero", "risk3", 100.0).run(at(60), at(170))
    stress = ibt.Sim(cfg, data, "stress", "risk3", 100.0).run(at(60), at(170))
    assert zero["path_resolution"] == "15m"                               # flagged approximation without 5m data
    if zero["trades"] and stress["trades"]:
        assert stress["trades"][0].fees > zero["trades"][0].fees


# ================================================================ CLI: preview, backtest, replay
def _cli(tmp_root, clock, bn, *argv):
    from perpbot.cli import Factories, main
    from perpbot.paths import Paths

    return main(list(argv), paths=Paths(tmp_root), clock=clock,
                factories=Factories(binance=lambda cfg: bn, registered_root=lambda: None))


def _intraday_root(tmp_root):
    import yaml

    (tmp_root / "config" / "config.yaml").write_text(yaml.safe_dump(cfg_dict()), encoding="utf-8")
    return tmp_root


def test_preview_shows_the_15_minute_decision(tmp_root, capsys):
    from perpbot.timeutil import FixedClock, from_ms

    bn = IntradayBinance(path((I0 + 120, 87_400)))
    assert _cli(_intraday_root(tmp_root), FixedClock(from_ms(T_LONG + 70_000)), bn, "preview", "--equity", "150") == 0
    out = capsys.readouterr().out
    assert "預覽：如果而家決定" in out and "做多" in out and "順勢回調" in out and "下次決定" in out


def test_cli_intraday_backtest_download_and_run(tmp_root, capsys):
    import json

    from perpbot.timeutil import FixedClock, from_ms

    root = _intraday_root(tmp_root)
    (root / "config" / "calendar_history.yaml").write_text("calendar_version: t\nevents: []\n", encoding="utf-8")
    m15 = path((I0 + 100, 86_800), (I0 + 104, 86_500), (I0 + 130, 85_000), (I0 + 170, 86_500))
    bn = IntradayBinance(m15)
    clock = FixedClock(from_ms(at(171)))
    assert _cli(root, clock, bn, "intraday-backtest", "download", "--days", "2") == 0
    assert _cli(root, clock, bn, "intraday-backtest", "run", "--days", "2") == 0
    out = capsys.readouterr().out
    assert "approximation" in out and "base/owner" in out
    res = sorted((root / "data" / "backtest_intraday").glob("results_*"))[-1]
    rep = json.loads((res / "summary.json").read_text(encoding="utf-8"))
    assert rep["full"]["base/owner"]["path_resolution"] == "5m" and (res / "summary.md").exists()


def test_cli_replay_reports_no_difference(tmp_root, capsys):
    import shutil

    from perpbot.timeutil import FixedClock, from_ms

    root = _intraday_root(tmp_root)
    w = IWorld(root / "data", cfg_dict(), path((I0 + 120, 87_400)))
    w.run_range(at(80), at(95))
    w.store.close()
    shutil.move(str(root / "data" / "db.sqlite3"), str(root / "data" / "btcperp.sqlite3"))
    from perpbot.paths import Paths

    assert Paths(root).db_file.name == "btcperp.sqlite3"
    assert _cli(root, FixedClock(from_ms(at(96))), w.bn, "intraday-replay", "--days", "30") == 0
    out = capsys.readouterr().out
    assert "DIFFERENT: 0" in out and "checked: 16" in out


# ================================================================ recovery and partial fills
def test_second_leg_unfilled_leaves_a_single_leg_runner(tmp_path):
    w = IWorld(tmp_path, cfg_dict(), path((I0 + 120, 87_400)))
    w.ex.fok_outcomes.extend([True, False])                              # leg B fills, leg A does not
    out = w.run_at(T_LONG)
    t = Records(w.store).open_trade()
    assert out["decision"]["action"] == "enter" and t and not t["two_legs"] and list(t["legs"]) == ["B"]
    assert t["tp_price"] == t["tp2_price"] and w.ex.pos_size == pytest.approx(t["legs"]["B"]["qty"])


def test_runner_leg_unfilled_means_no_position(tmp_path):
    w = IWorld(tmp_path, cfg_dict(), path((I0 + 120, 87_400)))
    w.ex.fok_outcomes.extend([False])
    out = w.run_at(T_LONG)
    assert out["decision"]["action"] == "rejected" and w.ex.pos_size == 0 and len(w.fok_calls()) == 1
    assert Records(w.store).state()["position_state"] == "flat"


def test_interrupted_entry_is_recovered_as_an_intraday_trade(tmp_path):
    from perpbot import intraday_live

    w = IWorld(tmp_path, cfg_dict(), path((I0 + 120, 87_400)))
    real = intraday_live.IntradayRunner._record_entry

    def boom(self, *a, **k):
        raise RuntimeError("process killed after the fills")
    intraday_live.IntradayRunner._record_entry = boom
    try:
        with pytest.raises(RuntimeError):
            w.run_at(T_LONG)
    finally:
        intraday_live.IntradayRunner._record_entry = real
    assert w.ex.pos_size > 0 and Records(w.store).open_trade() is None
    out = w.run_at(T_LONG + M15)
    t = Records(w.store).open_trade()
    assert t and t["adopted"] and t["intraday"] and t["two_legs"] and set(t["legs"]) == {"A", "B"}
    assert len(w.fok_calls()) == 2                                       # never a second entry
    assert out["decision"]["action"] != "enter"


def test_no_missed_run_flood_after_the_upgrade(tmp_root):
    """The first 15-minute run after upgrading from the hourly schedule must not report the past day's 15-minute
    slots as missed (they belong to the old schedule)."""
    from datetime import timedelta

    from perpbot.cli import _missed_run_alerts
    from perpbot.storage import Store
    from perpbot.timeutil import FixedClock

    cfg = config_from_dict(cfg_dict())
    now = __import__("conftest").hkt(2026, 10, 6, 14, 1)
    clock = FixedClock(now - timedelta(hours=20))
    old = Store(tmp_root / "db.sqlite3", clock, "1.10.0", "1.10.0")
    for h in range(20):
        clock.set(now - timedelta(hours=20 - h, minutes=31))
        old.insert("runs", command="decide", event="start")
    old.close()
    clock.set(now)
    store = Store(tmp_root / "db.sqlite3", clock, cfg.config_version, "2.0.0")
    store.insert("runs", command="decide", event="start")
    alerts = []
    eng = __import__("types").SimpleNamespace(alert=lambda kind, text, dedupe_key=None: alerts.append(text))
    _missed_run_alerts(eng, cfg, store, clock)
    assert alerts == []
