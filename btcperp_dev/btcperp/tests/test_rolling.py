"""v1.5.0 option B (strategy.cadence rolling_4h): the same score and rules, decided every 4 hours on daily candles
that end at the decision time. Live and backtest must agree exactly."""

import random
from datetime import date, datetime, timedelta, timezone

import pytest

from perpbot import backtest as bt
from perpbot import strategy as st
from perpbot.indicators import Candle
from perpbot.records import Records
from perpbot.timeutil import DAY_MS, HOUR_MS, day_start_ms, to_ms

from conftest import World, hkt, ok_leverage

UTC = timezone.utc
H4 = 4 * HOUR_MS


def T(y, m, d, h):
    return to_ms(datetime(y, m, d, h, tzinfo=UTC))


def rworld(tmp_path, rolling_cfg_dict, start, **ov):
    w = World(tmp_path, start, rolling_cfg_dict, **ov)
    ok_leverage(w.ex)
    return w


# ---------------------------------------------------------------- daily candles ending at T
def _h4_series(start: date, days: int, seed: int = 5) -> list[Candle]:
    rnd = random.Random(seed)
    out, px, t0 = [], 30_000.0, day_start_ms(start)
    for i in range(days * 6):
        o = px
        c = max(1000.0, o * (1 + rnd.gauss(0, 0.01)))
        out.append(Candle(t0 + i * H4, o, max(o, c) * 1.003, min(o, c) * 0.997, c, 2.0, t0 + (i + 1) * H4))
        px = c
    return out


def test_shifted_daily_at_midnight_equals_the_daily_candle_and_ends_at_t():
    h4 = _h4_series(date(2024, 1, 1), 60)
    cut = day_start_ms(date(2024, 2, 20))
    days = st.shifted_daily(h4, cut, 10)
    assert len(days) == 10 and days[-1].open_ms == cut - DAY_MS and days[-1].close_ms == cut
    for d in days:                                          # identical to aggregating the day's six 4h candles
        g = [c for c in h4 if d.open_ms <= c.open_ms < d.open_ms + DAY_MS]
        assert (d.open, d.high, d.low, d.close) == (g[0].open, max(c.high for c in g), min(c.low for c in g), g[-1].close)
    t = cut + 3 * H4                                        # 12:00 UTC: days run 12:00 -> 12:00
    days12 = st.shifted_daily(h4, t, 3)
    assert [x.open_ms for x in days12] == [t - 3 * DAY_MS, t - 2 * DAY_MS, t - DAY_MS]
    assert days12[-1].close == next(c for c in h4 if c.close_ms == t).close
    assert all(c.close_ms <= t for c in days12)


def test_backtest_score_cache_equals_live_shifted_candles_even_with_a_gap(cfg):
    h4 = _h4_series(date(2023, 1, 1), 700, seed=9)
    del h4[3000]                                            # a missing 4h candle (exchange maintenance)
    ds = bt.Dataset([], h4, [], [(T(2023, 1, 1, 0) + k * 8 * HOUR_MS, 0.0001, 0.0) for k in range(2100)])
    pi = bt.PeriodInputs(cfg, ds, None)
    rnd = random.Random(1)
    for _ in range(25):
        t = T(2024, 6, 1, 0) + rnd.randrange(0, 150) * H4
        live = st.rolling_score(st.shifted_daily(h4, t, int(cfg.binance.daily_candles_to_load)), t, cfg.strategy)
        assert pi.score_at(t).to_dict() == live.to_dict()


# ---------------------------------------------------------------- strategy rules in 4h periods
def test_opposite_streak_counts_periods_after_the_entry_period():
    keys = [st.period_key(T(2026, 10, 5, 0) + i * H4) for i in range(20)]
    dirs = {k: -1 for k in keys}
    assert st.opposite_streak_keys(1, keys[1], dirs, keys[19]) == 18
    assert st.opposite_streak_keys(1, keys[2], dirs, keys[19]) == 17
    dirs[keys[10]] = 1
    assert st.opposite_streak_keys(1, keys[1], dirs, keys[19]) == 9
    assert st.opposite_streak_keys(1, "2026-10-05", dirs, keys[19]) == 9          # a daily entry key still works
    assert st.flip_confirmed(1, {keys[18]: -40.0, keys[19]: -45.0}, keys[19], 2, 30.0)
    assert not st.flip_confirmed(1, {keys[18]: 20.0, keys[19]: -45.0}, keys[19], 2, 30.0)
    assert not st.flip_confirmed(1, {keys[18]: -20.0, keys[19]: -45.0}, keys[19], 2, 30.0)   # weak earlier period
    assert st.flip_confirmed(1, {keys[19]: -45.0}, keys[19], 1, 30.0)


def test_rolling_three_day_rule_is_18_periods(rolling_cfg_dict):
    from perpbot.config import config_from_dict

    cfg = config_from_dict(rolling_cfg_dict)
    keys = [st.period_key(T(2026, 10, 5, 0) + i * H4) for i in range(20)]
    sc = st.ScoreResult(keys[-1], 0, 100.0, 101.0, 99.0, 101.0, 99.0, 100.0, 90.0, 2.0, 0.0, -10.0, 0.0, 0.0, 0.0,
                        -10.0, -1, 10.0, 0.25, 300)
    g = st.GateResult("x", False)
    f = st.DayFeatures(date(2026, 10, 8), sc, 1.0, 1.0, 0, st.FundingStat(0.0, 0, 50.0, 100), {k: -1 for k in keys}, [],
                       g, g, g, st.event_gate([]), [], "rolling_4h", keys[-1], T(2026, 10, 8, 0), {k: -10.0 for k in keys})
    plan, ctx = st.plan_for(f, cfg, position_dir=1, entry_day=None, entered_today=False, paused_reason=None,
                            entry_key=keys[1])
    assert ctx.opposite_days_rule == 18 and ctx.opposite_streak == 18 and plan.close_reason == "three_day_rule"
    plan, ctx = st.plan_for(f, cfg, position_dir=1, entry_day=None, entered_today=False, paused_reason=None,
                            entry_key=keys[2])
    assert ctx.opposite_streak == 17 and plan.action == "hold"


# ---------------------------------------------------------------- the live engine
def test_rolling_decide_enters_once_per_period_and_flips_next_period(tmp_path, rolling_cfg_dict):
    w = rworld(tmp_path, rolling_cfg_dict, hkt(2026, 10, 5, 12, 30))
    t1 = T(2026, 10, 5, 4)
    w.bn.signal_4h(t1, "strong_long")
    before = st.rolling_score(st.shifted_daily(sorted(w.bn.h4.values(), key=lambda c: c.open_ms), t1 - H4, 1000),
                              t1 - H4, w.cfg.strategy)
    assert abs(before.score) < 30                            # the shaped candle only counts from its close at T
    w.decide()
    assert w.pos() > 0
    row = w.store.latest("decisions", "score IS NOT NULL")
    assert row["utc_day"] == "2026-10-05T04:00" and row["data"]["plan"]["cadence"] == "rolling_4h"
    assert row["data"]["score"]["score"] > 50 and row["data"]["inputs"]["opposite_rule"] == 18
    tr = Records(w.store).open_trade()
    assert tr["entry_period"] == "2026-10-05T04:00" and tr["entry_utc_day"] == "2026-10-05"
    w.at(hkt(2026, 10, 5, 12, 50)).decide()                 # retry in the same period: no second entry
    assert len(w.fok_calls()) == 1
    t2 = T(2026, 10, 5, 8)
    w.bn.signal_4h(t2, "strong_short")
    w.at(hkt(2026, 10, 5, 16, 30)).decide()                 # next period: strong opposite signal -> flip
    assert w.pos() < 0
    assert w.store.latest("decisions", "score IS NOT NULL")["utc_day"] == "2026-10-05T08:00"


def test_rolling_flip_waits_for_confirmation_when_configured(tmp_path, rolling_cfg_dict):
    w = rworld(tmp_path, rolling_cfg_dict, hkt(2026, 10, 5, 12, 30), strategy__flip_confirm_periods=2)
    w.bn.signal_4h(T(2026, 10, 5, 4), "strong_long")
    w.decide()
    assert w.pos() > 0
    w.bn.signal_4h(T(2026, 10, 5, 8), "strong_short")
    w.at(hkt(2026, 10, 5, 16, 30)).decide()
    assert w.pos() > 0                                      # first opposite period: hold
    assert "confirmation" in w.store.latest("decisions", "score IS NOT NULL")["reason"]
    w.bn.signal_4h(T(2026, 10, 5, 12), "strong_short")
    w.at(hkt(2026, 10, 5, 20, 30)).decide()
    assert w.pos() < 0                                      # second opposite period: flip


def test_rolling_missed_decide_runs_the_close_rules_late_in_manage(tmp_path, rolling_cfg_dict):
    w = rworld(tmp_path, rolling_cfg_dict, hkt(2026, 10, 5, 12, 30))
    w.bn.signal_4h(T(2026, 10, 5, 4), "strong_long")
    w.decide()
    assert w.pos() > 0
    w.bn.signal_4h(T(2026, 10, 5, 8), "strong_short")
    w.at(hkt(2026, 10, 5, 18, 30)).manage()                 # 16:30 / 16:50 decide never ran (PC asleep)
    assert w.pos() == 0                                     # flip rule closed the long, no late entry
    row = w.store.latest("decisions", "score IS NOT NULL")
    assert row["utc_day"] == "2026-10-05T08:00" and row["data"]["plan"]["late"] is True
    assert len(w.fok_calls()) == 1


def test_rolling_decide_outside_the_period_window_does_not_enter(tmp_path, rolling_cfg_dict):
    w = rworld(tmp_path, rolling_cfg_dict, hkt(2026, 10, 5, 14, 0))       # 06:00 UTC: after the 04:30-05:30 window
    w.bn.signal_4h(T(2026, 10, 5, 4), "strong_long")
    w.decide()
    assert w.pos() == 0 and len(w.fok_calls()) == 0


# ---------------------------------------------------------------- live == backtest over many 4h periods
def test_backtest_rolling_decisions_equal_live_decide(tmp_path, rolling_cfg_dict):
    rnd = random.Random(21)
    start = hkt(2026, 10, 5, 8, 30)
    w = rworld(tmp_path, rolling_cfg_dict, start)
    kinds = ["strong_long", "strong_short", "flat", "flat"]
    t0 = T(2026, 10, 5, 0)
    periods = [t0 + i * H4 for i in range(30)]
    for t in periods:
        w.bn.signal_4h(t, rnd.choice(kinds))
        w.bn.fund[t] = rnd.choice([0.0001, 0.0003, -0.0002, 0.001])
    for t in periods:
        dt = datetime.fromtimestamp((t + 30 * 60_000 + 5_000) / 1000, tz=UTC)
        w.at(dt).decide()
    ds = bt.Dataset([], sorted(w.bn.h4.values(), key=lambda c: c.open_ms), [],
                    [(ts, r, 0.0) for ts, r in sorted(w.bn.fund.items())])
    pi = bt.PeriodInputs(w.cfg, ds, w.calendar)
    rows = w.store.query("SELECT utc_day, data FROM decisions WHERE score IS NOT NULL ORDER BY id")
    compared = 0
    for r in rows:
        live = r["data"]
        plan_live = live["plan"]
        t = st.key_ms(r["utc_day"])
        f = pi.features(t)
        plan, _ = bt.plan_for(f, w.cfg, position_dir=int(plan_live["position_dir_at_decision"]),
                              entry_day=date.fromisoformat(live["inputs"]["entry_day"]),
                              entered_today=bool(live["inputs"]["entered_today"]), paused_reason=live["paused_reason"],
                              entry_key=live["inputs"]["entry_key"])
        assert f.score.to_dict() == live["score"], r["utc_day"]
        assert f.gates_triggered() == plan_live["gates_triggered"], r["utc_day"]
        assert f.funding.percentile == plan_live["funding_percentile"], r["utc_day"]
        assert f.scores == live["inputs"]["score_history"], r["utc_day"]
        for k in ("action", "close_reason", "enter_direction", "enter_fraction", "target_direction", "entry_blocked",
                  "notes"):
            assert plan.to_dict()[k] == plan_live[k], (r["utc_day"], k)
        compared += 1
    assert compared >= 28
    assert len({r["data"]["plan"]["action"] for r in rows}) >= 3


def test_backtest_runs_rolling_variants_in_4h_steps(bt_rolling_setup):
    sim, feats, pfeats, d0 = bt_rolling_setup
    res = sim.run("R4h_live", d0, d0 + timedelta(days=20), 0, 10_000.0)
    assert res.trades and all("T" in t.entry_day for t in res.trades)
    assert len(res.equity_curve) == 20                       # one equity point per UTC day
    hours = {int(t.entry_day[11:13]) for t in res.trades}
    assert hours - {0}, "entries happen at 4h boundaries other than 00:00 UTC"
    daily = sim.run("A_live", d0, d0 + timedelta(days=20), 0, 10_000.0)
    assert all("T" not in t.entry_day for t in daily.trades)


@pytest.fixture
def bt_rolling_setup(cfg):
    from test_backtest import synthetic_dataset

    ds = synthetic_dataset(date(2020, 1, 1), 440, seed=4)          # funding percentile needs 365 days of history
    d0 = date(2021, 2, 1)
    cal = bt.merged_calendar(cfg, _root(), _cal())
    di, pi = bt.DayInputs(cfg, ds, cal), bt.PeriodInputs(cfg, ds, cal)
    feats = {d0 + timedelta(days=i): di.features(d0 + timedelta(days=i)) for i in range(21)}
    pfeats = {day_start_ms(d0) + i * H4: pi.features(day_start_ms(d0) + i * H4) for i in range(21 * 6)}
    sim = bt.Simulator(cfg, ds.h1, ds.funding, feats, 0.0005, pfeats)
    return sim, feats, pfeats, d0


def _root():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent


def _cal():
    from perpbot.calendar_events import load_calendar

    return load_calendar(_root() / "config" / "calendar.yaml")


# ---------------------------------------------------------------- config, schedule, reports
def test_shipped_config_is_rolling_and_validated(rolling_cfg_dict):
    from perpbot.config import ConfigError, config_from_dict

    cfg = config_from_dict(rolling_cfg_dict)
    assert cfg.strategy.cadence == "rolling_4h" and cfg.config_version == "1.8.0"
    assert len(cfg.schedule.decide_times_hkt) == 12 and len(cfg.schedule.manage_times_hkt) == 6
    bad = dict(rolling_cfg_dict, schedule=dict(rolling_cfg_dict["schedule"], decide_times_hkt=["08:30", "08:50"]))
    with pytest.raises(ConfigError, match="no decide time"):
        config_from_dict(bad)
    with pytest.raises(ConfigError):
        config_from_dict(dict(rolling_cfg_dict, strategy=dict(rolling_cfg_dict["strategy"], cadence="hourly")))


def test_schedule_upgrade_removes_the_old_daily_tasks(rolling_cfg_dict, monkeypatch, tmp_path):
    from perpbot import winsched
    from perpbot.config import config_from_dict

    cfg = config_from_dict(rolling_cfg_dict)
    names = {s.name for s in winsched.plan(cfg)}
    assert {f"decide_{h:02d}30" for h in (0, 4, 8, 12, 16, 20)} <= names and "manage_1030" in names
    calls = []
    old = ['"\\btcperp\\decide_0830","x","Ready"', '"\\btcperp\\manage_1230","x","Ready"',
           '"\\btcperp\\manage_0430","x","Ready"', '"\\btcperp\\report_daily","x","Ready"', '"\\other\\decide_x","x","Ready"']

    def fake_run(args):
        calls.append(args)
        if args[:2] == ["schtasks", "/Query"]:
            return 0, "\n".join(old)
        return 0, "SUCCESS"

    monkeypatch.setattr(winsched, "_live", lambda: True)
    monkeypatch.setattr(winsched, "_run", fake_run)
    monkeypatch.setattr(winsched, "write_marker", lambda root: None)
    monkeypatch.setattr(winsched, "user_id", lambda: "PC\\owner")
    res = winsched.install(cfg, tmp_path, tmp_path / "tasks")
    deleted = [a[3] for a in calls if a[:2] == ["schtasks", "/Delete"]]
    assert sorted(deleted) == ["\\btcperp\\manage_0430", "\\btcperp\\manage_1230"]   # decide_0830 is still planned
    assert any("removed (not in this schedule)" in r["output"] for r in res)


def test_monthly_report_counts_missed_4h_periods(tmp_path, rolling_cfg_dict):
    from perpbot.paths import Paths
    from perpbot.reports import Reporter

    w = rworld(tmp_path, rolling_cfg_dict, hkt(2026, 10, 5, 8, 30), reports__max_missed_decision_days=0)
    for h in (8, 12, 16):
        w.at(hkt(2026, 10, 5, h, 30)).decide()
    w.at(hkt(2026, 10, 7, 9, 0))
    paths = Paths(tmp_path / "root")
    paths.ensure()
    info = Reporter(w.engine(), paths)._decision_days(date(2026, 10, 1), date(2026, 11, 1), date(2026, 10, 7), "2026-10")
    assert info["cadence"] == "rolling_4h" and info["incomplete_month"] is True
    assert "2026-10-05T12:00" in info["missed_days"] and "2026-10-05T04:00" not in info["missed_days"]
    assert len(info["missed_days"]) == 12 - 3                   # 2 days x 6 periods, 3 decided on time


# ---------------------------------------------------------------- readable analysis and Preview.bat
def test_decision_stores_a_readable_analysis_and_notifies(tmp_path, rolling_cfg_dict):
    w = rworld(tmp_path, rolling_cfg_dict, hkt(2026, 10, 5, 12, 30))
    w.bn.signal_4h(T(2026, 10, 5, 4), "strong_long")
    calls = []
    eng = w.engine()
    eng.notifier = lambda kind, text: calls.append((kind, text))
    eng.cmd_decide()
    data = w.store.latest("decisions", "score IS NOT NULL")["data"]
    text = "\n".join(data["analysis"])
    assert "分數 +" in text and "做多" in text and "行動：開倉" in text and "止損約" in text and "止賺約" in text
    assert "每 4 小時" in text and "下一次決定：2026-10-05 16:30" in text and "反手條件：分數去到 -30" in text
    assert ("分析", "分數 +100（做多）→ 開倉") in calls
    from perpbot.dashboard import build_summary

    summ = build_summary(w.store, w.cfg, w.calendar, w.clock.now())
    assert summ["decision"]["analysis"] == data["analysis"]


def test_analysis_toast_can_be_switched_off_and_daily_decisions_have_it_too(tmp_path, cfg_dict):
    w = World(tmp_path, hkt(2026, 10, 5, 8, 30), cfg_dict, notifications__analysis_toast=False)
    ok_leverage(w.ex)
    w.bn.signal(date(2026, 10, 5), "strong_short")
    calls = []
    eng = w.engine()
    eng.notifier = lambda kind, text: calls.append((kind, text))
    eng.cmd_decide()
    text = "\n".join(w.store.latest("decisions", "score IS NOT NULL")["data"]["analysis"])
    assert "（每日）" in text and "做空" in text and "下一次決定：2026-10-06 08:30" in text
    assert not any(k == "分析" for k, _ in calls)


def _preview(tmp_root, clock, bn, *extra):
    from perpbot.cli import Factories, main
    from perpbot.paths import Paths

    return main(["preview", *extra], paths=Paths(tmp_root), clock=clock,
                factories=Factories(binance=lambda cfg: bn, registered_root=lambda: None))


def test_preview_is_read_only_and_explains_the_rolling_decision(tmp_root, capsys):
    from conftest import ROOT, FakeBinance
    from perpbot.timeutil import FixedClock

    (tmp_root / "config" / "config.yaml").write_text((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"),
                                                     encoding="utf-8")        # the shipped rolling_4h config
    (tmp_root / "config" / "calendar_history.yaml").write_text("", encoding="utf-8")
    now = hkt(2026, 10, 5, 12, 40)
    bn = FakeBinance(now.date())
    bn.signal_4h(T(2026, 10, 5, 4), "strong_long")
    assert _preview(tmp_root, FixedClock(now), bn, "--equity", "200") == 0
    out = capsys.readouterr().out
    assert "預覽：如果而家決定" in out and "做多" in out and "行動：開倉" in out
    assert "倉位 = 本金 ×10（12 倍逐倉）" in out and "（倉位約 $2,000）" in out   # v1.8.0: 200 x 10, score 100
    assert "每 4 小時" in out and "唔落單" in out
    assert not (tmp_root / "data" / "btcperp.sqlite3").exists()          # nothing written


def test_preview_daily_config_without_signal(tmp_root, capsys):
    from conftest import FakeBinance
    from perpbot.timeutil import FixedClock

    now = hkt(2026, 10, 5, 8, 40)
    assert _preview(tmp_root, FixedClock(now), FakeBinance(now.date())) == 0
    out = capsys.readouterr().out
    assert "（每日）" in out and "分數" in out and "下一次決定：2026-10-06 08:30" in out
