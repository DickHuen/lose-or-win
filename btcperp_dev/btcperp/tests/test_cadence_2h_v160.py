"""v1.6.0: the owner asked about deciding every 2 hours. Backtest first: variant R2h_live (and its stress twin)
decides at every 2h boundary on daily candles ending then, built from 1h candles. Live still decides every 4h."""

import random
from datetime import date, timedelta

import pytest

from perpbot import backtest as bt
from perpbot import strategy as st
from perpbot.config import ConfigError, config_from_dict
from perpbot.timeutil import DAY_MS, HOUR_MS, day_start_ms

from conftest import shipped_config
from test_backtest import synthetic_dataset
from test_rolling import _cal, _root

H2 = 2 * HOUR_MS


@pytest.fixture(scope="module")
def setup():
    from conftest import TEST_RISK, as_daily

    d = as_daily(shipped_config())
    d["risk"].update(TEST_RISK)
    cfg = config_from_dict(d)
    ds = synthetic_dataset(date(2020, 1, 1), 440, seed=4)
    cal = bt.merged_calendar(cfg, _root(), _cal())
    return cfg, ds, cal, bt.PeriodInputs(cfg, ds, cal), bt.PeriodInputs(cfg, ds, cal, cadence="rolling_2h")


def test_2h_scores_use_1h_candles_and_match_4h_at_shared_boundaries(setup):
    cfg, ds, _cal_, p4, p2 = setup
    n = int(cfg.binance.daily_candles_to_load)
    rnd = random.Random(3)
    t0 = day_start_ms(date(2021, 2, 1))
    for _ in range(20):
        t = t0 + rnd.randrange(0, 40 * 12) * H2                        # the synthetic data ends 2021-03-16
        live_like = st.rolling_score(st.shifted_daily(ds.h1, t, n, bar_ms=HOUR_MS), t, cfg.strategy)
        assert p2.score_at(t).to_dict() == live_like.to_dict()
        if t % (4 * HOUR_MS) == 0:
            assert p2.score_at(t).score == p4.score_at(t).score       # same daily candles, built from 1h or 4h


def test_2h_three_day_rule_is_36_periods(setup):
    cfg, _ds, _cal_, _p4, p2 = setup
    t = day_start_ms(date(2021, 3, 1)) + H2                        # 02:00 UTC: not a 4h boundary
    f = p2.features(t)
    assert f is not None and f.cadence == "rolling_2h" and f.key.endswith("T02:00")
    assert len(f.scores) == int(cfg.strategy.opposite_days_rule) * 12 + 2
    keys = sorted(f.scores)
    assert all(st.key_ms(b) - st.key_ms(a) == H2 for a, b in zip(keys, keys[1:]))
    _plan, ctx = st.plan_for(f, cfg, position_dir=1, entry_day=date(2021, 2, 20), entered_today=False,
                             paused_reason=None, entry_key="2021-02-20T00:00")
    assert ctx.opposite_days_rule == 36 and ctx.period_word == "2-hour period"


def test_simulator_runs_r2h_in_2h_steps(setup):
    cfg, ds, _cal_, p4, p2 = setup
    d0 = date(2021, 2, 1)
    days = 20
    di = bt.DayInputs(cfg, ds, setup[2])
    feats = {d0 + timedelta(days=i): di.features(d0 + timedelta(days=i)) for i in range(days + 1)}
    pf4 = {day_start_ms(d0) + i * 4 * HOUR_MS: p4.features(day_start_ms(d0) + i * 4 * HOUR_MS) for i in range(days * 6 + 1)}
    pf2 = {day_start_ms(d0) + i * H2: p2.features(day_start_ms(d0) + i * H2) for i in range(days * 12 + 1)}
    sim = bt.Simulator(cfg, ds.h1, ds.funding, feats, 0.0005, pf4, pf2)
    res = sim.run("R2h_live", d0, d0 + timedelta(days=days), 0, 10_000.0)
    assert res.trades and len(res.equity_curve) == days
    hours = {int(t.entry_day[11:13]) for t in res.trades}
    assert all(h % 2 == 0 for h in hours) and any(h % 4 == 2 for h in hours), hours
    r4 = sim.run("R4h_live", d0, d0 + timedelta(days=days), 0, 10_000.0)
    assert all(int(t.entry_day[11:13]) % 4 == 0 for t in r4.trades)


def test_r2h_variants_and_the_i8_comparison():
    assert bt.VARIANTS["R2h_live"].cadence == "rolling_2h" and bt.VARIANTS["R2h_live"].twin == "R2h_live_stress"
    assert bt.VARIANTS["R2h_live_stress"].stress
    import yaml

    crit = yaml.safe_load((_root() / "config" / "backtest_criteria.yaml").read_text(encoding="utf-8"))
    sums = {"R2h_live": {"full_total_r_median": 20.0}, "R4h_live": {"full_total_r_median": 31.3}}
    i8 = next(r for r in bt.evaluate(crit, sums) if r["id"] == "I8")
    assert i8["informational"] and i8["value"] == pytest.approx(-11.3) and i8["pass"] is False


def test_live_config_cannot_decide_every_2h_yet():
    d = shipped_config()
    d["strategy"]["cadence"] = "rolling_2h"
    with pytest.raises(ConfigError):
        config_from_dict(d)
