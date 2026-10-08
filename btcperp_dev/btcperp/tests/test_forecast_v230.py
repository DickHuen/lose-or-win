"""v2.3.0 (owner 2026-10-08): forecast engine, early reversal warnings, dynamic exit (backtest / live shadow only),
early-reversal entries (backtest only), the fair comparison and the walk-forward check. The forecast never places,
changes or cancels an order, uses no future candle, and a failed forecast leaves the run as v2.2.0."""

from __future__ import annotations

import json

import pytest

from perpbot import forecast as fc
from perpbot import forecast_eval as fe
from perpbot import intraday as idy
from perpbot import intraday_bt as ibt
from perpbot import intraday_compare as icmp
from perpbot.analysis import render_forecast
from perpbot.candles import CandleCache
from perpbot.config import ConfigError, config_from_dict
from perpbot.indicators import Candle
from perpbot.records import Records

from conftest import shipped_intraday_config
from intraday_helpers import H1, H4, M15, T0, IntradayBinance, IWorld, agg, mirror, random_15m
from test_intraday_v200 import I0, at, path


def cfgd(**sections):
    d = shipped_intraday_config()
    for sec, vals in sections.items():
        d[sec].update(vals)
    return d


def params(**fover):
    d = cfgd(forecast=dict(calib_days=60, min_samples=40, **fover))
    cfg = config_from_dict(d)
    return idy.Params.from_cfg(cfg), fc.FParams.from_cfg(cfg), cfg


@pytest.fixture(scope="module")
def walk():
    m15 = random_15m(96 * 90, 11)
    return m15, agg(m15, H1), agg(m15, H4)


# ================================================================ config
def test_shipped_values_keep_live_trading_unchanged():
    cfg = config_from_dict(shipped_intraday_config())
    assert cfg.config_version == "2.3.0"
    assert cfg.forecast.enabled is True and cfg.forecast.entry_filter is False and cfg.early_reversal.enabled is False
    assert cfg.dynamic_exit.mode == "shadow"
    ic = cfg.intraday                                             # the v2.2.0 intraday values
    assert (ic.tp1_r, ic.tp2_r, ic.min_room_r, ic.max_hold_hours, ic.no_progress_hours) == (0.8, 3.0, 0.8, 12, 6)
    assert (cfg.risk.notional_multiple_full_tier, cfg.risk.leverage, cfg.risk.kill_drawdown_pct) == (20, 25, 95)


@pytest.mark.parametrize("sec,key,value", [("dynamic_exit", "mode", "live"), ("forecast", "entry_filter", True),
                                           ("early_reversal", "enabled", True), ("forecast", "horizons_min", [7]),
                                           ("dynamic_exit", "tp1_r", 9.0), ("forecast", "min_samples", 1)])
def test_live_switches_are_refused_in_v230(sec, key, value):
    d = shipped_intraday_config()
    d[sec][key] = value
    with pytest.raises(ConfigError):
        config_from_dict(d)


# ================================================================ forecast: no look-ahead, symmetry, live == backtest
def test_no_future_candle_changes_the_forecast(walk):
    m15, h1, h4 = walk
    ip, fp, _ = params()
    t = m15[96 * 75].open_ms + 60_000
    a = fc.forecast_now(h1, h4, m15, t, ip, fp)
    cut = [c for c in m15 if c.close_ms <= t]
    spiked = cut + [Candle(c.open_ms, c.open, c.high * 1.2, c.low * 0.8, c.close * 1.1, 9.0, c.close_ms)
                    for c in m15 if c.close_ms > t]
    b = fc.forecast_now(agg(spiked, H1), agg(spiked, H4), spiked, t, ip, fp)
    assert a is not None and json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_backtest_incremental_model_equals_the_live_build(walk):
    m15, h1, h4 = walk
    ip, fp, _ = params()
    inc = fc.Incremental(fc.Frame(h1, h4, m15, ip, fp))
    for k in (61, 70, 70, 82, 89):                     # same time twice is fine, never backwards
        t = m15[96 * k].open_ms + 60_000
        live = fc.forecast_now(h1, h4, m15, t, ip, fp)
        bt = inc.forecast(t)
        assert json.dumps(live, sort_keys=True, default=str) == json.dumps(bt, sort_keys=True, default=str), k
    with pytest.raises(ValueError):
        inc.forecast(m15[96 * 60].open_ms)


def test_samples_are_only_finished_outcomes(walk):
    m15, h1, h4 = walk
    ip, fp, _ = params()
    fr = fc.Frame(h1, h4, m15, ip, fp)
    t = fr.close_ms[24 * 70]
    model = fc.build(fr, t)
    newest = max(fr.close_ms[i] for i in range(len(fr.h1)) if fr.close_ms[i] + fp.outcome_hours() * H1 <= t)
    assert newest == t - fp.outcome_hours() * H1
    n = sum(1 for i in range(len(fr.h1)) if fr.ok(i) and t - fp.calib_days * 86_400_000 <= fr.close_ms[i] <= newest
            and fr.outcomes(i, 1) is not None)
    assert model.count == n > 0


def test_forecast_is_mirror_symmetric(walk):
    m15, h1, h4 = walk
    ip, fp, _ = params()
    mm = mirror(m15, 86_000.0)
    for k in (65, 80):
        t = m15[96 * k].open_ms + 60_000
        a = fc.forecast_now(h1, h4, m15, t, ip, fp)
        b = fc.forecast_now(agg(mm, H1), agg(mm, H4), mm, t, ip, fp)
        assert a["direction"] == -b["direction"] and a["regime"].replace("up", "x").replace("down", "x") == \
            b["regime"].replace("up", "x").replace("down", "x")
        assert a["p_up_first"] == pytest.approx(b["p_down_first"]) and a["p_down_first"] == pytest.approx(b["p_up_first"])
        assert a["warnings"]["top"]["level"] == b["warnings"]["bottom"]["level"]
        assert a["up_room_usd"] == pytest.approx(b["down_room_usd"])
        assert a["ranges"]["60"]["high"] - a["price"] == pytest.approx(b["price"] - b["ranges"]["60"]["low"])


def test_probabilities_are_counted_frequencies():
    fp = params()[1]
    model = fc.Model(fp)
    key = (1, 2, 1, 0, 1)
    outs = [1] * 30 + [-1] * 15 + [0] * 5
    for i, ps in enumerate(outs):
        model.update(key, {"fwd": {60: 0.1 * (i % 7 - 3), 120: 0.0}, "passage": ps, "fav": 1.0 + i / 50,
                           "adv": 0.5}, +1)
    st = idy.Structure(1, [idy.Swing(0, 0, 90.0), idy.Swing(5, 5, 110.0)], [idy.Swing(2, 2, 80.0), idy.Swing(7, 7, 95.0)])
    feat = {"t_ms": 0, "d": 1, "atr": 10.0, "key": key, "structure": st, "bias": 1, "vol_ratio": 1.0, "hi24": 112.0,
            "lo24": 94.0, "warn_key": {1: fc.Warning(1), -1: fc.Warning(-1)}}
    f = model.forecast(feat, 100.0)
    assert f["p_continuation"] == pytest.approx(30 / 50) and f["p_reversal"] == pytest.approx(15 / 50)
    assert f["p_up_first"] == f["p_continuation"] and f["passage_n"] == 50 and f["passage_level"] == 0
    assert f["reach"]["up"]["500"] == 0.0 and f["up_room_usd"] == pytest.approx(10.0 * (1.0 + 24.5 / 50))
    assert f["invalidation"] == 95.0 and f["resistance"] == 110.0 and f["support"] == 95.0
    # a bucket with too few samples falls back to a coarser one
    f2 = model.forecast({**feat, "key": (1, 2, 1, 0, 2)}, 100.0)
    assert f2["passage_level"] == 1 and f2["p_continuation"] == pytest.approx(0.6)


# ================================================================ early reversal warning
def top_series():
    """Sideways, then a run up into a 24h high with a rejection wick, slowing and failed tests, then a turn down."""
    out = []
    px = 80_000.0
    t = T0
    for i in range(300):                                 # sideways warm-up
        c = px + (150 if i % 2 else -150)
        out.append(Candle(t, px, max(px, c) + 60, min(px, c) - 60, c, 10.0, t + H1))
        px, t = c, t + H1
    for i in range(16):                                  # strong run up, slowing at the end
        step = 260 if i < 10 else 70
        c = px + step
        out.append(Candle(t, px, c + 40, px - 40, c, 10.0, t + H1))
        px, t = c, t + H1
    top = px + 80
    for i in range(3):                                   # tests of the high, rejected: close in the lower half
        out.append(Candle(t, px, top, px - 60, px - 5, 10.0, t + H1))
        px, t = px - 5, t + H1
    return out, top


def test_top_warning_levels_up_to_preparation():
    ip, fp, _ = params()
    h1, top = top_series()
    h4 = agg(h1, H4)
    fr = fc.Frame(h1, h4, None, ip, fp)
    i = len(h1) - 1
    st = fr.structure(i)
    w = fr.warning(i, 1, st, 1)
    assert w.level == 1 and {"failed_tests", "wick"} <= set(w.signs) and w.extreme == top
    # a 1h close below the lows of the two candles before = turned from the high -> preparation
    last = h1[-1]
    drop = Candle(last.close_ms, last.close, last.close + 10, last.close - 250, last.close - 200, 10.0, last.close_ms + H1)
    h1b = h1 + [drop]
    fr2 = fc.Frame(h1b, agg(h1b, H4), None, ip, fp)
    w2 = fr2.warning(len(h1b) - 1, 1, fr2.structure(len(h1b) - 1), 1)
    assert w2.level == 2 and "turned" in w2.note
    # mirrored: a bottom warning at the same levels
    mh = [Candle(c.open_ms, 160_000 - c.open, 160_000 - c.low, 160_000 - c.high, 160_000 - c.close, c.volume,
                 c.close_ms) for c in h1b]
    fr3 = fc.Frame(mh, agg(mh, H4), None, ip, fp)
    w3 = fr3.warning(len(mh) - 1, -1, fr3.structure(len(mh) - 1), -1)
    assert w3.level == 2 and w3.extreme == pytest.approx(160_000 - top)


def test_no_warning_without_a_move_into_the_extreme():
    ip, fp, _ = params()
    h1, _ = top_series()
    flat = h1[:300]
    fr = fc.Frame(flat, agg(flat, H4), None, ip, fp)
    w = fr.warning(len(flat) - 1, 1, fr.structure(len(flat) - 1), 0)
    assert w.level == 0 and "no move into an extreme" in w.note


# ================================================================ dynamic exit (pure)
def fcast(level=0, side="top", p_up=0.5, p_dn=0.5, signs=("wick", "slowing"), note=""):
    w = {"top": {"level": 0, "signs": []}, "bottom": {"level": 0, "signs": []}}
    w[side] = {"level": level, "signs": list(signs), "note": note}
    return {"warnings": w, "p_up_first": p_up, "p_down_first": p_dn, "up_room_usd": 600.0, "down_room_usd": 400.0}


def state(**kw):
    base = dict(direction=1, entry=100.0, r=10.0, entry_ms=0, invalidation=95.0, stage="initial", stop=90.0,
                best=112.0, worst=97.0, two_legs=True, cost_unit=1.0)
    base.update(kw)
    return idy.ManageState(**base)


def test_dynamic_actions():
    dp = idy.DynParams.from_cfg(config_from_dict(shipped_intraday_config()))
    s = state()
    assert idy.dynamic_action(s, None, 108.0, 4.0, True, dp)[0] == "HOLD"                # no forecast: baseline
    assert idy.dynamic_action(s, fcast(3, note="1h broke"), 101.0, 4.0, True, dp)[0] == "CLOSE"
    assert idy.dynamic_action(s, fcast(2), 106.0, 4.0, True, dp)[0] == "CLOSE"           # turned, +0.6 R
    assert idy.dynamic_action(s, fcast(2), 103.0, 4.0, True, dp)[0] != "CLOSE"           # +0.3 R: not yet
    assert idy.dynamic_action(s, fcast(1), 106.0, 4.0, True, dp)[0] == "REDUCE"
    act, why, new = idy.dynamic_action(s, fcast(1), 110.0, 4.0, False, dp)              # leg A gone: tighten
    assert act == "TIGHTEN_STOP" and new == pytest.approx(112.0 - 0.75 * 4.0)
    assert idy.dynamic_action(s, fcast(1), 106.0, 4.0, False, dp)[0] == "HOLD"          # already below 109: no move
    assert idy.dynamic_action(s, fcast(1, side="bottom"), 106.0, 4.0, True, dp)[0] == "HOLD"   # not against a long
    sh = state(direction=-1, entry=100.0, stop=110.0, best=88.0, worst=103.0)            # mirrored short
    assert idy.dynamic_action(sh, fcast(1, side="bottom"), 94.0, 4.0, True, dp)[0] == "REDUCE"
    assert idy.dynamic_action(sh, fcast(2, side="bottom"), 94.0, 4.0, True, dp)[0] == "CLOSE"
    act, _, new = idy.dynamic_action(sh, fcast(1, side="bottom"), 90.0, 4.0, False, dp)
    assert act == "TIGHTEN_STOP" and new == pytest.approx(88.0 + 3.0)


def test_tighten_never_loosens_nor_jumps_past_the_price():
    dp = idy.DynParams.from_cfg(config_from_dict(shipped_intraday_config()))
    s = state(stop=110.0, stage="runner")
    assert idy.dynamic_action(s, fcast(1), 111.0, 4.0, False, dp)[0] == "HOLD"           # 109 would loosen
    s2 = state(best=112.0, stop=90.0)
    assert idy.dynamic_action(s2, fcast(1), 108.5, 4.0, False, dp)[0] == "HOLD"          # 109 > price 108.5


def test_hold_extension_only_for_a_healthy_runner():
    dp = idy.DynParams.from_cfg(config_from_dict(shipped_intraday_config()))
    assert idy.hold_extended(state(stage="runner"), fcast(0, p_up=0.55, p_dn=0.45), dp)
    assert not idy.hold_extended(state(stage="runner"), fcast(0, p_up=0.45, p_dn=0.55), dp)
    assert not idy.hold_extended(state(stage="runner"), fcast(1), dp)
    assert not idy.hold_extended(state(stage="initial"), fcast(0, p_up=0.6, p_dn=0.4), dp)


def test_entry_filter():
    assert fc.entry_veto(None, 1) is None
    assert "against the entry" in fc.entry_veto(fcast(1), 1)
    assert fc.entry_veto(fcast(1), -1) is None                                          # a top warning, short side
    f = {**fcast(0, p_up=0.35, p_dn=0.55), "confidence": "medium"}
    assert "went against" in fc.entry_veto(f, 1) and fc.entry_veto(f, -1) is None
    assert fc.entry_veto({**f, "confidence": "low"}, 1) is None


# ================================================================ early reversal entry (backtest variant E)
def test_early_reversal_needs_preparation_and_a_15m_turn():
    ip = idy.Params.from_cfg(config_from_dict(shipped_intraday_config()))
    bars = [Candle(T0 + i * M15, 100.0, 101.0, 99.0, 100.5, 1, T0 + (i + 1) * M15) for i in range(3)]
    bars.append(Candle(T0 + 3 * M15, 100.5, 100.6, 99.0, 99.2, 1, T0 + 4 * M15))        # turns down
    w = {"top": {"level": 2, "extreme": 103.0, "extreme_ms": 5, "signs": ["wick"]}, "bottom": {"level": 0}}
    s = idy.early_reversal(w, bars, ip)
    assert len(s) == 1 and s[0].ok and s[0].direction == -1 and s[0].stop == 103.0
    w1 = {"top": {"level": 1, "extreme": 103.0, "extreme_ms": 5}, "bottom": {"level": 0}}
    assert idy.early_reversal(w1, bars, ip) == []                                      # a warning is not an entry
    up = bars[:3] + [Candle(T0 + 3 * M15, 100.5, 102.0, 100.4, 101.8, 1, T0 + 4 * M15)]
    assert not idy.early_reversal(w, up, ip)[0].ok                                     # no turn down


def test_evaluate_adds_early_reversal_only_when_asked():
    m15 = path((I0 + 120, 87_400))
    ip = idy.Params.from_cfg(config_from_dict(shipped_intraday_config()))
    w = {"top": {"level": 2, "extreme": 99_999.0, "extreme_ms": 1}, "bottom": {"level": 0}}
    kw = dict(cost_unit_fn=lambda d, e: e * 0.0014, history=idy.History())
    t = at(86)                                                                          # no entry here
    a = idy.evaluate(ip, t, m15, agg(m15, H1), agg(m15, H4), **kw)
    b = idy.evaluate(ip, t, m15, agg(m15, H1), agg(m15, H4), warnings=w, **kw)
    c = idy.evaluate(ip, t, m15, agg(m15, H1), agg(m15, H4), warnings=w, early=True, **kw)
    assert a.action != "enter" and a.to_dict() == b.to_dict()
    assert any(s["kind"] == "early_reversal" for s in c.setups)
    assert not any(s["kind"] == "early_reversal" for s in a.setups)


# ================================================================ backtest variants
def test_variant_a_is_the_live_rules_and_c_changes_the_targets():
    m15 = random_15m(96 * 40, 5)
    cfg = config_from_dict(shipped_intraday_config())
    data = ibt.Data([], m15, agg(m15, H1), agg(m15, H4), [])
    start, end = m15[0].open_ms + 30 * 96 * M15, m15[-1].close_ms
    plain = ibt.Sim(cfg, data, "base", "owner", 100.0).run(start, end)
    a = ibt.Sim(cfg, data, "base", "owner", 100.0, variant=ibt.VARIANTS["A"]).run(start, end)
    key = lambda r: [(t.entry_ms, t.reason, round(t.net, 9)) for t in r["trades"]]  # noqa: E731
    assert key(plain) == key(a) and plain["trades"]
    c = ibt.Sim(cfg, data, "base", "owner", 100.0, variant=ibt.VARIANTS["C0"]).run(start, end)
    for t in c["trades"]:
        if t.tp1 is not None:
            assert abs(t.tp1 - t.entry) == pytest.approx(1.5 * t.r) and abs(t.tp2 - t.entry) == pytest.approx(6.0 * t.r)


def test_a_failed_forecast_falls_back_to_the_baseline(monkeypatch):
    m15 = random_15m(96 * 40, 8)
    cfg = config_from_dict(shipped_intraday_config())
    data = ibt.Data([], m15, agg(m15, H1), agg(m15, H4), [])
    start, end = m15[0].open_ms + 30 * 96 * M15, m15[-1].close_ms
    monkeypatch.setattr(fc.Incremental, "forecast", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    b = ibt.Sim(cfg, data, "base", "owner", 100.0, variant=ibt.VARIANTS["B"]).run(start, end)
    a = ibt.Sim(cfg, data, "base", "owner", 100.0).run(start, end)
    assert [(t.entry_ms, t.reason) for t in a["trades"]] == [(t.entry_ms, t.reason) for t in b["trades"]]


def test_precomputed_forecasts_give_the_same_runs():
    m15 = random_15m(96 * 40, 3)
    cfg = config_from_dict(shipped_intraday_config())
    data = ibt.Data([], m15, agg(m15, H1), agg(m15, H4), [])
    start, end = m15[0].open_ms + 30 * 96 * M15, m15[-1].close_ms
    pre = ibt.precompute_forecasts(cfg, data, start, end)
    for v in ("C", "E"):
        x = ibt.Sim(cfg, data, "base", "owner", 100.0, variant=ibt.VARIANTS[v]).run(start, end)
        y = ibt.Sim(cfg, data, "base", "owner", 100.0, variant=ibt.VARIANTS[v], forecasts=pre).run(start, end)
        assert [(t.entry_ms, t.reason, round(t.net, 9)) for t in x["trades"]] == \
            [(t.entry_ms, t.reason, round(t.net, 9)) for t in y["trades"]]


def test_compare_report_has_every_variant_segment_and_older_profile():
    m15 = random_15m(96 * 34, 4)
    cfg = config_from_dict(shipped_intraday_config())
    data = ibt.Data([], m15, agg(m15, H1), agg(m15, H4), [])
    start, end = m15[0].open_ms + 30 * 96 * M15, m15[-1].close_ms
    rep = icmp.compare(cfg, data, start, end, scenarios=("base",), sizings=("owner", "risk1"), segments=2)
    assert set(rep["runs"]) == {f"{v}/base/{z}" for v in ("A", "B", "C", "C0", "E") for z in ("owner", "risk1")}
    assert {k.split("/")[0] for k in rep["segments"]} == {"1", "2"}
    assert set(rep["profiles"]) == {"v2.1.0/base/owner", "v2.0.0/base/owner"}
    text = icmp.report_md(rep, "t")
    assert "Walk-forward" in text and "captured >=500/1000/2000" in text and "risk1" in text
    s = rep["runs"]["A/base/owner"]
    for k in ("profit_factor", "avg_win_usd", "avg_loss_usd", "avg_captured_usd", "avg_giveback_usd", "captured",
              "by_side", "by_regime"):
        assert k in s or s["trades"] == 0


def test_walk_forward_check_reports_every_quarter(walk):
    m15, h1, h4 = walk
    ip, fp, _ = params()
    res = fe.walk_forward(h1, h4, m15, ip, fp, h1[0].open_ms + 62 * 86_400_000, h1[-1].open_ms)
    assert res["hours"] > 300 and 0 <= res["ranges"]["60"]["coverage_10_90"] <= 1
    assert res["ranges"]["15"]["n"] > 0 and res["passage"]["n"] > 0 and res["quarters"]
    text = fe.report_md(res, "t")
    assert "Every quarter" in text and "Early reversal warning" in text


# ================================================================ live: analysis only
def live_cfg(**over):
    d = shipped_intraday_config()
    d["forecast"].update(calib_days=30)
    for k, v in over.items():
        d[k].update(v)
    return d


def test_live_forecast_is_stored_and_never_trades(tmp_path):
    m15 = path((I0 + 120, 87_400))
    on = IWorld(tmp_path / "on", live_cfg(), m15)
    off = IWorld(tmp_path / "off", live_cfg(forecast={"enabled": False}, dynamic_exit={"mode": "off"}), m15)
    for w in (on, off):
        w.run_range(at(84), at(100))
    assert on.fok_calls() == off.fok_calls() and on.fok_calls()
    assert [c[0] for c in on.ex.calls] == [c[0] for c in off.ex.calls]                 # the very same exchange calls
    rows = on.store.query("SELECT bar, data FROM forecasts ORDER BY id")
    assert len(rows) == 17 and not off.store.query("SELECT bar FROM forecasts")
    f = rows[-1]["data"]["forecast"]
    assert not f.get("error") and f["ranges"]["60"]["low"] < f["price"] < f["ranges"]["60"]["high"]
    dec = on.decisions()[-1]["data"]
    assert "forecast" in dec["intraday"] and any(x.startswith("預測（只作分析，唔會落單）") for x in dec["analysis"])
    shadow = [r["data"]["shadow"] for r in rows if r["data"]["shadow"]]
    assert shadow and all(s["executed"] is False for s in shadow)


def test_live_forecast_failure_changes_nothing(tmp_path, monkeypatch):
    m15 = path((I0 + 120, 87_400))
    monkeypatch.setattr(fc, "forecast_now", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    w = IWorld(tmp_path / "x", live_cfg(), m15)
    ref = IWorld(tmp_path / "y", live_cfg(forecast={"enabled": False}), m15)
    for x in (w, ref):
        x.run_range(at(84), at(92))
    assert w.fok_calls() == ref.fok_calls() and Records(w.store).open_trade()
    f = w.store.query("SELECT data FROM forecasts ORDER BY id")[-1]["data"]["forecast"]
    assert "RuntimeError" in f["error"]
    assert any("預測：暫時冇" in x for x in w.decisions()[-1]["data"]["analysis"])


def test_history_backfill_for_the_forecast_respects_the_pause(tmp_path):
    from perpbot.storage import Store
    from perpbot.timeutil import FixedClock, from_ms

    m15 = random_15m(96 * 40, 2)
    bn = IntradayBinance(m15)
    clock = FixedClock(from_ms(m15[-1].close_ms))
    store = Store(tmp_path / "db.sqlite3", clock, "2.3.0", "test")
    cache = CandleCache(store, bn, backfill_days=10)
    now = m15[-1].close_ms
    cache.update(("1h",), now)
    first = cache.first_open("1h")
    assert first >= now - 10 * 86_400_000 - H1
    out = cache.backfill_older("1h", now - 35 * 86_400_000, now, max_requests=1)
    assert out["fetched"] > 0 and cache.first_open("1h") == now - 35 * 86_400_000 // H1 * H1 or \
        cache.first_open("1h") < first
    bars = cache.load("1h", 0, now)
    assert all(b.open_ms - a.open_ms == H1 for a, b in zip(bars, bars[1:]))           # no gap at the join
    bn.blocked_until_ms = now + 1
    store.insert("data_health", kind="binance_backoff", data={"until_ms": now + 600_000, "status": 418})
    n = len(bn.requests)
    assert cache.backfill_older("1h", 0, now)["error"] and len(bn.requests) == n


def test_forecast_text_is_honest_about_direction():
    f = {"regime": "trend_up", "confidence": "low", "passage_n": 312, "ranges": {"60": {"low": 85_000, "high": 86_000}},
         "atr1h": 400, "p_up_first": 0.52, "p_down_first": 0.46, "reach": {"up": {"500": 0.6}, "down": {}},
         "warnings": {"top": {"level": 2, "level_zh": "準備轉勢", "signs": ["wick"], "extreme": 86_200},
                      "bottom": {"level": 0}}, "shadow": {"action": "REDUCE", "why": "x"}}
    text = "\n".join(render_forecast(f))
    assert "唔會落單" in text and "擲毫" in text and "準備轉勢" in text and "預警唔係入場訊號" in text
    assert "影子，唔會執行" in text and "減倉" in text
