"""v2.0.0 trading-cost estimate and gate (owner 2026-10-06: "使用帳戶實際費率、平台 bid/ask、倉位深度、預估滑價及資金費。
價格空間不足支付成本時不入場；記錄拒絕原因。不要假設 maker 掛單一定成交").

Every entry and every exit is assumed to be a TAKER order (FOK entry, triggered stop / target, reduce-only IOC close):
no maker rebate or resting fill is ever assumed. Round-trip cost per BTC, in price units:
  fees      2 x taker fee rate x price (the account's rate: the higher of the exchange schedule for the instrument's
            category and the rate actually charged on the account's recent taker fills; the config estimate when
            neither is known)
  entry     walking the Polymarket book for the order quantity: average fill vs the mid (half spread + depth)
  exit      the same walk on the other side of the book (an exit trades against it)
  stop      extra slippage of a triggered stop in a fast market (stop_slippage_bps)
  funding   the Polymarket hourly funding rate x funding_hold_hours when the position pays it (0 when it receives)
The backtest uses the same structure with fixed per-side fee and slippage scenarios (from_scenario).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any, Sequence


@dataclass
class CostEstimate:
    fee_rate: float                 # per side, fraction of notional
    fee_source: str
    entry_bps: float                # vs mid (half spread + depth), basis points of the price
    exit_bps: float
    stop_bps: float
    funding_bps: float
    depth_ok: bool = True
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def total_bps(self) -> float:
        return 2.0 * self.fee_rate * 1e4 + self.entry_bps + self.exit_bps + self.stop_bps + self.funding_bps

    def per_unit(self, price: float) -> float:
        return price * self.total_bps / 1e4

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total_bps"] = self.total_bps
        return d


def walk(levels: Sequence[tuple[float, float]], qty: float) -> tuple[float | None, float]:
    """(average fill price, quantity available) taking `qty` from the book side `levels` (best first)."""
    left, cost, took = float(qty), 0.0, 0.0
    for px, q in levels:
        if left <= 0:
            break
        t = min(left, float(q))
        cost += t * float(px)
        took += t
        left -= t
    if took <= 0:
        return None, 0.0
    return cost / took, took


def account_fee_rate(schedule: list[dict[str, Any]] | None, category: str, taker_fills: Sequence[Any],
                     estimate: float, min_fills: int) -> tuple[float, str]:
    """The higher of the listed taker rate and the median rate charged on recent taker fills (>= min_fills)."""
    rates: list[tuple[float, str]] = []
    if schedule:
        row = next((e for e in schedule if e.get("category") == category and e.get("taker_fee_rate") is not None), None)
        if row is not None:
            rates.append((float(row["taker_fee_rate"]), f"exchange schedule ({category})"))
        else:
            listed = [float(e["taker_fee_rate"]) for e in schedule if e.get("taker_fee_rate") is not None]
            if listed:
                rates.append((max(listed), "exchange schedule (highest listed; category not listed)"))
    measured = [f.fee / (f.price * f.quantity) for f in taker_fills
                if getattr(f, "taker", False) and f.price > 0 and f.quantity > 0 and f.fee >= 0]
    if len(measured) >= min_fills:
        rates.append((median(measured), f"charged on {len(measured)} recent taker fills"))
    if not rates:
        return float(estimate), "config estimate (no schedule, no fills)"
    return max(rates, key=lambda x: x[0])


def from_book(*, book: Any, qty: float, direction: int, fee_rate: float, fee_source: str, stop_slippage_bps: float,
              funding_rate_hourly: float | None, funding_hold_hours: float) -> CostEstimate:
    bids, asks = list(book.bids), list(book.asks)
    if not bids or not asks:
        return CostEstimate(fee_rate, fee_source, 0.0, 0.0, stop_slippage_bps, 0.0, False, {"error": "empty book"})
    mid = (bids[0][0] + asks[0][0]) / 2.0
    entry_side, exit_side = (asks, bids) if direction > 0 else (bids, asks)
    e_px, e_q = walk(entry_side, qty)
    x_px, x_q = walk(exit_side, qty)
    depth_ok = e_q >= qty - 1e-12 and x_q >= qty - 1e-12
    entry_bps = abs(e_px - mid) / mid * 1e4 if e_px else 0.0
    exit_bps = abs(mid - x_px) / mid * 1e4 if x_px else 0.0
    pays = funding_rate_hourly is not None and funding_rate_hourly * direction > 0
    funding_bps = abs(float(funding_rate_hourly)) * float(funding_hold_hours) * 1e4 if pays else 0.0
    detail = {"mid": mid, "best_bid": bids[0][0], "best_ask": asks[0][0], "spread": asks[0][0] - bids[0][0],
              "entry_avg": e_px, "exit_avg": x_px, "qty": qty, "entry_depth": e_q, "exit_depth": x_q,
              "funding_rate_hourly": funding_rate_hourly, "funding_paid": pays}
    return CostEstimate(fee_rate, fee_source, entry_bps, exit_bps, float(stop_slippage_bps), funding_bps, depth_ok, detail)


def from_scenario(*, fee_bps_side: float, slip_bps_side: float, funding_rate_hourly: float | None, direction: int,
                  funding_hold_hours: float, name: str) -> CostEstimate:
    """Backtest: fixed per-side fee and slippage (Codex scenarios), funding proxy when paid."""
    pays = funding_rate_hourly is not None and funding_rate_hourly * direction > 0
    funding_bps = abs(float(funding_rate_hourly)) * float(funding_hold_hours) * 1e4 if pays else 0.0
    return CostEstimate(fee_bps_side / 1e4, f"scenario {name}", slip_bps_side, slip_bps_side, 0.0, funding_bps, True,
                        {"scenario": name})
