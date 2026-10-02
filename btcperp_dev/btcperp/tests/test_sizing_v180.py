"""v1.8.0 (owner 2026-10-02): back to the analysis rules (bold mode off) with positions by score: below 20 no new
position, 20-40 x1.5, 40-70 x5, above 70 x10 equity, at 12x isolated. Live 2026-10-02 12:30: score +73.8 held a
~0.0003 BTC position that made under $1 on +2,887 points."""

from datetime import date
from decimal import Decimal

import pytest

from perpbot.config import config_from_dict
from perpbot.exchange.mock import default_instrument
from perpbot.records import Records
from perpbot.risk import compute_size, estimate_liquidation, liquidation_ok, quantize_qty
from perpbot.strategy import DecisionContext, evaluate_entry, tier_fraction

from conftest import P, hkt, shipped_config

LIVE = dict(quantity_decimals=5, price_decimals=1, min_notional=10.0, max_leverage=50, price_bounds=0.02,
            risk_tiers=[(0.0, 50), (250_000.0, 25), (1_000_000.0, 20)])
PRICE, ATR = 86_841.0, 2_281.0                          # the 12:30 analysis
D1, D2 = date(2026, 10, 5), date(2026, 10, 6)


def _inst():
    inst = default_instrument()
    for k, v in LIVE.items():
        setattr(inst, k, v)
    return inst


def _size(cfg, equity, fraction, atr=ATR):
    r = cfg.risk
    return compute_size(equity=equity, risk_pct=float(r.risk_per_trade_pct), fraction=fraction, price=PRICE, atr=atr,
                        sl_atr_multiple=float(cfg.exits.sl_atr_multiple),
                        notional_cap_pct=float(r.notional_cap_pct_equity), leverage=int(r.leverage), inst=_inst(),
                        raise_to_min=bool(r.raise_to_min_notional), notional_multiple=r.notional_multiple_full_tier)


def test_shipped_v180_values():
    cfg = config_from_dict(shipped_config())
    s, r = cfg.strategy, cfg.risk
    assert cfg.bold.enabled is False and cfg.strategy.cadence == "rolling_4h"
    assert (s.min_entry_abs_score, s.tier_low_max, s.tier_mid_max) == (20, 40, 70)
    assert (s.tier_low_fraction, s.tier_mid_fraction, s.tier_high_fraction) == (0.15, 0.50, 1.00)
    assert (r.notional_multiple_full_tier, r.leverage, r.liq_min_sl_multiple, r.cross_margin) == (10, 12, 1.5, False)
    assert (r.kill_drawdown_pct, r.kill_losing_streak_pct) == (95, 95)
    assert (r.equity_floor_pct_of_net_funded, r.permanent_floor_pct_of_cumulative_funded) == (5, 5)
    assert r.permanent_floor_lowered_in == cfg.config_version == "1.8.1"
    assert r.live_review_expectancy_floor_r is None
    assert (cfg.exits.sl_atr_multiple, cfg.exits.tp_atr_multiple) == (1.0, 1.5)       # v1.8.1 (were 1.5 / 3.0)


@pytest.mark.parametrize("score,times", [(19.9, None), (20, 1.5), (39.9, 1.5), (40, 5), (70, 5), (70.1, 10),
                                         (73.8, 10), (100, 10)])
def test_owner_score_to_position_map(score, times):
    cfg = config_from_dict(shipped_config())
    s = cfg.strategy
    ctx = DecisionContext(direction=1, abs_score=score, tier_fraction=tier_fraction(score, s), caps=[],
                          funding_pct=None, funding_high=95, funding_low=5, event_active=False, position_dir=0,
                          opposite_streak=0, entered_today=False, paused_reason=None, flip_min_abs_score=30,
                          opposite_days_rule=18, event_allows_rule_closes=True,
                          min_entry_abs_score=float(s.min_entry_abs_score))
    d, frac, blocked = evaluate_entry(ctx)
    if times is None:
        assert d == 0 and any("below the entry threshold 20" in b for b in blocked)
    else:
        assert d == 1 and frac * cfg.risk.notional_multiple_full_tier == pytest.approx(times)
    # a gate against the trade (200-day line or 4h trend, cap 50%) limits it to x5
    capped = DecisionContext(**{**ctx.__dict__, "caps": [("h4_trend", 0.5)]})
    if times is not None:
        assert evaluate_entry(capped)[1] * 10 == pytest.approx(min(times, 5))


@pytest.mark.parametrize("fraction,qty", [(1.0, "0.01151"), (0.5, "0.00575"), (0.15, "0.00172")])
def test_positions_on_100_usdc(fraction, qty):
    s = _size(config_from_dict(shipped_config()), 100.0, fraction)
    assert s.ok and s.qty == Decimal(qty) and s.notional <= 1_000 * fraction + 1e-9
    assert s.risk_usd == pytest.approx(float(qty) * 1.0 * ATR)                 # x10: ~$26 at the 1.0 ATR stop
    assert s.risk_pct_used == pytest.approx(s.risk_usd)                        # % of 100 USDC
    assert s.effective_leverage == pytest.approx(10 * fraction, rel=0.01)


def test_leverage_still_caps_the_position():
    cfg = config_from_dict(dict(shipped_config(), risk=dict(shipped_config()["risk"], leverage=10,
                                                             notional_multiple_full_tier=10)))
    s = compute_size(equity=100.0, risk_pct=5, fraction=1.0, price=PRICE, atr=ATR, sl_atr_multiple=1.5,
                     notional_cap_pct=150, leverage=5, inst=_inst(), notional_multiple=10)
    assert s.ok and "leverage cap 5x" in s.capped_by and s.notional <= 500
    assert cfg.risk.notional_multiple_full_tier == 10
    from perpbot.config import ConfigError
    with pytest.raises(ConfigError, match="needs more than risk.leverage"):
        config_from_dict(dict(shipped_config(), risk=dict(shipped_config()["risk"], notional_multiple_full_tier=13)))


@pytest.mark.parametrize("atr,ok", [(2_281.0, True), (4_200.0, True), (4_300.0, False)])
def test_12x_liquidation_guard(atr, ok):
    """12x on BTC-USD (max 50x): liquidation ~7.33% away (1/12 - 0.5/50). It must be 1.5 x the 1.0 ATR stop (v1.8.1)
    away: entries stop when ATR is above ~4.9% of the price (~4,245 at 86,841)."""
    cfg = config_from_dict(shipped_config())
    s = _size(cfg, 100.0, 1.0, atr)
    liq = estimate_liquidation(PRICE, 1, int(cfg.risk.leverage), _inst(), s.notional,
                               float(cfg.risk.liq_estimate_mmr_divisor))
    assert (PRICE - liq) / PRICE == pytest.approx(1 / 12 - 0.01, abs=1e-12)
    assert liquidation_ok(PRICE, liq, float(cfg.exits.sl_atr_multiple) * atr, float(cfg.risk.liq_min_sl_multiple)) is ok


# ---------------------------------------------------------------- engine, live-like BTC-USD, 100 USDC
def v180_world(world, start, cash=100.0):
    w = world(start, risk__notional_multiple_full_tier=10, risk__leverage=12, risk__liq_min_sl_multiple=1.5,
              strategy__min_entry_abs_score=20, strategy__tier_low_max=40, strategy__tier_mid_max=70,
              strategy__tier_low_fraction=0.15, exits__sl_atr_multiple=1.0, exits__tp_atr_multiple=1.5)
    for k, v in LIVE.items():
        setattr(w.ex.inst, k, v)
    w.ex.cash = cash
    return w


def _plan(w):
    return w.store.latest("decisions", "score IS NOT NULL")["data"]["plan"]


def test_strong_signal_opens_the_big_position_with_the_atr_brackets(world):
    w = v180_world(world, hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.decide()
    assert w.pos() > 0
    frac = float(_plan(w)["enter_fraction"])
    ref = P + w.ex.spread / 2
    fok = w.fok_calls()[-1]
    assert frac in (0.5, 1.0)
    assert Decimal(fok["quantity"]) == quantize_qty(100.0 * 10 * frac / ref, 5)
    t = Records(w.store).open_trade()
    assert t["sl_price"] == pytest.approx(ref - 1.0 * t["atr"], abs=10)       # v1.8.1 brackets: 1.0 / 1.5 ATR
    assert t["tp_price"] == pytest.approx(ref + 1.5 * t["atr"], abs=10)
    assert ("update_leverage", {"leverage": 12, "cross": False}) in w.ex.calls
    opened = next(m for m in w.tg.sent if "] open:" in m)
    assert f"position x{float(fok['quantity']) * ref / 100:.2f} equity" in opened and "[ramp]" not in opened
    assert not w.tg.has("liquidation check")
    text = "\n".join(w.store.latest("decisions", "score IS NOT NULL")["data"]["analysis"])
    assert f"注碼：倉位 = 本金 ×{10 * frac:g}（12 倍逐倉）" in text and "反手條件" in text


def test_weak_signal_below_20_does_not_enter(world):
    w = v180_world(world, hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "weak_long")
    w.decide()
    plan = _plan(w)
    assert w.pos() == 0 and not w.fok_calls()
    assert any("below the entry threshold 20" in b for b in plan["entry_blocked"])
    text = "\n".join(w.store.latest("decisions", "score IS NOT NULL")["data"]["analysis"])
    assert "分數未到入場門檻" in text


def test_no_top_up_while_holding(world):
    """Owner 2026-10-02: a position is not added to when a later score asks for a bigger one."""
    w = v180_world(world, hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.decide()
    qty = w.pos()
    w.bn.signal(D2, "strong_long")
    w.at(hkt(2026, 10, 6, 8, 30)).decide()
    assert w.pos() == qty and len(w.fok_calls()) == 1


def test_recovered_entry_is_not_over_budget(world):
    w = v180_world(world, hkt(2026, 10, 5, 8, 30))
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
