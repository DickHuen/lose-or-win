"""v2.3.0: two new intraday knobs, NEUTRAL as shipped (the v2.2.0 behaviour): `min_room_r` (room needed to the next
obstacle, v2.0.0 - v2.2.0 = TP1 = 0.8 R) and `range_latest_edge` (off). Tested 2026-10-08 on the owner's 21 real days
for "開多啲單": room 0.5 / 0.7 R and the latest-edge fade gave more trades and no better results, so they are not used.
The forecast / dynamic exit of v2.3.0: test_forecast_v230.py."""

from __future__ import annotations

import pytest

from perpbot import intraday as idy
from perpbot.config import ConfigError, config_from_dict

from conftest import shipped_intraday_config
from intraday_helpers import H1, H4, agg, bars_from_points, mirror
from test_intraday_v200 import I0, T_LONG, at, path
from test_intraday_v210 import RANGE
from test_intraday_v220 import V220


def shipped():
    return shipped_intraday_config()


def params(**over):
    d = shipped()
    d["intraday"].update(over)
    return idy.Params.from_cfg(config_from_dict(d))


def ev(m15, t, p=None):
    return idy.evaluate(p or params(), t, m15, agg(m15, H1), agg(m15, H4), cost_unit_fn=lambda d, e: e * 0.0014,
                        history=idy.History())


def test_shipped_values_are_the_v220_behaviour():
    cfg = config_from_dict(shipped())
    ic, r = cfg.intraday, cfg.risk
    assert cfg.config_version == "2.3.0"
    assert (ic.min_room_r, ic.tp1_r, ic.range_latest_edge, ic.max_hold_hours, ic.no_progress_hours) == (0.8, 0.8, False, 12, 6)
    assert (r.notional_multiple_full_tier, r.leverage, r.kill_drawdown_pct) == (20, 25, 95)


@pytest.mark.parametrize("key,value", [("min_room_r", -0.1), ("range_latest_edge", "yes")])
def test_new_values_are_validated(key, value):
    d = shipped()
    d["intraday"][key] = value
    with pytest.raises(ConfigError):
        config_from_dict(d)


def test_the_room_minimum_is_min_room_r_not_tp1():
    m15 = path((I0 + 120, 87_400))
    refused = ev(m15, T_LONG, p=params(min_room_r=50.0))
    room = next(s["reason"] for s in refused.setups if s["reason"].startswith("room"))
    assert refused.action == "none" and room.startswith("room 2.40 R") and room.endswith("< 50.0 R")
    assert ev(m15, T_LONG, p=params(min_room_r=2.35)).action == "enter"        # TP1 stays 0.8 R either way
    assert ev(m15, T_LONG, p=params(min_room_r=2.45)).action == "none"


def test_range_latest_edge_fades_the_edge_touched_last():
    m15 = bars_from_points(RANGE)
    off = [ev(m15, at(i)) for i in range(0, 140)]
    on = [ev(m15, at(i), p=params(range_latest_edge=True)) for i in range(0, 140)]
    assert sum(any("both edges" in s["reason"] for s in d.setups) for d in off) > 0
    assert not [d for d in on if any("both edges" in s["reason"] for s in d.setups)]
    fades = [d for d in on if d.action == "enter" and d.setup == "range"]
    assert len(fades) > len([d for d in off if d.action == "enter"]) and {d.direction for d in fades} == {1, -1}
    for d in fades:
        det = d.setups[-1]["detail"]
        lo, hi = det["range_low"], det["range_high"]
        assert lo < d.entry_ref < hi and (d.entry_ref - (lo + hi) / 2) * d.direction < 0      # bought low / sold high
        assert d.stop is not None and (d.entry_ref - d.stop) * d.direction > 0


def test_range_latest_edge_is_mirror_symmetric():
    m15 = bars_from_points(RANGE)
    mm = mirror(m15, 84_000.0)
    p = params(range_latest_edge=True, sl_min_pct=0.0)
    n = 0
    for i in range(0, 140, 2):
        a = idy.evaluate(p, at(i), m15, agg(m15, H1), agg(m15, H4), cost_unit_fn=lambda d, e: 100.0,
                         history=idy.History())
        b = idy.evaluate(p, at(i), mm, agg(mm, H1), agg(mm, H4), cost_unit_fn=lambda d, e: 100.0,
                         history=idy.History())
        assert (a.action, a.setup, a.direction) == (b.action, b.setup, -b.direction), i
        n += a.action == "enter"
    assert n >= 2


def test_v220_profile_equals_the_shipped_intraday_values():
    d = shipped()
    d["intraday"].update(V220)
    assert config_from_dict(d).intraday.to_dict() == config_from_dict(shipped()).intraday.to_dict()
