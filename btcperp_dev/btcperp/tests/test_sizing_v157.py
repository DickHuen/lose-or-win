"""v1.5.7: the owner's more aggressive sizing (2026-10-02). Live 2026-09-30 16:30: score +37.17 (LONG, 50% tier)
was refused because 100 USDC x 1.5% x 0.5 ramp x 0.5 tier = $0.375 risk -> 0.00011 BTC = $9.1 < the $10 minimum."""

from decimal import Decimal

import pytest

from perpbot.config import config_from_dict
from perpbot.exchange.mock import default_instrument
from perpbot.risk import compute_size, min_order_qty, risk_pct_for_trade

from conftest import shipped_config

# BTC-USD as read live on 2026-09-30
LIVE = dict(quantity_decimals=5, price_decimals=1, min_notional=10.0, max_leverage=50, price_bounds=0.02,
            risk_tiers=[(0.0, 50), (250_000.0, 25), (1_000_000.0, 20)])
PRICE, ATR = 83_066.0, 2_198.0                          # the 16:30 decision


def _inst():
    inst = default_instrument()
    for k, v in LIVE.items():
        setattr(inst, k, v)
    return inst


def _size(cfg, equity, fraction, trades_before=0, raise_to_min=None):
    pct, _ramp = risk_pct_for_trade(cfg.risk, trades_before)
    return compute_size(equity=equity, risk_pct=pct, fraction=fraction, price=PRICE, atr=ATR,
                        sl_atr_multiple=float(cfg.exits.sl_atr_multiple),
                        notional_cap_pct=float(cfg.risk.notional_cap_pct_equity), leverage=int(cfg.risk.leverage),
                        inst=_inst(),
                        raise_to_min=bool(cfg.risk.raise_to_min_notional) if raise_to_min is None else raise_to_min)


def test_shipped_sizing_values():
    cfg = config_from_dict(shipped_config())
    r = cfg.risk
    assert (r.risk_per_trade_pct, r.ramp_trades, r.notional_cap_pct_equity, r.raise_to_min_notional) == (2.0, 0, 60, True)
    assert r.live_review_expectancy_floor_r == -0.196
    assert risk_pct_for_trade(r, 0) == (2.0, False)


def test_the_refused_1630_trade_would_have_been_placed():
    old = config_from_dict(dict(shipped_config(), risk=dict(shipped_config()["risk"], risk_per_trade_pct=1.5,
                                                             ramp_trades=10, notional_cap_pct_equity=30,
                                                             raise_to_min_notional=False)))
    refused = _size(old, 100.0, 0.5)
    assert not refused.ok and "below instrument min_notional" in refused.reject_reason
    new = _size(config_from_dict(shipped_config()), 100.0, 0.5)
    assert new.ok and new.qty == Decimal("0.0003") and new.risk_usd == pytest.approx(0.989, abs=0.01)


@pytest.mark.parametrize("fraction,qty,risk_max", [(0.25, "0.00015", 0.50), (0.5, "0.0003", 1.00),
                                                   (1.0, "0.0006", 2.00)])
def test_every_tier_trades_with_100_usdc(fraction, qty, risk_max):
    s = _size(config_from_dict(shipped_config()), 100.0, fraction)
    assert s.ok and s.qty == Decimal(qty) and s.notional >= 10.0 and s.risk_usd <= risk_max + 1e-9
    assert s.notional <= 60.0                                # the 60% notional cap


def test_raise_to_minimum_stays_within_the_full_tier_budget():
    cfg = config_from_dict(shipped_config())
    assert min_order_qty(_inst(), PRICE) == Decimal("0.00013")          # 0.00012 x 83,066 = 9.97 < 10
    s = _size(cfg, 40.0, 0.25)                               # $0.20 budget -> 0.00006 BTC: below the minimum
    assert s.ok and s.qty == Decimal("0.00013") and any("exchange minimum" in c for c in s.capped_by)
    assert s.risk_usd <= 40.0 * 2.0 / 100                     # within the 100%-tier risk ($0.80)
    tiny = _size(cfg, 15.0, 0.25)                            # the minimum would risk $0.43 > $0.30 full budget
    assert not tiny.ok and "below instrument min_notional" in tiny.reject_reason
    off = _size(cfg, 40.0, 0.25, raise_to_min=False)
    assert not off.ok
