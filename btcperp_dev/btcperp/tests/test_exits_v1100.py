"""v1.10.0 (owner 2026-10-04: "the target is too far, the smaller swings are missed"): the SL / TP follow the hourly
swings: 2 x / 3 x the Wilder ATR(14) of the 1h candles closed at the decision period's start, at least 0.5% / 0.8% of
the price. Live 2026-10-03: a x10 long from 84,574 had its TP +4.2% away (1.5 daily ATR) while BTC moved +/-0.5% in
the next 30 hours."""

from datetime import date, datetime, timedelta, timezone

import pytest

from perpbot import backtest as bt
from perpbot import strategy as st
from perpbot.config import ConfigError, config_from_dict
from perpbot.indicators import Candle
from perpbot.records import Records
from perpbot.timeutil import HOUR_MS, day_start_ms, to_ms

from conftest import P, World, hkt, ok_leverage, shipped_config
from test_backtest import synthetic_dataset
from test_rolling import _cal, _root

PX = 85_000.0
T0 = day_start_ms(date(2026, 10, 3))


def hours(n, rng, start=T0, px=PX):
    """n flat 1h candles ending at `start`, each with high - low = rng (so every true range = rng)."""
    return [Candle(start - (n - i) * HOUR_MS, px, px + rng / 2, px - rng / 2, px, 1.0, start - (n - i - 1) * HOUR_MS)
            for i in range(n)]


def test_shipped_exits_follow_the_1h_atr():
    ex = config_from_dict(shipped_config()).exits
    assert (ex.atr_source, ex.atr_1h_period, ex.sl_atr_multiple, ex.tp_atr_multiple) == ("1h", 14, 2.0, 3.0)
    assert (ex.sl_min_pct, ex.tp_min_pct) == (0.5, 0.8)


def test_distances_from_the_1h_atr():
    cfg = config_from_dict(shipped_config())
    e = st.exit_distances(cfg, PX, 2_300.0, hours(300, 300.0), T0)
    assert e["source"] == "1h" and e["atr"] == pytest.approx(300.0)
    assert (e["sl_dist"], e["tp_dist"]) == pytest.approx((600.0, 900.0))       # 2 x / 3 x 300
    assert e["sl_pct"] == pytest.approx(600 / PX * 100) and e["tp_pct"] == pytest.approx(900 / PX * 100)


def test_quiet_market_uses_the_floors():
    cfg = config_from_dict(shipped_config())
    e = st.exit_distances(cfg, PX, 2_300.0, hours(300, 100.0), T0)
    assert e["atr"] == pytest.approx(100.0)
    assert (e["sl_dist"], e["tp_dist"]) == pytest.approx((0.005 * PX, 0.008 * PX))     # 0.5% / 0.8%


def test_only_candles_closed_at_the_period_start_count():
    cfg = config_from_dict(shipped_config())
    h1 = hours(300, 300.0) + [Candle(T0, PX, PX + 5_000, PX - 5_000, PX, 1.0, T0 + HOUR_MS)]   # after T0
    assert st.exit_distances(cfg, PX, 2_300.0, h1, T0)["atr"] == pytest.approx(300.0)
    assert st.exit_distances(cfg, PX, 2_300.0, h1, T0 + HOUR_MS)["atr"] > 300.0


def test_falls_back_to_the_daily_atr_without_1h_candles():
    cfg = config_from_dict(shipped_config())
    for h1 in (None, [], hours(10, 300.0)):
        e = st.exit_distances(cfg, PX, 2_300.0, h1, T0)
        assert e["source"] == "daily" and e["sl_dist"] == pytest.approx(4_600.0)


def test_daily_source_is_the_v18_behaviour():
    d = shipped_config()
    d["exits"].update(atr_source="daily", sl_atr_multiple=1.0, tp_atr_multiple=1.5, sl_min_pct=0, tp_min_pct=0)
    e = st.exit_distances(config_from_dict(d), PX, 2_300.0, hours(300, 300.0), T0)
    assert e["source"] == "daily" and (e["sl_dist"], e["tp_dist"]) == pytest.approx((2_300.0, 3_450.0))


@pytest.mark.parametrize("key,value", [("atr_source", "4h"), ("atr_1h_period", 1), ("tp_min_pct", -1)])
def test_exit_config_is_validated(key, value):
    d = shipped_config()
    d["exits"][key] = value
    with pytest.raises(ConfigError):
        config_from_dict(d)


# ---------------------------------------------------------------- live engine (hourly world)
def test_entry_brackets_follow_the_1h_atr(tmp_path, hourly_cfg_dict):
    hourly_cfg_dict["exits"].update(atr_source="1h", atr_1h_period=14, sl_atr_multiple=2.0, tp_atr_multiple=3.0,
                                    sl_min_pct=0.5, tp_min_pct=0.8)
    w = World(tmp_path, hkt(2026, 10, 5, 12, 30), hourly_cfg_dict)
    ok_leverage(w.ex)
    t = to_ms(datetime(2026, 10, 5, 4, tzinfo=timezone.utc))
    for o in range(t - 400 * HOUR_MS, t, HOUR_MS):                         # quiet hours: range 0.3%
        w.bn.h1[o] = Candle(o, P, P + 150, P - 150, P, 1.0, o + HOUR_MS)
    w.bn.signal_1h(t, "strong_long")
    w.decide()
    assert w.pos() > 0
    plan = w.store.latest("decisions", "score IS NOT NULL")["data"]["plan"]
    want = st.exit_distances(w.cfg, plan["mark"], plan["atr"], sorted(w.bn.h1.values(), key=lambda c: c.open_ms), t)
    assert plan["exit"]["source"] == "1h" and plan["exit"]["sl_dist"] == pytest.approx(want["sl_dist"])
    tr = Records(w.store).open_trade()
    ref = P + w.ex.spread / 2
    assert tr["sl_price"] == pytest.approx(ref - want["sl_dist"], abs=10)
    assert tr["tp_price"] == pytest.approx(ref + want["tp_dist"], abs=10)
    assert want["tp_dist"] < 0.02 * P                                        # far closer than 1.5 daily ATR
    text = "\n".join(w.store.latest("decisions", "score IS NOT NULL")["data"]["analysis"])
    assert "止損止賺跟 1 小時波幅" in text


# ---------------------------------------------------------------- backtest uses the same distances
def test_backtest_brackets_match_live():
    from conftest import as_daily, with_test_risk

    d = with_test_risk(as_daily(shipped_config()))
    d["exits"].update(atr_source="1h", atr_1h_period=14, sl_atr_multiple=2.0, tp_atr_multiple=3.0,
                      sl_min_pct=0.5, tp_min_pct=0.8)
    cfg = config_from_dict(d)
    ds = synthetic_dataset(date(2020, 1, 1), 440, seed=4)
    cal = bt.merged_calendar(cfg, _root(), _cal())
    d0, days = date(2021, 2, 1), 12
    di = bt.DayInputs(cfg, ds, cal)
    feats = {d0 + timedelta(days=i): di.features(d0 + timedelta(days=i)) for i in range(days + 1)}
    p1 = bt.PeriodInputs(cfg, ds, cal, cadence="rolling_1h")
    pf1 = {day_start_ms(d0) + i * HOUR_MS: p1.features(day_start_ms(d0) + i * HOUR_MS) for i in range(days * 24 + 1)}
    sim = bt.Simulator(cfg, ds.h1, ds.funding, feats, 0.0005, {}, None, pf1)
    res = sim.run("R1h_live", d0, d0 + timedelta(days=days), 0, 10_000.0)
    assert res.trades
    for t in res.trades:
        want = st.exit_distances(cfg, t.entry_price, t.atr, ds.h1, st.key_ms(t.entry_day))
        assert abs(t.entry_price - t.sl) == pytest.approx(want["sl_dist"])
        assert abs(t.tp - t.entry_price) == pytest.approx(want["tp_dist"])
