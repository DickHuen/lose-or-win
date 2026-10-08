"""v2.1.0 (owner 2026-10-07: "唔好太嚴，我要博多啲、食多啲波幅"): looser intraday values, range-edge fades, a leg may be
traded twice, and the fix that the trigger candle's own wick is not "the next obstacle" (it refused most entries)."""

from __future__ import annotations

import pytest

from perpbot import intraday as idy
from perpbot import intraday_bt as ibt
from perpbot.config import config_from_dict
from perpbot.indicators import Candle
from perpbot.records import Records

from conftest import shipped_intraday_config
from intraday_helpers import H1, H4, M15, T0, IWorld, agg, background, bars_from_points, mirror, split5
from test_intraday_v200 import I0, REV, STRICT_V200, T_LONG, UP, at, path

RANGE = background(I0 + 200)


# v2.1.0 shipped values (v2.2.0 ships looser ones: test_intraday_v220.py)
V210 = dict(leg_min_atr=1.0, retrace_min=0.236, retrace_max=0.786, pullback_max_bars=12, reversal_window_hours=12,
            retest_zone_atr=0.5, reclaim_atr=0.5, range_enabled=True, range_min_atr=2.0, range_edge_atr=0.35,
            sl_min_atr1h=0.75, sl_min_pct=0.3, max_cost_r=0.35, room_recent_bars=8, cooldown_bars=1,
            max_entries_per_day=12, max_entries_per_leg=2, tp1_r=1.0, trigger_clv_min=0.0, trigger_beyond="high_low",
            min_room_r=1.0, range_latest_edge=False, max_hold_hours=12, no_progress_hours=6)


def shipped():
    d = shipped_intraday_config()
    d["intraday"].update(V210)
    return d


def params(**over):
    d = shipped()
    d["intraday"].update(over)
    return idy.Params.from_cfg(config_from_dict(d))


def ev(m15, t, p=None, hist=None):
    return idy.evaluate(p or params(), t, m15, agg(m15, H1), agg(m15, H4), cost_unit_fn=lambda d, e: e * 0.0014,
                        history=hist or idy.History())


def test_v210_values_are_looser_and_the_owner_risk_is_unchanged():
    cfg = config_from_dict(shipped())
    ic, r, s = cfg.intraday, cfg.risk, cfg.strategy
    assert cfg.config_version == "2.3.0"
    assert (ic.max_cost_r, ic.sl_min_pct, ic.sl_min_atr1h, ic.leg_min_atr, ic.retrace_min) == (0.35, 0.3, 0.75, 1.0, 0.236)
    assert (ic.range_enabled, ic.max_entries_per_leg, ic.max_entries_per_day, ic.cooldown_bars) == (True, 2, 12, 1)
    assert (ic.tp1_r, ic.tp2_r, ic.max_hold_hours) == (1.0, 3.0, 12)                 # exits unchanged
    assert (r.notional_multiple_full_tier, r.leverage, r.max_margin_use_pct) == (20, 25, 92)
    assert s.size_tiers == [[0, 0.15], [40, 0.30], [50, 0.50], [75, 1.00]] and s.min_entry_abs_score == 20
    assert (r.kill_drawdown_pct, r.equity_floor_pct_of_net_funded) == (95, 5)


def test_the_trigger_candles_own_wick_is_not_the_next_obstacle():
    """v2.0.0 counted the high of the candle that triggered a long as the nearest resistance, so the room was
    almost always a few dollars and most entries were refused."""
    p = params()
    st = idy.Structure(1, [], [])
    bars = [Candle(T0 + i * M15, 100.0, 101.0, 99.0, 100.5, 1, T0 + (i + 1) * M15) for i in range(10)]
    bars.append(Candle(T0 + 10 * M15, 100.5, 104.0, 100.4, 103.5, 1, T0 + 11 * M15))     # trigger, wick to 104
    lvl, room = idy.obstacle(1, 103.5, st, bars, 2.0, p)
    assert lvl is None and room == pytest.approx(p.open_room_atr * 2.0)
    bars[-2] = Candle(bars[-2].open_ms, 100.0, 105.0, 99.0, 100.5, 1, bars[-2].close_ms)  # an earlier high counts
    assert idy.obstacle(1, 103.5, st, bars, 2.0, p) == (105.0, pytest.approx(1.5))


def test_range_edges_are_faded_both_ways():
    m15 = bars_from_points(RANGE)
    seen = [ev(m15, at(i)) for i in range(0, 120)]
    fades = [d for d in seen if d.action == "enter" and d.setup == "range"]
    assert {d.direction for d in fades} == {1, -1}
    for d in fades:
        lo, hi = d.setups[-1]["detail"]["range_low"], d.setups[-1]["detail"]["range_high"]
        assert lo < d.entry_ref < hi and (d.entry_ref - (lo + hi) / 2) * d.direction < 0   # bought low / sold high
        assert d.stop is not None and (d.entry_ref - d.stop) * d.direction > 0
    off = [ev(m15, at(i), p=params(range_enabled=False)) for i in range(0, 120)]
    assert not [d for d in off if d.action == "enter" and d.setup == "range"]              # v2.0.0 behaviour


def test_range_fades_are_mirror_symmetric():
    m15 = bars_from_points(RANGE)
    mm = mirror(m15, 84_000.0)
    p = params(sl_min_pct=0.0)
    for i in range(0, 120, 3):
        a = idy.evaluate(p, at(i), m15, agg(m15, H1), agg(m15, H4), cost_unit_fn=lambda d, e: 100.0,
                         history=idy.History())
        b = idy.evaluate(p, at(i), mm, agg(mm, H1), agg(mm, H4), cost_unit_fn=lambda d, e: 100.0,
                         history=idy.History())
        assert (a.action, a.setup, a.direction) == (b.action, b.setup, -b.direction), i


def test_a_leg_may_be_traded_twice_not_three_times():
    m15 = path((I0 + 120, 87_400))
    first = ev(m15, T_LONG)
    assert first.action == "enter"
    assert ev(m15, T_LONG, hist=idy.History({first.leg_id: 1})).action == "enter"
    third = ev(m15, T_LONG, hist=idy.History({first.leg_id: 2}))
    assert third.action == "none" and any("already traded 2" in s["reason"] for s in third.setups)


def test_shallower_pullbacks_now_count():
    # a 30% pullback: refused by v2.0.0 (38.2% minimum), taken by v2.1.0
    pts = UP[:-2] + [(I0 + 82, 85_700), (I0 + 83, 85_900)]
    m15 = bars_from_points(pts + [(I0 + 110, 87_000)])
    t = at(83)
    strict = idy.Params.from_cfg(config_from_dict({**shipped(), "intraday": {**shipped()["intraday"], **STRICT_V200}}))
    assert ev(m15, t, p=strict).action == "none"
    d = ev(m15, t)
    assert d.action == "enter" and d.setup == "continuation" and d.direction == 1


def test_more_entries_than_v200_on_the_same_market():
    """The same synthetic market (a range, a trend, a break): v2.1.0 enters more often than the v2.0.0 values."""
    m15 = bars_from_points(REV)
    data = ibt.Data(split5(m15), m15, agg(m15, H1), agg(m15, H4), [])
    loose = config_from_dict(shipped())
    tight = config_from_dict({**shipped(), "intraday": {**shipped()["intraday"], **STRICT_V200}})
    a = ibt.Sim(tight, data, "base", "owner", 100.0).run(T0 + 1200 * M15, at(140))
    b = ibt.Sim(loose, data, "base", "owner", 100.0).run(T0 + 1200 * M15, at(140))
    assert len(b["trades"]) > len(a["trades"]) and {t.setup for t in b["trades"]} >= {"range"}


def test_live_range_fade_two_legs(tmp_path):
    m15 = bars_from_points(RANGE)
    w = IWorld(tmp_path, shipped(), m15)
    outs = w.run_range(at(0), at(80))
    entered = [o for o in outs if o["decision"]["action"] == "enter"]
    assert entered, [o["decision"] for o in outs[:5]]
    first = Records(w.store).closed_trades()[-1] if Records(w.store).closed_trades() else Records(w.store).open_trade()
    assert first["setup"] == "range" and first["two_legs"]
    foks = w.fok_calls()
    assert len(foks) >= 2 and foks[0]["sl"] == foks[1]["sl"]


def test_replay_skips_decisions_of_another_config_version(tmp_path):
    m15 = path((I0 + 120, 87_400))
    w = IWorld(tmp_path, shipped(), m15)
    w.run_range(at(85), at(92))
    rep = ibt.replay(w.store, w.cfg, 0)
    assert rep["checked"] == 8 and rep["different"] == []
    other = config_from_dict({**shipped(), "config_version": "2.1.1"})
    rep = ibt.replay(w.store, other, 0)
    assert rep["checked"] == 0 and rep["skipped"] == 8
