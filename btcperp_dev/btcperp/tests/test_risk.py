"""Sizing, ramp, notional/leverage caps, instrument rules, liquidation check, kill-switch math."""

from decimal import Decimal

import pytest

from perpbot.exchange.mock import default_instrument
from perpbot.risk import (
    OrderRuleError,
    compute_size,
    drawdown,
    estimate_liquidation,
    liquidation_ok,
    losing_streak,
    quantize_qty,
    risk_pct_for_trade,
    size_weighted_expectancy,
    validate_order,
)

INST = default_instrument()


def size(**kw):
    base = dict(equity=10_000.0, risk_pct=1.5, fraction=1.0, price=100_000.0, atr=2_000.0, sl_atr_multiple=1.5,
                notional_cap_pct=50.0, leverage=3, inst=INST)
    base.update(kw)
    return compute_size(**base)


def test_fixed_risk_sizing():
    s = size()
    # risk 150 / SL distance 3000 = 0.05 BTC
    assert s.ok and s.qty == Decimal("0.0500")
    assert s.risk_usd == pytest.approx(150.0)
    half = size(fraction=0.5)
    assert half.qty == Decimal("0.0250")
    quarter = size(fraction=0.25)
    assert quarter.qty == Decimal("0.0125")


def test_ramp_first_ten_trades_half_risk(cfg):
    for n in range(10):
        pct, ramp = risk_pct_for_trade(cfg.risk, n)
        assert ramp and pct == pytest.approx(0.75)
    pct, ramp = risk_pct_for_trade(cfg.risk, 10)
    assert not ramp and pct == pytest.approx(1.5)


def test_notional_cap_50pct_equity():
    s = size(atr=100.0)  # tiny ATR -> huge qty before caps
    assert s.notional <= 5_000.0 + 1e-6
    assert any("notional cap" in c for c in s.capped_by)
    assert s.effective_leverage <= 0.5 + 1e-9


def test_leverage_cap():
    s = size(atr=100.0, notional_cap_pct=500.0)  # disable notional cap -> leverage cap binds at 3x
    assert s.notional <= 30_000.0 + 1e-6
    assert any("leverage cap" in c for c in s.capped_by)


def test_instrument_rules_reject():
    with pytest.raises(OrderRuleError):
        validate_order(INST, qty=Decimal("0.00001"), price=100_000, leverage=3, market=False)   # too many decimals
    with pytest.raises(OrderRuleError):
        validate_order(INST, qty=Decimal("0"), price=100_000, leverage=3, market=False)
    with pytest.raises(OrderRuleError):
        validate_order(INST, qty=Decimal("0.0001"), price=5_000, leverage=3, market=False)     # 0.5 < min notional 1
    with pytest.raises(OrderRuleError):
        validate_order(INST, qty=Decimal("0.01"), price=100_000, leverage=25, market=False)    # > max leverage
    validate_order(INST, qty=Decimal("0.01"), price=100_000, leverage=3, market=False)
    assert quantize_qty(0.123456, 4) == Decimal("0.1234")
    tiny = size(equity=10.0)
    assert not tiny.ok and tiny.reject_reason
    from dataclasses import replace

    strict = replace(INST, min_notional=100.0)
    small = size(equity=100.0, inst=strict)      # 0.0005 BTC = 50 USD notional < 100
    assert not small.ok and "min_notional" in small.reject_reason


def test_liquidation_check():
    assert liquidation_ok(100_000, 70_000, 3_000, 2.0)
    assert not liquidation_ok(100_000, 95_000, 3_000, 2.0)      # 5000 < 2 x 3000
    assert liquidation_ok(100_000, 94_000, 3_000, 2.0)          # exactly 2x is allowed
    assert not liquidation_ok(100_000, None, 3_000, 2.0)       # isolated: missing liquidation price fails (D10)
    assert not liquidation_ok(100_000, 0.0, 3_000, 2.0)
    assert not liquidation_ok(100_000, float("nan"), 3_000, 2.0)
    assert liquidation_ok(100_000, None, 3_000, 2.0, isolated=False)
    est = estimate_liquidation(100_000, 1, 3, INST, 5_000, 2)
    assert 60_000 < est < 70_000
    est_s = estimate_liquidation(100_000, -1, 3, INST, 5_000, 2)
    assert 130_000 < est_s < 140_000


def test_drawdown_kill_math():
    assert not drawdown(8_600, 10_000, 15).triggered
    st = drawdown(8_500, 10_000, 15)
    assert st.triggered and st.drawdown_pct == pytest.approx(15.0)
    assert drawdown(12_000, 10_000, 15).peak == 12_000


def test_losing_streak_math():
    trades = [{"net_pnl": -800, "equity_at_entry": 9_000}, {"net_pnl": -700, "equity_at_entry": 9_700},
              {"net_pnl": -600, "equity_at_entry": 10_000}, {"net_pnl": 300, "equity_at_entry": 9_700}]
    st = losing_streak(trades, 20)
    assert st.losing_trades == 3 and st.streak_loss == 2100
    assert st.loss_pct == pytest.approx(21.0) and st.triggered
    assert not losing_streak(trades[1:], 20).triggered
    assert not losing_streak([{"net_pnl": 5, "equity_at_entry": 1}], 20).triggered


def test_size_weighted_expectancy():
    trades = [{"net_pnl": -100, "initial_risk_usd": 100}] * 20 + [{"net_pnl": 300, "initial_risk_usd": 300}] * 5
    exp, n = size_weighted_expectancy(trades, 25)
    assert n == 25 and exp == pytest.approx((-2000 + 1500) / (2000 + 1500))
    assert size_weighted_expectancy(trades[:10], 25)[0] is None
