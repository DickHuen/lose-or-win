"""Position sizing, caps, instrument rules, liquidation sanity and kill-switch math."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal
from typing import Any, Sequence

from perpbot.exchange.base import PRICE_SIG_FIGS, Instrument


class OrderRuleError(Exception):
    """Order would violate /v1/info/instruments constraints or bot caps."""


def quantize_qty(qty: float, decimals: int) -> Decimal:
    q = Decimal(1).scaleb(-decimals)
    return Decimal(str(qty)).quantize(q, rounding=ROUND_DOWN)


def quantize_price(price: float, decimals: int, mode: str, sig_figs: int = PRICE_SIG_FIGS) -> Decimal:
    """mode: 'down' | 'up' | 'nearest'. At most `decimals` decimals AND at most `sig_figs` significant figures:
    BTC at 83,264 -> whole dollars; at 100,000 or more -> steps of 10. Never an exponent in the result."""
    d = Decimal(str(price))
    q = Decimal(1).scaleb(-decimals)
    if sig_figs and d != 0:
        q = max(q, Decimal(1).scaleb(d.adjusted() - (sig_figs - 1)))
    rounding = {"down": ROUND_FLOOR, "up": ROUND_CEILING}.get(mode)
    out = d.quantize(q) if rounding is None else d.quantize(q, rounding=rounding)
    if out.as_tuple().exponent > 0:                       # 8.327E+4 -> 83270
        out = out.quantize(Decimal(1))
    return out


def risk_pct_for_trade(cfg_risk: Any, live_trades_opened_before: int) -> tuple[float, bool]:
    """(risk % at the 100% tier, ramp_active) for the next trade."""
    ramp = live_trades_opened_before < cfg_risk.ramp_trades
    pct = cfg_risk.risk_per_trade_pct * (cfg_risk.ramp_factor if ramp else 1.0)
    return pct, ramp


@dataclass
class SizeResult:
    ok: bool
    qty: Decimal
    notional: float
    risk_usd: float
    risk_pct_used: float
    sl_distance: float
    effective_leverage: float
    capped_by: list[str] = field(default_factory=list)
    reject_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["qty"] = str(self.qty)
        return d


def max_leverage_for_notional(inst: Instrument, notional: float) -> int:
    lev = inst.max_leverage
    tiers = sorted(inst.risk_tiers, key=lambda t: t[0])
    for lower, max_lev in tiers:
        if notional >= lower:
            lev = max_lev
    return lev


def compute_size(*, equity: float, risk_pct: float, fraction: float, price: float, atr: float,
                 sl_atr_multiple: float, notional_cap_pct: float, leverage: int,
                 inst: Instrument) -> SizeResult:
    sl_distance = sl_atr_multiple * atr
    if equity <= 0 or price <= 0 or sl_distance <= 0 or fraction <= 0:
        return SizeResult(False, Decimal(0), 0, 0, 0, sl_distance, 0, reject_reason="non-positive equity/price/ATR/fraction")
    risk_usd = equity * risk_pct / 100.0 * fraction
    qty = risk_usd / sl_distance
    capped: list[str] = []
    notional_cap = equity * notional_cap_pct / 100.0
    if qty * price > notional_cap:
        qty = notional_cap / price
        capped.append(f"notional cap {notional_cap_pct}% of equity")
    lev_cap_notional = equity * leverage
    if qty * price > lev_cap_notional:
        qty = lev_cap_notional / price
        capped.append(f"leverage cap {leverage}x")
    q = quantize_qty(qty, inst.quantity_decimals)
    notional = float(q) * price
    res = SizeResult(True, q, notional, float(q) * sl_distance, risk_pct * fraction, sl_distance,
                     notional / equity if equity else 0.0, capped)
    try:
        validate_order(inst, qty=q, price=price, leverage=leverage, market=False)
    except OrderRuleError as e:
        res.ok = False
        res.reject_reason = str(e)
    return res


def validate_order(inst: Instrument, *, qty: Decimal, price: float, leverage: int, market: bool) -> None:
    if qty <= 0:
        raise OrderRuleError("quantity rounds to zero at instrument quantity_decimals")
    if qty.as_tuple().exponent < -inst.quantity_decimals:
        raise OrderRuleError("quantity has more decimals than instrument allows")
    notional = float(qty) * price
    if notional < inst.min_notional:
        raise OrderRuleError(f"notional {notional:.2f} below instrument min_notional {inst.min_notional}")
    max_notional = inst.max_market_notional if market else inst.max_limit_notional
    if max_notional > 0 and notional > max_notional:
        raise OrderRuleError(f"notional {notional:.2f} above instrument max {'market' if market else 'limit'} notional {max_notional}")
    if leverage > inst.max_leverage:
        raise OrderRuleError(f"leverage {leverage} above instrument max_leverage {inst.max_leverage}")
    tier_lev = max_leverage_for_notional(inst, notional)
    if leverage > tier_lev:
        raise OrderRuleError(f"leverage {leverage} above risk-tier max {tier_lev} for notional {notional:.2f}")


def validate_price(inst: Instrument, price: Decimal) -> None:
    if price <= 0:
        raise OrderRuleError("price must be positive")
    if price.as_tuple().exponent < -inst.price_decimals:
        raise OrderRuleError("price has more decimals than instrument allows")


def estimate_liquidation(entry: float, direction: int, leverage: int, inst: Instrument, notional: float,
                         mmr_divisor: float) -> float:
    """Isolated-margin liquidation ESTIMATE: distance = 1/leverage - mmr (fees ignored), with
    mmr = 1 / (mmr_divisor * max_leverage); divisor 2 gives 0.5 / max_leverage."""
    mmr = 1.0 / (mmr_divisor * max_leverage_for_notional(inst, notional))
    frac = max(1.0 / leverage - mmr, 0.0)
    return entry * (1 - frac) if direction > 0 else entry * (1 + frac)


def liquidation_ok(entry: float, liq_price: float | None, sl_distance: float, min_multiple: float,
                   isolated: bool = True) -> bool:
    """Reject if the liquidation price is closer than min_multiple x SL distance.
    On an isolated position a missing, zero or NaN liquidation price also fails."""
    if liq_price is None or math.isnan(liq_price) or liq_price <= 0:
        return not isolated
    return abs(entry - liq_price) >= min_multiple * sl_distance


# ---------------------------------------------------------------- kill switches

@dataclass
class DrawdownState:
    equity: float
    peak: float
    drawdown_pct: float
    triggered: bool


def drawdown(equity: float, prev_peak: float | None, limit_pct: float) -> DrawdownState:
    peak = max(equity, prev_peak or 0.0)
    dd = 0.0 if peak <= 0 else (peak - equity) / peak * 100.0
    return DrawdownState(equity, peak, dd, dd >= limit_pct)


@dataclass
class StreakState:
    losing_trades: int
    streak_loss: float
    equity_at_streak_start: float
    loss_pct: float
    triggered: bool


def is_tie(t: dict[str, Any], tie_pct: float) -> bool:
    """Review D11: |net PnL| within tie_pct % of equity at entry is a tie."""
    eq = float(t.get("equity_at_entry") or 0.0)
    return eq > 0 and abs(float(t["net_pnl"])) <= eq * tie_pct / 100.0


def losing_streak(trades_newest_first: Sequence[dict[str, Any]], limit_pct: float, tie_pct: float = 0.0) -> StreakState:
    """trades: dicts with net_pnl and equity_at_entry, newest first (already filtered since last resume).
    A tie (review D11) neither ends nor extends the streak and is not counted in its loss."""
    loss = 0.0
    n = 0
    start_equity = 0.0
    for t in trades_newest_first:
        if tie_pct > 0 and is_tie(t, tie_pct):
            continue
        pnl = float(t["net_pnl"])
        if pnl >= 0:
            break
        loss += -pnl
        n += 1
        start_equity = float(t.get("equity_at_entry") or 0.0)
    pct = (loss / start_equity * 100.0) if start_equity > 0 else 0.0
    return StreakState(n, loss, start_equity, pct, n > 0 and pct >= limit_pct)


def size_weighted_expectancy(trades_newest_first: Sequence[dict[str, Any]], window: int) -> tuple[float | None, int]:
    """Sum(net_pnl) / Sum(initial_risk) over the last `window` trades (= risk-weighted mean R)."""
    sel = list(trades_newest_first[:window])
    if len(sel) < window:
        return None, len(sel)
    risk = sum(float(t.get("initial_risk_usd") or 0) for t in sel)
    if risk <= 0:
        return None, len(sel)
    return sum(float(t["net_pnl"]) for t in sel) / risk, len(sel)
