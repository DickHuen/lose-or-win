"""Technical indicators on closed candles (pure functions)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Candle:
    open_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    close_ms: int = 0  # exclusive end (open_ms + interval)


def clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """EMA seeded with the SMA of the first `period` values (None before the seed)."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    tr: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            tr.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            tr.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return tr


def atr_wilder(candles: Sequence[Candle], period: int) -> list[float | None]:
    """Wilder ATR. Seed = mean of TR[1..period] (TRs that have a previous close)."""
    out: list[float | None] = [None] * len(candles)
    if len(candles) < period + 1:
        return out
    tr = true_ranges(candles)
    seed = sum(tr[1 : period + 1]) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, len(candles)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def clv(high: float, low: float, close: float) -> float:
    """Close location value in [-1, 1]; 0 when high == low."""
    rng = high - low
    if rng == 0:
        return 0.0
    return ((close - low) - (high - close)) / rng


def percentile_rank(values: Sequence[float], x: float) -> float:
    """Mid-rank percentile of x within values, 0..100."""
    if not values:
        raise ValueError("percentile_rank of empty sequence")
    below = sum(1 for v in values if v < x)
    equal = sum(1 for v in values if v == x)
    return 100.0 * (below + 0.5 * equal) / len(values)


def last_value(series: Sequence[float | None]) -> float:
    v = series[-1] if series else None
    if v is None:
        raise ValueError("indicator has no value yet (not enough candles)")
    return v
