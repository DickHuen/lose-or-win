"""v2.2.0 (owner 2026-10-07: "再放多啲"): still looser - a weaker 15m turn (close beyond the previous close), TP1 at
0.8 R, longer pullback window, smaller legs, deeper pullbacks, wider ranges, a leg up to 3 times, no cooldown."""

from __future__ import annotations

import pytest

from perpbot import intraday as idy
from perpbot import intraday_bt as ibt
from perpbot.config import config_from_dict
from perpbot.indicators import Candle
from perpbot.records import Records

from conftest import shipped_intraday_config
from intraday_helpers import H1, H4, M15, T0, IWorld, agg, bars_from_points, mirror, random_15m, split5
from test_intraday_v200 import I0, REV, T_LONG, at, path
from test_intraday_v210 import RANGE, V210


def shipped():
    return shipped_intraday_config()


def params(**over):
    d = shipped()
    d["intraday"].update(over)
    return idy.Params.from_cfg(config_from_dict(d))


def test_shipped_values():
    cfg = config_from_dict(shipped())
    ic, r = cfg.intraday, cfg.risk
    assert cfg.config_version == "2.2.0"
    assert (ic.trigger_beyond, ic.trigger_clv_min, ic.tp1_r, ic.tp2_r) == ("close", -0.2, 0.8, 3.0)
    assert (ic.pullback_max_bars, ic.leg_min_atr, ic.retrace_min, ic.retrace_max) == (16, 0.75, 0.236, 0.886)
    assert (ic.range_min_atr, ic.range_edge_atr, ic.max_entries_per_leg, ic.cooldown_bars) == (1.5, 0.5, 3, 0)
    assert (ic.max_cost_r, ic.sl_min_pct, ic.max_entries_per_day) == (0.35, 0.3, 12)       # cost gate kept
    assert (r.notional_multiple_full_tier, r.leverage, r.kill_drawdown_pct) == (20, 25, 95)


@pytest.mark.parametrize("mode,ok", [("close", True), ("high_low", False)])
def test_the_weaker_turn(mode, ok):
    prev = Candle(0, 100.0, 103.0, 99.0, 100.0, 1, M15)
    cur = Candle(M15, 100.0, 102.0, 99.5, 101.5, 1, 2 * M15)        # above the previous close, below its high
    assert idy._turn(prev, cur, 1, 0.0, mode == "close")[0] is ok
    down = Candle(M15, 100.0, 100.5, 99.0, 99.2, 1, 2 * M15)       # below the previous close, above its low
    assert idy._turn(prev, down, -1, 0.0, mode == "close")[0] is ok   # mirrored


def test_clv_minus_02_accepts_a_close_just_below_the_middle():
    prev = Candle(0, 100.0, 101.0, 99.0, 100.0, 1, M15)
    cur = Candle(M15, 100.0, 103.0, 99.0, 100.7, 1, 2 * M15)          # CLV -0.15
    assert idy._turn(prev, cur, 1, -0.2, True)[0] and not idy._turn(prev, cur, 1, 0.0, True)[0]


def test_tp1_is_0_8_r_on_the_exchange(tmp_path):
    w = IWorld(tmp_path, shipped(), path((I0 + 120, 87_400)))
    out = w.run_range(at(80), at(95))
    t = Records(w.store).open_trade() or Records(w.store).closed_trades()[-1]
    assert t["two_legs"]
    r = abs(t["entry_price"] - t["legs"]["A"]["sl"])
    assert abs(t["tp1_price"] - t["entry_price"]) == pytest.approx(0.8 * r, abs=3)
    assert abs(t["tp2_price"] - t["entry_price"]) == pytest.approx(3.0 * r, abs=3)
    assert any(o["decision"]["action"] == "enter" for o in out)


def test_rules_stay_mirror_symmetric():
    p = params(sl_min_pct=0.0)
    for pts, center in ((RANGE, 84_000.0), (REV, 85_000.0)):
        m15 = bars_from_points(pts)
        mm = mirror(m15, center)
        n = 0
        for i in range(0, 140, 2):
            a = idy.evaluate(p, at(i), m15, agg(m15, H1), agg(m15, H4), cost_unit_fn=lambda d, e: 100.0,
                             history=idy.History())
            b = idy.evaluate(p, at(i), mm, agg(mm, H1), agg(mm, H4), cost_unit_fn=lambda d, e: 100.0,
                             history=idy.History())
            assert (a.action, a.setup, a.direction) == (b.action, b.setup, -b.direction), i
            n += a.action == "enter"
        assert n >= 1


def test_more_entries_than_v210_on_the_same_markets():
    v210 = config_from_dict({**shipped(), "intraday": {**shipped()["intraday"], **V210}})
    v220 = config_from_dict(shipped())
    n210 = n220 = 0
    for seed in (3, 5, 8):
        m15 = random_15m(96 * 30, seed)
        data = ibt.Data([], m15, agg(m15, H1), agg(m15, H4), [])
        start, end = m15[0].open_ms + 12 * 96 * M15, m15[-1].close_ms
        n210 += len(ibt.Sim(v210, data, "base", "owner", 100.0).run(start, end)["trades"])
        n220 += len(ibt.Sim(v220, data, "base", "owner", 100.0).run(start, end)["trades"])
    assert n220 > n210 > 0


def test_cost_gate_still_refuses_expensive_trades():
    m15 = path((I0 + 120, 87_400))
    d = idy.evaluate(params(), T_LONG, m15, agg(m15, H1), agg(m15, H4), cost_unit_fn=lambda dd, e: e * 0.01,
                     history=idy.History())
    assert d.action == "none"
