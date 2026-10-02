"""v1.9.0 (owner 2026-10-02): decide every hour. strategy.cadence rolling_1h: a decision 30 minutes after every UTC
hour (HH:30 HKT, retry HH:50) on daily candles that end at that hour, built from Binance 1h candles; the h4 gate
uses the last 4h candle closed then; the 3-day rule counts 72 hours. Backtest variant R1h_live does the same."""

import random
from datetime import date, datetime, timedelta, timezone

import pytest

from perpbot import backtest as bt
from perpbot import strategy as st
from perpbot import winsched
from perpbot.config import ConfigError, config_from_dict
from perpbot.records import Records
from perpbot.timeutil import DAY_MS, HOUR_MS, day_start_ms, to_ms

from conftest import World, hkt, ok_leverage, shipped_config
from test_backtest import synthetic_dataset
from test_rolling import _cal, _root


def utc(y, m, d, h):
    return to_ms(datetime(y, m, d, h, tzinfo=timezone.utc))


# ---------------------------------------------------------------- config and schedule
def test_shipped_schedule_is_hourly():
    cfg = config_from_dict(shipped_config())
    sc = cfg.schedule
    assert cfg.strategy.cadence == "rolling_1h"
    assert sc.decide_times_hkt == [f"{h:02d}:{m}" for h in range(24) for m in ("30", "50")]
    assert sc.manage_times_hkt == ["02:10", "06:10", "10:10", "14:10", "18:10", "22:10"]
    assert (sc.period_entry_start_minutes, sc.period_entry_end_minutes) == (30, 60)
    names = [t.name for t in winsched.plan(cfg)]
    assert sum(n.startswith("decide_") for n in names) == 48 and sum(n.startswith("manage_") for n in names) == 6
    assert len(set(names)) == len(names)


def test_hourly_config_is_validated():
    d = shipped_config()
    d["schedule"]["period_entry_end_minutes"] = 90
    with pytest.raises(ConfigError, match="may not overlap"):
        config_from_dict(d)
    d = shipped_config()
    d["schedule"]["decide_times_hkt"] = [t for t in d["schedule"]["decide_times_hkt"] if not t.startswith("13:")]
    with pytest.raises(ConfigError, match="05:00 UTC period"):
        config_from_dict(d)
    d = shipped_config()
    d["strategy"]["cadence"] = "rolling_2h"                     # still backtest only
    with pytest.raises(ConfigError):
        config_from_dict(d)


# ---------------------------------------------------------------- strategy / backtest equivalence
@pytest.fixture(scope="module")
def setup():
    from conftest import as_daily, with_test_risk

    cfg = config_from_dict(with_test_risk(as_daily(shipped_config())))
    ds = synthetic_dataset(date(2020, 1, 1), 440, seed=4)
    cal = bt.merged_calendar(cfg, _root(), _cal())
    return cfg, ds, cal, bt.PeriodInputs(cfg, ds, cal), bt.PeriodInputs(cfg, ds, cal, cadence="rolling_1h")


def test_1h_scores_match_live_and_the_4h_ones_at_shared_boundaries(setup):
    cfg, ds, _cal_, p4, p1 = setup
    n = int(cfg.binance.daily_candles_to_load)
    rnd = random.Random(5)
    t0 = day_start_ms(date(2021, 2, 1))
    for _ in range(24):
        t = t0 + rnd.randrange(0, 40 * 24) * HOUR_MS                  # the synthetic data ends 2021-03-16
        live_like = st.rolling_score(st.shifted_daily(ds.h1, t, n, bar_ms=HOUR_MS), t, cfg.strategy)
        assert p1.score_at(t).to_dict() == live_like.to_dict()
        if t % (4 * HOUR_MS) == 0:
            assert p1.score_at(t).score == p4.score_at(t).score
    t = t0 + 5 * HOUR_MS
    f_live = st.live_rolling_features(cfg, t, ds.h4, [(a, r) for a, r, _ in ds.funding], [], cadence="rolling_1h",
                                      base=ds.h1)
    assert f_live.score.to_dict() == p1.score_at(t).to_dict() and f_live.key.endswith("T05:00")


def test_1h_three_day_rule_is_72_periods(setup):
    cfg, _ds, _cal_, _p4, p1 = setup
    t = day_start_ms(date(2021, 3, 1)) + 5 * HOUR_MS
    f = p1.features(t)
    assert f is not None and f.cadence == "rolling_1h" and f.key.endswith("T05:00")
    assert len(f.scores) == int(cfg.strategy.opposite_days_rule) * 24 + 2
    keys = sorted(f.scores)
    assert all(st.key_ms(b) - st.key_ms(a) == HOUR_MS for a, b in zip(keys, keys[1:]))
    _plan, ctx = st.plan_for(f, cfg, position_dir=1, entry_day=date(2021, 2, 20), entered_today=False,
                             paused_reason=None, entry_key="2021-02-20T00:00")
    assert ctx.opposite_days_rule == 72 and ctx.period_word == "1-hour period"


def test_simulator_runs_r1h_in_1h_steps(setup):
    cfg, ds, cal, p4, p1 = setup
    d0 = date(2021, 2, 1)
    days = 12
    di = bt.DayInputs(cfg, ds, cal)
    feats = {d0 + timedelta(days=i): di.features(d0 + timedelta(days=i)) for i in range(days + 1)}
    pf4 = {day_start_ms(d0) + i * 4 * HOUR_MS: p4.features(day_start_ms(d0) + i * 4 * HOUR_MS) for i in range(days * 6 + 1)}
    asked: list[int] = []

    class Seen(dict):
        def get(self, k, default=None):
            asked.append(k)
            return super().get(k, default)
    pf1 = Seen({day_start_ms(d0) + i * HOUR_MS: p1.features(day_start_ms(d0) + i * HOUR_MS) for i in range(days * 24 + 1)})
    sim = bt.Simulator(cfg, ds.h1, ds.funding, feats, 0.0005, pf4, None, pf1)
    res = sim.run("R1h_live", d0, d0 + timedelta(days=days), 0, 10_000.0)
    assert res.trades and len(res.equity_curve) == days
    assert asked == [day_start_ms(d0) + i * HOUR_MS for i in range(days * 24)]      # a decision every hour
    assert all(t.entry_day[13:] == ":00" for t in res.trades)
    assert bt.VARIANTS["R1h_live"].cadence == "rolling_1h" and bt.VARIANTS["R1h_live_stress"].stress


# ---------------------------------------------------------------- live engine
@pytest.fixture
def hourly(tmp_path, hourly_cfg_dict):
    def make(start):
        w = World(tmp_path, start, hourly_cfg_dict)
        ok_leverage(w.ex)
        return w
    return make


def test_hourly_decide_enters_once_per_hour_and_holds(hourly):
    w = hourly(hkt(2026, 10, 5, 12, 30))                         # 04:30 UTC: the 04:00 period
    w.bn.signal_1h(utc(2026, 10, 5, 4), "strong_long")
    w.decide()
    assert w.pos() > 0
    t = Records(w.store).open_trade()
    assert t["entry_period"] == "2026-10-05T04:00"
    row = w.store.latest("decisions", "score IS NOT NULL")
    inp = row["data"]["inputs"]
    assert inp["cadence"] == "rolling_1h" and inp["opposite_rule"] == 72 and inp["h1_candles_fetched"] > 24 * 200
    text = "\n".join(row["data"]["analysis"])
    assert "每 1 小時" in text and "下一次決定：2026-10-05 13:30" in text
    assert w.store.count("bn_klines_1h") > 24 * 200
    w.at(hkt(2026, 10, 5, 12, 50)).decide()                       # retry in the same hour: no second entry
    w.at(hkt(2026, 10, 5, 13, 30)).decide()                       # next hour, same direction: hold
    assert len(w.fok_calls()) == 1 and w.pos() > 0
    assert w.store.latest("decisions", "score IS NOT NULL")["utc_day"] == "2026-10-05T05:00"
    w.at(hkt(2026, 10, 5, 14, 10)).manage()                       # manage at :10 = before the hour's window
    assert not w.tg.has("late decision") and not w.tg.has("missed")


def test_hourly_flip_on_a_strong_opposite_hour(hourly):
    w = hourly(hkt(2026, 10, 5, 12, 30))
    w.bn.signal_1h(utc(2026, 10, 5, 4), "strong_long")
    w.decide()
    assert w.pos() > 0
    w.bn.signal_1h(utc(2026, 10, 5, 5), "strong_short")
    w.at(hkt(2026, 10, 5, 13, 30)).decide()
    assert w.pos() < 0
    assert Records(w.store).closed_trades()[0]["exit_reason"] == "flip"


def test_missed_hour_is_reported_and_the_next_hour_decides(hourly):
    w = hourly(hkt(2026, 10, 5, 12, 30))
    w.bn.signal_1h(utc(2026, 10, 5, 5), "strong_long")
    w.at(hkt(2026, 10, 5, 13, 5)).decide()                        # before the 05:00 period's window
    assert w.pos() == 0 and not w.fok_calls()
    w.at(hkt(2026, 10, 5, 13, 30)).decide()
    assert w.pos() > 0


def test_hourly_analysis_toast_only_when_something_happens(tmp_path, hourly_cfg_dict):
    hourly_cfg_dict["notifications"]["analysis_toast_only_actions"] = True             # shipped
    w = World(tmp_path, hkt(2026, 10, 5, 12, 30), hourly_cfg_dict)
    ok_leverage(w.ex)
    toasts = []
    w.bn.signal_1h(utc(2026, 10, 5, 4), "strong_long")
    eng = w.engine()
    eng.notifier = lambda kind, text: toasts.append(kind)
    eng.cmd_decide()
    assert "分析" in toasts                                       # the entry
    toasts.clear()
    eng = w.at(hkt(2026, 10, 5, 13, 30)).engine()
    eng.notifier = lambda kind, text: toasts.append(kind)
    eng.cmd_decide()                                              # hold: no analysis toast
    assert "分析" not in toasts
