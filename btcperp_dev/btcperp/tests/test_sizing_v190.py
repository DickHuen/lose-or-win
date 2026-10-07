"""v1.8.0 / v1.9.0 (owner 2026-10-02): the analysis rules with positions by score. v1.9.0 map: below 20 no new
position, 20-40 x3, 40-50 x6, 50-75 x10, 75 and above x20 equity, each trade at the lowest isolated leverage its
margin needs (x20 -> 22x), cut when the stop is so wide that the liquidation would come too close (volatility cap).
Live 2026-10-02 12:30: score +73.8 held a ~0.0002 BTC position that made under $1 on +2,887 points."""

from datetime import date
from decimal import Decimal

import pytest

from perpbot.config import ConfigError, config_from_dict
from perpbot.exchange.mock import default_instrument
from perpbot.records import Records
from perpbot.risk import compute_size, estimate_liquidation, quantize_qty, trade_leverage
from perpbot.strategy import DecisionContext, evaluate_entry, tier_fraction

from conftest import P, hkt, shipped_config

LIVE = dict(quantity_decimals=5, price_decimals=1, min_notional=10.0, max_leverage=50, price_bounds=0.02,
            risk_tiers=[(0.0, 50), (250_000.0, 25), (1_000_000.0, 20)])
PRICE, ATR = 86_841.0, 2_281.0                          # the 12:30 analysis (ATR 2.63%)
D1, D2 = date(2026, 10, 5), date(2026, 10, 6)


def _inst():
    inst = default_instrument()
    for k, v in LIVE.items():
        setattr(inst, k, v)
    return inst


def _lev(multiple, sl_pct, cfg=None):
    r = (cfg or config_from_dict(shipped_config())).risk
    return trade_leverage(multiple=multiple, sl_pct=sl_pct, max_leverage=int(r.leverage),
                          margin_use_pct=float(r.max_margin_use_pct), liq_multiple=float(r.liq_min_sl_multiple),
                          inst=_inst(), notional=100.0 * multiple, mmr_divisor=float(r.liq_estimate_mmr_divisor))


def test_shipped_v190_values():
    cfg = config_from_dict(shipped_config())
    s, r = cfg.strategy, cfg.risk
    assert cfg.bold.enabled is False and s.cadence == "rolling_1h"
    assert s.min_entry_abs_score == 20 and s.size_tiers == [[0, 0.15], [40, 0.30], [50, 0.50], [75, 1.00]]
    assert (r.notional_multiple_full_tier, r.leverage, r.max_margin_use_pct) == (20, 25, 92)
    assert (r.liq_min_sl_multiple, r.liq_after_fill_sl_multiple, r.cross_margin) == (1.3, 1.15, False)
    assert (cfg.exits.sl_atr_multiple, cfg.exits.tp_atr_multiple, cfg.exits.atr_source) == (2.0, 3.0, "1h")   # v1.10.0
    assert (r.kill_drawdown_pct, r.kill_losing_streak_pct) == (95, 95)
    assert (r.equity_floor_pct_of_net_funded, r.permanent_floor_pct_of_cumulative_funded) == (5, 5)
    assert r.permanent_floor_lowered_in == "1.10.0" and cfg.config_version == "2.2.0"
    assert r.live_review_expectancy_floor_r is None


@pytest.mark.parametrize("score,times", [(19.9, None), (20, 3), (39.9, 3), (40, 6), (49.9, 6), (50, 10), (74.9, 10),
                                         (75, 20), (100, 20)])
def test_owner_score_to_position_map(score, times):
    cfg = config_from_dict(shipped_config())
    s = cfg.strategy
    ctx = DecisionContext(direction=1, abs_score=score, tier_fraction=tier_fraction(score, s), caps=[],
                          funding_pct=None, funding_high=95, funding_low=5, event_active=False, position_dir=0,
                          opposite_streak=0, entered_today=False, paused_reason=None, flip_min_abs_score=30,
                          opposite_days_rule=72, event_allows_rule_closes=True,
                          min_entry_abs_score=float(s.min_entry_abs_score))
    d, frac, blocked = evaluate_entry(ctx)
    if times is None:
        assert d == 0 and any("below the entry threshold 20" in b for b in blocked)
        return
    assert d == 1 and frac * cfg.risk.notional_multiple_full_tier == pytest.approx(times)
    capped = DecisionContext(**{**ctx.__dict__, "caps": [("h4_trend", 0.5)]})   # a gate against the trade: <= x10
    assert evaluate_entry(capped)[1] * 20 == pytest.approx(min(times, 10))


@pytest.mark.parametrize("bad", [[[10, 0.5]], [[0, 0.5], [40, 0.3]], [[0, 0.5], [40, 1.5]], [[0, 0.5], [30, 0.6], [30, 0.7]],
                                 "x"])
def test_size_tiers_are_validated(bad):
    with pytest.raises(ConfigError, match="size_tiers"):
        config_from_dict(dict(shipped_config(), strategy=dict(shipped_config()["strategy"], size_tiers=bad)))


@pytest.mark.parametrize("multiple,lev", [(3, 4), (6, 7), (10, 11), (20, 22)])
def test_lowest_leverage_per_trade(multiple, lev):
    """Margin = position / leverage must fit in 92% of equity: the lowest such leverage, the farthest liquidation."""
    got_lev, got, note = _lev(multiple, ATR / PRICE)
    assert (got_lev, got, note) == (lev, multiple, None)
    liq = estimate_liquidation(PRICE, 1, got_lev, _inst(), 100.0 * multiple, 2)
    assert (PRICE - liq) / PRICE >= 1.3 * ATR / PRICE                       # >= 1.3 x the 1.0 ATR stop


@pytest.mark.parametrize("sl_pct,lev,cut", [(0.03, 20, 18.4), (0.04, 16, 14.72), (0.20, 3, 2.76)])
def test_volatility_cuts_the_x20_position(sl_pct, lev, cut):
    got_lev, got, note = _lev(20, sl_pct)
    assert got_lev == lev and got == pytest.approx(cut) and "volatility" in note
    assert 1 / got_lev - 0.01 >= 1.3 * sl_pct                               # the liquidation stays 1.3 x beyond


def test_no_leverage_possible_for_an_absurd_stop():
    assert _lev(3, 0.8)[:2] == (0, 0.0)


def test_positions_on_100_usdc():
    s = compute_size(equity=100.0, risk_pct=5, fraction=1.0, price=PRICE, atr=ATR, sl_atr_multiple=1.0,
                     notional_cap_pct=150, leverage=22, inst=_inst(), notional_multiple=20)
    assert s.ok and s.qty == Decimal("0.02303") and s.notional <= 2_000
    assert s.risk_usd == pytest.approx(0.02303 * ATR)                       # x20: ~$53 at the 1.0 ATR stop


def test_config_refuses_a_multiple_the_margin_cannot_carry():
    with pytest.raises(ConfigError, match="needs more than risk.leverage"):
        config_from_dict(dict(shipped_config(), risk=dict(shipped_config()["risk"], leverage=21)))   # 20 > 21 x 0.92


# ---------------------------------------------------------------- engine, live-like BTC-USD, 100 USDC (daily world)
def v190_world(world, start, cash=100.0, **extra):
    kw = dict(risk__notional_multiple_full_tier=20, risk__leverage=25, risk__max_margin_use_pct=92,
              risk__liq_min_sl_multiple=1.3, risk__liq_after_fill_sl_multiple=1.15,
              strategy__min_entry_abs_score=20, strategy__size_tiers=[[0, 0.15], [40, 0.30], [50, 0.50], [75, 1.00]],
              exits__sl_atr_multiple=1.0, exits__tp_atr_multiple=1.5)
    w = world(start, **{**kw, **extra})
    for k, v in LIVE.items():
        setattr(w.ex.inst, k, v)
    w.ex.cash = cash
    return w


def _plan(w):
    return w.store.latest("decisions", "score IS NOT NULL")["data"]["plan"]


def test_strong_signal_opens_at_the_lowest_leverage(world):
    w = v190_world(world, hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.decide()
    assert w.pos() > 0
    plan = _plan(w)
    mult = 20 * float(plan["enter_fraction"])
    lev = {10.0: 11, 20.0: 22}[mult]
    assert plan["sizing"] == {"multiple": mult, "leverage": lev, "note": None}
    ref = P + w.ex.spread / 2
    fok = w.fok_calls()[-1]
    assert Decimal(fok["quantity"]) == quantize_qty(100.0 * mult / ref, 5)
    assert ("update_leverage", {"leverage": lev, "cross": False}) in w.ex.calls
    t = Records(w.store).open_trade()
    assert t["sl_price"] == pytest.approx(ref - 1.0 * t["atr"], abs=10)
    assert t["tp_price"] == pytest.approx(ref + 1.5 * t["atr"], abs=10)
    opened = next(m for m in w.tg.sent if "] open:" in m)
    assert f" at {lev}x" in opened and "[ramp]" not in opened and not w.tg.has("liquidation check")
    text = "\n".join(w.store.latest("decisions", "score IS NOT NULL")["data"]["analysis"])
    assert f"注碼：倉位 = 本金 ×{mult:.3g}（{lev} 倍逐倉）" in text and f"倉位級別 本金 ×{mult:g}" in text


def test_instrument_below_the_maximum_leverage_is_not_an_error(world):
    """risk.leverage 25 is a per-trade maximum; an instrument allowing less only caps the trade."""
    w = v190_world(world, hkt(2026, 10, 5, 8, 30))
    w.ex.inst.max_leverage, w.ex.inst.risk_tiers = 20, [(0.0, 20)]
    w.bn.signal(D1, "strong_long")
    w.decide()
    lev = int(_plan(w)["sizing"]["leverage"])
    assert w.pos() > 0 and lev <= 20


def test_volatile_market_cuts_the_position_and_says_so(world):
    w = v190_world(world, hkt(2026, 10, 5, 8, 30), exits__sl_atr_multiple=4.0)    # stop ~8% away
    w.bn.signal(D1, "strong_long")
    w.decide()
    plan = _plan(w)
    want = 20 * float(plan["enter_fraction"])
    sizing = plan["sizing"]
    assert sizing["leverage"] == 8 and sizing["multiple"] == pytest.approx(8 * 0.92) and sizing["multiple"] < want
    assert w.tg.has(f"position cut from x{want:g} to x7.4 equity (volatility")
    assert w.pos() > 0 and float(w.fok_calls()[-1]["quantity"]) * (P + w.ex.spread / 2) <= 100 * 7.36 + 1
    text = "\n".join(w.store.latest("decisions", "score IS NOT NULL")["data"]["analysis"])
    assert "波動大，倉位由" in text


def test_weak_signal_below_20_does_not_enter(world):
    w = v190_world(world, hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "weak_long")
    w.decide()
    assert w.pos() == 0 and not w.fok_calls()
    assert any("below the entry threshold 20" in b for b in _plan(w)["entry_blocked"])


def test_no_top_up_while_holding(world):
    w = v190_world(world, hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.decide()
    qty = w.pos()
    w.bn.signal(D2, "strong_long")
    w.at(hkt(2026, 10, 6, 8, 30)).decide()
    assert w.pos() == qty and len(w.fok_calls()) == 1


def test_recovered_entry_is_not_over_budget(world):
    w = v190_world(world, hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    eng = w.engine()

    def boom(*a, **k):
        raise RuntimeError("process killed")
    eng._record_entry = boom
    with pytest.raises(RuntimeError):
        eng.cmd_decide()
    w.at(hkt(2026, 10, 5, 8, 50)).decide()
    t = Records(w.store).open_trade()
    assert t is not None and t["adopted"] and not w.state()["paused"] and not w.tg.has("over risk budget")
