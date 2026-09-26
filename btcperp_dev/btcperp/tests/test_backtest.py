"""Backtest (review B3 / v1.2.0 item 1): same decisions as live, deterministic, conservative execution."""

import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from perpbot import backtest as bt
from perpbot.indicators import Candle
from perpbot.strategy import DayFeatures, FundingStat, GateResult, ScoreResult
from perpbot.timeutil import DAY_MS, HOUR_MS, day_start_ms

from conftest import World, hkt, ok_leverage

UTC = timezone.utc


# ---------------------------------------------------------------- committee check: backtest == live decide
def test_backtest_decisions_equal_live_decide_on_20_days(tmp_path, cfg_dict):
    rnd = random.Random(11)
    start = hkt(2026, 10, 5, 8, 30)
    w = World(tmp_path, start, cfg_dict)
    ok_leverage(w.ex)
    kinds = ["strong_long", "strong_short", "weak_long", "weak_short", "flat"]
    days = [start.date() + timedelta(days=i) for i in range(26)]
    for d in days:
        w.bn.signal(d, rnd.choice(kinds))
        w.bn.set_funding(d, rnd.choice([0.0001, 0.0003, -0.0002, 0.001]))
    for d in days:
        w.at(hkt(d.year, d.month, d.day, 8, 30)).decide()
    ds = bt.Dataset(sorted(w.bn.daily.values(), key=lambda c: c.open_ms), sorted(w.bn.h4.values(), key=lambda c: c.open_ms),
                    [], [(ts, r, 0.0) for ts, r in sorted(w.bn.fund.items())])
    di = bt.DayInputs(w.cfg, ds, w.calendar)
    rows = w.store.query("SELECT utc_day, data FROM decisions WHERE score IS NOT NULL ORDER BY id")
    compared = 0
    for r in rows:
        live = r["data"]
        plan_live = live["plan"]
        d = date.fromisoformat(r["utc_day"])
        f = di.features(d)
        plan, _ = bt.plan_for(f, w.cfg, position_dir=int(plan_live["position_dir_at_decision"]),
                              entry_day=date.fromisoformat(live["inputs"]["entry_day"]),
                              entered_today=bool(live["inputs"]["entered_today"]), paused_reason=live["paused_reason"])
        assert f.score.to_dict() == live["score"], d
        assert f.gates_triggered() == plan_live["gates_triggered"] and [list(c) for c in f.caps] == \
            [list(c) for c in plan_live["caps"]], d
        assert f.funding.percentile == plan_live["funding_percentile"], d
        for k in ("action", "close_reason", "enter_direction", "enter_fraction", "target_direction", "entry_blocked",
                  "notes"):
            assert plan.to_dict()[k] == plan_live[k], (d, k)
        compared += 1
    assert compared >= 20
    assert len({r["data"]["plan"]["action"] for r in rows}) >= 3          # a mix of enter / hold / flip / none


# ---------------------------------------------------------------- synthetic data for end-to-end runs
def synthetic_dataset(start: date, days: int, seed: int = 3) -> bt.Dataset:
    rnd = random.Random(seed)
    h1, px = [], 20_000.0
    t0 = day_start_ms(start)
    drift = 0.0
    for i in range(days * 24):
        if i % (24 * 20) == 0:
            drift = rnd.choice([-1, 1]) * 0.0006
        o = px
        c = max(1000.0, o * (1 + drift + rnd.gauss(0, 0.006)))
        hi, lo = max(o, c) * (1 + abs(rnd.gauss(0, 0.002))), min(o, c) * (1 - abs(rnd.gauss(0, 0.002)))
        h1.append(Candle(t0 + i * HOUR_MS, o, hi, lo, c, 1.0, t0 + (i + 1) * HOUR_MS))
        px = c

    def agg(n: int) -> list[Candle]:
        out = []
        for k in range(0, len(h1) - n + 1, n):
            g = h1[k:k + n]
            out.append(Candle(g[0].open_ms, g[0].open, max(x.high for x in g), min(x.low for x in g), g[-1].close,
                              float(n), g[-1].close_ms))
        return out

    funding = [(t0 + k * 8 * HOUR_MS, 0.0001 + 0.0002 * math.sin(k / 9.0) + rnd.gauss(0, 0.00005), 0.0)
               for k in range(days * 3)]
    return bt.Dataset(agg(24), agg(4), h1, funding)


def write_dataset(ds: bt.Dataset, d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    bt._write_candles(bt._csv_path(d, "1d"), ds.daily)
    bt._write_candles(bt._csv_path(d, "4h"), ds.h4)
    bt._write_candles(bt._csv_path(d, "1h"), ds.h1)
    bt._write_funding(bt._csv_path(d, "funding"), ds.funding)


@pytest.fixture
def bt_cfg(cfg_dict):
    from perpbot.config import config_from_dict

    cfg_dict["backtest"].update({"first_window_start": "2021-01-10", "window_months": 3, "start_offsets_days": [0, 7]})
    return config_from_dict(cfg_dict)


def test_run_backtest_is_deterministic_and_writes_results(tmp_path, bt_cfg):
    from perpbot.calendar_events import load_calendar

    root = Path(__file__).resolve().parent.parent
    ds = synthetic_dataset(date(2020, 1, 1), 610)
    write_dataset(ds, tmp_path / "data")
    cal = load_calendar(root / "config" / "calendar.yaml")
    r1 = bt.run_backtest(bt_cfg, root, tmp_path / "data", tmp_path / "out1", cal, 0.0005, progress=lambda *a: None)
    r2 = bt.run_backtest(bt_cfg, root, tmp_path / "data", tmp_path / "out2", cal, 0.0005, progress=lambda *a: None)
    assert r1["result_sha256"] == r2["result_sha256"]
    assert (tmp_path / "out1" / "summary.md").read_text().startswith("# btcperp backtest - verdict:")
    assert set(r1["summaries"]) == set(bt.VARIANTS) and r1["verdict"] in ("PASS", "FAIL")
    assert len(r1["windows"]) == 2 and r1["summaries"]["A_live"]["full_trades_median"] > 5
    ids = [c["id"] for c in r1["criteria"]]
    assert ids[:5] == ["C1", "C2", "C3", "C4", "C5"]
    runs = (tmp_path / "out1" / "runs.csv").read_text().splitlines()
    assert len(runs) == 1 + len(bt.VARIANTS) * (2 * 2 + 2)
    assert (tmp_path / "out1" / "trades_A_live.csv").exists()


# ---------------------------------------------------------------- simulator mechanics on hand-made days
def _features(d: date, direction: int, abs_score: float, atr: float, close: float, tier: float = 1.0) -> DayFeatures:
    sc = ScoreResult(d.isoformat(), day_start_ms(d) - DAY_MS, close, close, close, close, close, close, close, atr, 0.0,
                     0.0, 0.0, 0.0, 0.0, direction * abs_score, direction, abs_score, tier, 1000)
    g = [GateResult("ema200_regime", False), GateResult("h4_trend", False), GateResult("extreme_funding", False),
         GateResult("event_window", False)]
    return DayFeatures(d, sc, 0.0, 0.0, 0, FundingStat(0.0, 0, 50.0, 1000), {d.isoformat(): direction}, [],
                       g[0], g[1], g[2], g[3], [])


def _flat_hours(start: date, days: int, price: float) -> list[Candle]:
    t0 = day_start_ms(start)
    return [Candle(t0 + i * HOUR_MS, price, price * 1.001, price * 0.999, price, 1.0, t0 + (i + 1) * HOUR_MS)
            for i in range(days * 24)]


def _sim(cfg, h1, feats):
    ds = bt.Dataset([], [], h1, [(day_start_ms(date(2021, 1, 1)), 0.0, 0.0)])
    return bt.Simulator(cfg, ds, feats, 0.0005)


def test_sl_counts_first_when_one_candle_touches_both(bt_cfg):
    d0 = date(2021, 1, 1)
    h1 = _flat_hours(d0, 5, 100.0)
    i = 24 + 5                                                     # day 2, 05:00: huge range candle
    h1[i] = Candle(h1[i].open_ms, 100.0, 200.0, 50.0, 100.0, 1.0, h1[i].close_ms)
    feats = {d0: _features(d0, 1, 60.0, 2.0, 100.0)}
    res = _sim(bt_cfg, h1, feats).run("A_live", d0, d0 + timedelta(days=4), 0, 10_000.0)
    t = res.trades[0]
    assert t.exit_reason == "SL" and t.exit_price < t.sl                      # stop hit first, minus exit slippage
    assert t.entry_price > 100.0                                              # entry slippage applied


def test_costs_follow_shadow_r(bt_cfg):
    from perpbot.shadow import SimTrade, _r

    d0 = date(2021, 1, 1)
    h1 = _flat_hours(d0, 6, 100.0)
    for k in range(30, 34):                                        # rally to the TP on day 2
        h1[k] = Candle(h1[k].open_ms, 100.0, 107.0, 99.9, 106.5, 1.0, h1[k].close_ms)
    feats = {d0: _features(d0, 1, 60.0, 2.0, 100.0)}
    sim = _sim(bt_cfg, h1, feats)
    res = sim.run("A_live", d0, d0 + timedelta(days=5), 0, 10_000.0)
    t = res.trades[0]
    assert t.exit_reason == "TP"
    unit = 1.5 * 2.0
    st = SimTrade("x", t.entry_day, 1, t.qty * unit, t.entry_price, t.entry_ts, 2.0, t.sl, t.tp, exit_price=t.exit_price,
                  exit_ts_ms=t.exit_ts)
    assert t.net_pnl == pytest.approx(_r(st, 1.5, 0.0005, sim.funding))
    assert res.end_equity == pytest.approx(10_000.0 + t.net_pnl)
    assert t.risk_usd == pytest.approx(t.qty * unit)


def test_drawdown_kill_pauses_then_resumes_after_pause_days(cfg_dict):
    from perpbot.config import config_from_dict

    cfg_dict["risk"].update({"risk_per_trade_pct": 5, "ramp_trades": 0, "notional_cap_pct_equity": 300,
                             "kill_losing_streak_pct": 90})              # isolate the drawdown switch
    cfg = config_from_dict(cfg_dict)
    d0 = date(2021, 1, 1)
    h1 = _flat_hours(d0, 40, 100.0)
    for day in range(1, 40):                                       # every day at 05:00 UTC a dip through the stop
        k = day * 24 + 5
        h1[k] = Candle(h1[k].open_ms, 100.0, 100.0, 96.0, 100.0, 1.0, h1[k].close_ms)
    feats = {d0 + timedelta(days=i): _features(d0 + timedelta(days=i), 1, 60.0, 2.0, 100.0) for i in range(40)}
    res = _sim(cfg, h1, feats).run("A_live", d0, d0 + timedelta(days=35), 0, 10_000.0)
    assert res.kills["drawdown"] >= 1 and res.kill_log[0][1] == "drawdown"
    kill_day = date.fromisoformat(res.kill_log[0][0])
    pause = int(cfg.backtest.kill_pause_days)
    entries = [date.fromisoformat(t.entry_day) for t in res.trades]
    assert all(e >= kill_day + timedelta(days=pause) for e in entries if e >= kill_day)
    assert any(e >= kill_day + timedelta(days=pause) for e in entries)       # resumed after the pause
    assert all(t.exit_reason == "SL" for t in res.trades)


def test_control_variant_goes_flat_on_weak_score(bt_cfg):
    d0 = date(2021, 1, 1)
    h1 = _flat_hours(d0, 5, 100.0)
    feats = {d0: _features(d0, 1, 60.0, 2.0, 100.0), d0 + timedelta(days=1): _features(d0 + timedelta(days=1), 1, 10.0, 2.0, 100.0)}
    live = _sim(bt_cfg, h1, feats).run("A_live", d0, d0 + timedelta(days=3), 0, 10_000.0)
    ctrl = _sim(bt_cfg, h1, feats).run("C_control", d0, d0 + timedelta(days=3), 0, 10_000.0)
    assert live.trades[0].exit_reason == "end_of_run"
    assert ctrl.trades[0].exit_reason == "flat_rule"


def test_breakeven_variant_moves_stop(bt_cfg):
    d0 = date(2021, 1, 1)
    h1 = _flat_hours(d0, 6, 100.0)
    h1[26] = Candle(h1[26].open_ms, 100.0, 103.0, 100.0, 102.0, 1.0, h1[26].close_ms)    # +1.5 ATR, no TP
    h1[40] = Candle(h1[40].open_ms, 100.0, 100.1, 99.0, 99.5, 1.0, h1[40].close_ms)      # back below entry
    feats = {d0: _features(d0, 1, 60.0, 2.0, 100.0)}
    b = _sim(bt_cfg, h1, feats).run("B_breakeven", d0, d0 + timedelta(days=4), 0, 10_000.0)
    a = _sim(bt_cfg, h1, feats).run("A_live", d0, d0 + timedelta(days=4), 0, 10_000.0)
    assert b.trades[0].exit_reason == "BE" and a.trades[0].exit_reason == "end_of_run"


def test_windows_and_criteria_evaluation(bt_cfg):
    ws = bt.windows(bt_cfg, date(2021, 12, 31))
    assert ws[0] == (date(2021, 1, 10), date(2021, 4, 10)) and ws[-1][1] <= date(2022, 1, 1)
    crit = {"primary_variant": "A_live", "rules": [
        {"id": "C1", "text": "x", "metric": "m", "op": ">", "value": 0},
        {"id": "I1", "text": "y", "metric": "m", "metric_minus": {"variant": "C", "metric": "m"}, "op": ">", "value": 0,
         "informational": True}]}
    res = bt.evaluate(crit, {"A_live": {"m": 1.0}, "C": {"m": 2.0}})
    assert res[0]["pass"] is True and res[1]["pass"] is False and res[1]["value"] == -1.0


def test_historical_calendar_loads_and_merges(cfg):
    from perpbot.calendar_events import load_calendar

    root = Path(__file__).resolve().parent.parent
    live = load_calendar(root / "config" / "calendar.yaml")
    cal = bt.merged_calendar(cfg, root, live)
    years = {e.release_utc.year for e in cal.events}
    assert {2020, 2021, 2022, 2023, 2024, 2025, 2026, 2027} <= years
    assert all(e.release_utc.weekday() < 5 for e in cal.events)
    cpi_2023 = [e for e in cal.events if e.type == "CPI" and e.release_utc.year == 2023]
    assert len(cpi_2023) == 12


def test_cli_backtest_needs_confirmed_criteria(tmp_root, capsys):
    import shutil

    from perpbot.cli import EXIT_CONFIRM, Factories, main
    from perpbot.paths import Paths
    from perpbot.timeutil import FixedClock

    from conftest import make_env

    root = Path(__file__).resolve().parent.parent
    for f in ("calendar_history.yaml", "backtest_criteria.yaml"):
        shutil.copy2(root / "config" / f, tmp_root / "config" / f)
    import yaml

    cfgp = tmp_root / "config" / "config.yaml"
    data = yaml.safe_load(cfgp.read_text())
    data["backtest"].update({"first_window_start": "2021-01-10", "window_months": 3, "start_offsets_days": [0]})
    cfgp.write_text(yaml.safe_dump(data))
    make_env(tmp_root)
    paths = Paths(tmp_root)
    paths.ensure()
    write_dataset(synthetic_dataset(date(2020, 1, 1), 480), paths.data_dir / "backtest")
    f = Factories(registered_root=lambda: None)
    clock = FixedClock(datetime(2021, 6, 1, tzinfo=UTC))
    assert main(["backtest", "run"], paths=paths, clock=clock, factories=f) == EXIT_CONFIRM
    assert "NOT confirmed" in (main(["backtest", "criteria"], paths=paths, clock=clock, factories=f) == 0
                               and capsys.readouterr().out)
    assert main(["backtest", "confirm"], paths=paths, clock=clock, factories=f) == 0
    capsys.readouterr()
    assert main(["backtest", "run"], paths=paths, clock=clock, factories=f) == 0
    out = capsys.readouterr().out
    assert "verdict" in out and "results:" in out
    crit = tmp_root / "config" / "backtest_criteria.yaml"
    crit.write_text(crit.read_text().replace("value: 30", "value: 3"))    # rules changed after confirmation
    assert main(["backtest", "run"], paths=paths, clock=clock, factories=f) == EXIT_CONFIRM
    from perpbot.storage import Store

    s = Store(paths.db_file, clock, "x", "x")
    assert [r["event"] for r in s.query("SELECT event FROM backtest_log ORDER BY id")] == ["criteria_confirmed", "run"]
    s.close()


def test_download_is_incremental(tmp_path, cfg):
    class FakeBn:
        def __init__(self):
            self.calls = []

        def klines_range(self, interval, start_ms, end_ms):
            self.calls.append((interval, start_ms, end_ms))
            step = bt.INTERVALS[interval]
            return [Candle(t, 1.0, 1.0, 1.0, 1.0, 1.0, t + step) for t in range(start_ms, min(end_ms, start_ms + 5 * step), step)]

        def funding(self, start_ms, end_ms):
            return [(start_ms + k * 8 * HOUR_MS, 0.0001, 1.0) for k in range(3)]

    bn = FakeBn()
    now = day_start_ms(date(2021, 1, 10))
    c1 = bt.download(bn, tmp_path, date(2021, 1, 1), now)
    c2 = bt.download(bn, tmp_path, date(2021, 1, 1), now)
    assert c1["1d"] == 5 and c2["1d"] == 9                          # second call continued where the first stopped
    starts_1d = [c[1] for c in bn.calls if c[0] == "1d"]
    assert starts_1d[1] == starts_1d[0] + 5 * DAY_MS
    assert json.dumps(c2)
