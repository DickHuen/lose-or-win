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
    assert ids[:14] == ["C0a", "C0b", "C0c", "C1a", "C1b", "C1c", "C1d", "C2a", "C2b", "C3", "C4", "C5", "C6", "C7"]
    runs = (tmp_path / "out1" / "runs.csv").read_text().splitlines()
    assert len(runs) == 1 + len(bt.VARIANTS) * (2 * 2 + 2)
    assert r1["selection"]["note"] and r1["data_quality"]["h1"]["missing"] == 0
    assert set(r1["data_quality"]["slice_sha256"]) == {"1d", "4h", "1h", "funding"}
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


def _sim(cfg, h1, feats, funding=None):
    return bt.Simulator(cfg, h1, funding or [(day_start_ms(date(2021, 1, 1)), 0.0, 0.0)], feats, 0.0005)


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
    assert not t.gap_fill


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
    res = _sim(cfg, h1, feats).run("A_live", d0, d0 + timedelta(days=35), 0, 10_000.0, ramp=True)
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
    rep = bt.evaluate({"rules": [{"id": "R", "text": "r", "metric": "m", "op": "report"},
                                 {"id": "D", "text": "d", "scope": "data", "metric": "x", "op": "<=", "value": 1},
                                 {"id": "T", "text": "t", "use_twin": True, "metric": "m", "op": ">", "value": 0}]},
                      {"A_live": {"m": 1.0}, "A_live_stress": {"m": -1.0}}, {"x": 0.5})
    assert rep[0]["informational"] and not rep[0]["pass"] and rep[0]["value"] == 1.0
    assert rep[1]["pass"] is True and rep[2]["pass"] is False and rep[2]["value"] == -1.0


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


def test_cli_backtest_confirm_locks_everything_and_numbers_runs(tmp_root, capsys):
    import shutil

    import yaml

    from perpbot.cli import EXIT_CONFIRM, Factories, main
    from perpbot.paths import Paths
    from perpbot.storage import Store
    from perpbot.timeutil import FixedClock

    from conftest import make_env

    root = Path(__file__).resolve().parent.parent
    for f in ("calendar_history.yaml", "backtest_criteria.yaml"):
        shutil.copy2(root / "config" / f, tmp_root / "config" / f)
    (tmp_root / "perpbot").mkdir()
    for n in bt.MANIFEST_CODE:
        shutil.copy2(root / "perpbot" / n, tmp_root / "perpbot" / n)
    cfgp = tmp_root / "config" / "config.yaml"
    data = yaml.safe_load(cfgp.read_text())
    data["backtest"].update({"first_window_start": "2021-01-10", "window_months": 3, "start_offsets_days": [0]})
    cfgp.write_text(yaml.safe_dump(data))
    make_env(tmp_root)
    paths = Paths(tmp_root)
    paths.ensure()
    more = synthetic_dataset(date(2020, 1, 1), 520)
    cut = day_start_ms(date(2020, 1, 1) + timedelta(days=480))
    first = bt.Dataset([c for c in more.daily if c.open_ms < cut], [c for c in more.h4 if c.open_ms < cut],
                       [c for c in more.h1 if c.open_ms < cut], [x for x in more.funding if x[0] < cut])
    write_dataset(first, paths.data_dir / "backtest")
    f = Factories(registered_root=lambda: None)
    clock = FixedClock(datetime(2021, 6, 1, tzinfo=UTC))
    assert main(["backtest", "run"], paths=paths, clock=clock, factories=f) == EXIT_CONFIRM
    assert main(["backtest", "confirm"], paths=paths, clock=clock, factories=f) == 0
    assert "CONFIRMED" in capsys.readouterr().out
    assert main(["backtest", "run"], paths=paths, clock=clock, factories=f) == 0
    out1 = capsys.readouterr().out
    assert "Run #1 under this confirmation" in out1
    # more data downloaded later (appended): the fixed data end keeps the same slice -> allowed, numbered #2
    write_dataset(more, paths.data_dir / "backtest")
    assert main(["backtest", "run"], paths=paths, clock=clock, factories=f) == 0
    assert "Run #2 under this confirmation" in capsys.readouterr().out
    # any change to config, criteria or code is refused
    data["backtest"]["start_offsets_days"] = [0, 7]
    cfgp.write_text(yaml.safe_dump(data))
    assert main(["backtest", "run"], paths=paths, clock=clock, factories=f) == EXIT_CONFIRM
    assert "config" in capsys.readouterr().out
    data["backtest"]["start_offsets_days"] = [0]
    cfgp.write_text(yaml.safe_dump(data))
    crit = tmp_root / "config" / "backtest_criteria.yaml"
    crit.write_text(crit.read_text().replace("value: 60", "value: 6"))
    assert main(["backtest", "run"], paths=paths, clock=clock, factories=f) == EXIT_CONFIRM
    assert "criteria" in capsys.readouterr().out
    s = Store(paths.db_file, clock, "x", "x")
    assert [r["event"] for r in s.query("SELECT event FROM backtest_log ORDER BY id")] == ["criteria_confirmed", "run", "run"]
    s.close()


def test_cli_backtest_fee_never_below_smoketest(tmp_root):
    from perpbot.cli import real_fee_rate
    from perpbot.paths import Paths

    paths = Paths(tmp_root)
    paths.ensure()
    assert real_fee_rate(paths) is None
    (paths.smoketest_dir / "smoketest_20261001000000.json").write_text(json.dumps(
        {"ok": True, "results": [{"step": "fees", "ok": True, "detail": {"taker_fee_rate": 0.0007}}]}))
    assert real_fee_rate(paths) == 0.0007


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


# ---------------------------------------------------------------- review v1.3.0 items
def test_bt1_gap_through_stop_fills_at_the_open(bt_cfg):
    d0 = date(2021, 1, 1)
    h1 = _flat_hours(d0, 5, 100.0)
    k = 24 + 3
    h1[k] = Candle(h1[k].open_ms, 95.0, 95.5, 94.0, 95.0, 1.0, h1[k].close_ms)        # opens 5% down, below SL 97
    feats = {d0: _features(d0, 1, 60.0, 2.0, 100.0)}
    t = _sim(bt_cfg, h1, feats).run("A_live", d0, d0 + timedelta(days=4), 0, 10_000.0).trades[0]
    assert t.gap_fill and t.exit_reason == "SL"
    assert t.exit_price == pytest.approx(95.0 * (1 - 10 / 1e4))                      # open minus exit slippage


def test_bt6_isolated_loss_capped_at_margin(bt_cfg):
    d0 = date(2021, 1, 1)
    h1 = _flat_hours(d0, 5, 100.0)
    k = 24 + 3
    h1[k] = Candle(h1[k].open_ms, 40.0, 40.0, 40.0, 40.0, 1.0, h1[k].close_ms)        # -60% gap
    feats = {d0: _features(d0, 1, 60.0, 2.0, 100.0)}
    t = _sim(bt_cfg, h1, feats).run("A_live", d0, d0 + timedelta(days=4), 0, 10_000.0).trades[0]
    assert t.exit_price == pytest.approx(t.entry_price * (1 - 1 / 3))                 # 3x isolated: -33% max


def test_bt2_intraday_drawdown_is_counted(bt_cfg):
    d0 = date(2021, 1, 1)
    h1 = _flat_hours(d0, 5, 100.0)
    k = 24 + 6
    h1[k] = Candle(h1[k].open_ms, 100.0, 100.0, 97.2, 100.0, 1.0, h1[k].close_ms)     # dips, recovers, no stop hit
    feats = {d0: _features(d0, 1, 60.0, 2.0, 100.0)}
    res = _sim(bt_cfg, h1, feats).run("A_live", d0, d0 + timedelta(days=4), 0, 10_000.0)
    t = res.trades[0]
    dip = t.qty * (t.entry_price - 97.2)
    assert res.max_dd_pct == pytest.approx(dip / 10_000.0 * 100.0, rel=0.05)
    assert bt.run_metrics(res, 0.1)["max_drawdown_pct"] == res.max_dd_pct


def test_bt3_stress_twin_costs_more_on_every_trade(bt_cfg):
    d0 = date(2021, 1, 1)
    h1 = _flat_hours(d0, 12, 100.0)
    for day in (2, 5, 8):
        k = day * 24 + 4
        h1[k] = Candle(h1[k].open_ms, 100.0, 107.0, 99.9, 106.0, 1.0, h1[k].close_ms)
    funding = [(day_start_ms(d0) + i * 8 * HOUR_MS, 0.0003 if i % 2 else -0.0001, 0.0) for i in range(40)]
    feats = {d0 + timedelta(days=i): _features(d0 + timedelta(days=i), 1 if i % 2 == 0 else -1, 60.0, 2.0, 100.0)
             for i in range(11)}
    live = _sim(bt_cfg, h1, feats, funding).run("A_live", d0, d0 + timedelta(days=11), 0, 10_000.0)
    stress = _sim(bt_cfg, h1, feats, funding).run("A_live_stress", d0, d0 + timedelta(days=11), 0, 10_000.0)
    assert len(live.trades) == len(stress.trades) >= 3
    for a, b in zip(live.trades, stress.trades):
        assert b.net_pnl < a.net_pnl


def test_s6_windows_full_risk_full_period_ramp(bt_cfg):
    d0 = date(2021, 1, 1)
    h1 = _flat_hours(d0, 5, 100.0)
    feats = {d0: _features(d0, 1, 60.0, 20.0, 100.0)}                                  # wide stop: cap not binding
    w = _sim(bt_cfg, h1, feats).run("A_live", d0, d0 + timedelta(days=3), 0, 10_000.0, ramp=False).trades[0]
    f = _sim(bt_cfg, h1, feats).run("A_live", d0, d0 + timedelta(days=3), 0, 10_000.0, ramp=True).trades[0]
    assert w.risk_usd == pytest.approx(2 * f.risk_usd, rel=0.01)


def test_c0_gaps_while_holding_are_recorded(bt_cfg):
    d0 = date(2021, 1, 1)
    h1 = _flat_hours(d0, 5, 100.0)
    del h1[30:32]                                                                     # 2 hours missing on day 2
    feats = {d0: _features(d0, 1, 60.0, 2.0, 100.0)}
    res = _sim(bt_cfg, h1, feats).run("A_live", d0, d0 + timedelta(days=4), 0, 10_000.0)
    assert [h for _, h in res.holding_gaps] == [2.0]
    ds = bt.Dataset(h1[::24], h1[::4], h1, [(day_start_ms(d0), 0.0, 0.0)])
    q = bt.data_quality(bt_cfg, ds, d0, d0 + timedelta(days=5))
    assert q["h1"]["missing"] == 2 and q["h1"]["max_gap_hours"] == 2.0


def test_s5_variant_selection_rules():
    import yaml

    crit = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "backtest_criteria.yaml").read_text())

    def summ(r_by, seg, exp=0.3, t=3.0):
        return {"full_total_r_by_offset": r_by, "full_total_r_min": min(r_by.values()),
                "segment_expectancy_r_median": seg, "full_expectancy_r_min": exp, "segments_positive": 3,
                "t_stat_median_offset": t, "window_positive_share_median": 0.8, "window_return_pct_median": 3.0,
                "window_max_drawdown_pct_max": 8.0, "floor_hits_total": 0, "full_trades_median": 100,
                "full_max_drawdown_pct_max": 12.0, "drawdown_kills_full_median": 0, "holding_gap_hours_max": 0.0,
                "holding_gap_hours_total_full_max": 0.0}

    offs = {0: 10.0, 7: 10.0}
    seg = {"a": 0.2, "b": 0.2, "c": 0.2}
    better = {0: 20.0, 7: 20.0}
    segb = {"a": 0.4, "b": 0.4, "c": 0.1}
    sums = {n: summ(offs, seg) for n in bt.VARIANTS}
    data = {"h1_missing_share": 0.0}
    sel = bt.select_variant(crit, sums, data)
    assert sel["live_variant"] == "A_live" and sel["qualified_replacements"] == []
    sums["B_breakeven"] = summ(better, segb)
    sums["B_breakeven_stress"] = summ(better, segb)
    sel = bt.select_variant(crit, sums, data)
    assert sel["live_variant"] == "A_live" and sel["qualified_replacements"] == ["B_breakeven"]
    sums["A_live"] = summ(offs, seg, exp=-0.1)                                       # A_live fails C1a
    sel = bt.select_variant(crit, sums, data)
    assert sel["live_variant"] is None and "NOT approved automatically" in sel["note"]
    sums["B_breakeven_stress"] = summ(offs, seg)                                     # (d) fails: stress not better
    assert bt.select_variant(crit, sums, data)["qualified_replacements"] == []


def test_i7_polymarket_replay_compares_trade_by_trade(bt_cfg):
    from perpbot.config import config_from_dict

    d0 = date(2021, 1, 1)
    h1 = _flat_hours(d0, 40, 100.0)
    ds = bt.Dataset([], [], h1, [(day_start_ms(d0), 0.0, 0.0)], pm_h1=list(h1))
    feats = {d0 + timedelta(days=i): _features(d0 + timedelta(days=i), 1, 60.0, 2.0, 100.0) for i in range(40)}
    cfg = config_from_dict({**bt_cfg.to_dict(), "backtest": {**bt_cfg.to_dict()["backtest"], "pm_replay_min_days": 10}})
    rep = bt.pm_replay(cfg, ds, feats, 0.0005, d0, d0 + timedelta(days=38), 10_000.0)
    assert rep["available"] and rep["exit_agreement"] == 1.0
    ds_short = bt.Dataset([], [], h1, ds.funding, pm_h1=h1[: 24 * 5])
    assert bt.pm_replay(cfg, ds_short, feats, 0.0005, d0, d0 + timedelta(days=38), 10_000.0)["available"] is False


def test_calendar_history_is_marked_verified():
    text = (Path(__file__).resolve().parent.parent / "config" / "calendar_history.yaml").read_text()
    assert "verify:" not in text and "verified" in text
