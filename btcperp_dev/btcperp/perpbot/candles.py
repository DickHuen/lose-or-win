"""v2.0.0 incremental Binance candle cache with integrity checks (owner 2026-10-06, Codex report: "Binance 改用增量快取，
尊重 429／418 的 Retry-After；K 線檢查連續性、完整根數及收線時間").

The 15-minute decisions read 15m / 1h / 4h Binance BTCUSDT spot candles from the bot's own database
(bn_klines_15m / bn_klines_1h / bn_klines_4h). Each run asks Binance only for the candles after the last stored one
(one request per interval; the first run back-fills `intraday.cache_backfill_days`). A Retry-After pause from a 429 /
418 is stored in `data_health` and no request is sent before it ends, also by the next scheduled runs.

`check()` verifies, for the window a decision needs: candles aligned to their interval, no duplicates, no gaps, sane
OHLC, and the last candle closing exactly at the latest boundary at or before the decision time. Anything else marks
the data stale or incomplete: new entries are refused; protection and exits of an open position keep running.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from perpbot.datasources.binance import INTERVAL_MS, DataSourceError, RateLimited
from perpbot.indicators import Candle
from perpbot.storage import Store
from perpbot.timeutil import DAY_MS

log = logging.getLogger("perpbot.candles")

TABLES = {"15m": "bn_klines_15m", "1h": "bn_klines_1h", "4h": "bn_klines_4h", "5m": "bn_klines_5m"}


@dataclass
class SeriesCheck:
    interval: str
    ok: bool
    count: int
    needed: int
    last_close_ms: int | None
    expected_close_ms: int
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"interval": self.interval, "ok": self.ok, "count": self.count, "needed": self.needed,
                "last_close_ms": self.last_close_ms, "expected_close_ms": self.expected_close_ms,
                "problems": list(self.problems)}


def boundary(t_ms: int, step_ms: int) -> int:
    """Latest candle boundary at or before t_ms."""
    return t_ms // step_ms * step_ms


def check(bars: Sequence[Candle], interval: str, needed: int, at_ms: int) -> SeriesCheck:
    """The last `needed` candles closed at or before at_ms: complete, contiguous, aligned, sane, and the newest one
    closing exactly at the latest boundary (no missing / late candle)."""
    step = INTERVAL_MS[interval]
    expected = boundary(at_ms, step)
    closed = [c for c in bars if c.open_ms + step <= at_ms]
    win = closed[-needed:] if needed > 0 else []
    problems: list[str] = []
    last_close = (win[-1].open_ms + step) if win else None
    if len(win) < needed:
        problems.append(f"{interval}: only {len(win)} of {needed} closed candles")
    if last_close != expected:
        problems.append(f"{interval}: newest closed candle ends {last_close}, expected {expected} (stale or missing)")
    seen: set[int] = set()
    for i, c in enumerate(win):
        if c.open_ms % step:
            problems.append(f"{interval}: candle {c.open_ms} not aligned to {interval}")
            break
        if c.open_ms in seen:
            problems.append(f"{interval}: duplicate candle {c.open_ms}")
            break
        seen.add(c.open_ms)
        if i and c.open_ms - win[i - 1].open_ms != step:
            problems.append(f"{interval}: gap between {win[i - 1].open_ms} and {c.open_ms}")
            break
        if not (0 < c.low <= min(c.open, c.close) and max(c.open, c.close) <= c.high):
            problems.append(f"{interval}: candle {c.open_ms} has inconsistent OHLC")
            break
        if c.close_ms and c.close_ms != c.open_ms + step:
            problems.append(f"{interval}: candle {c.open_ms} close time {c.close_ms} != open + {interval}")
            break
    return SeriesCheck(interval, not problems, len(win), needed, last_close, expected, problems)


def _row(c: Candle) -> dict[str, Any]:
    return {"open_ms": c.open_ms, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}


class CandleCache:
    """Binance candles kept in the append-only store; only new closed candles are fetched."""

    def __init__(self, store: Store, binance: Any, *, backfill_days: float, max_requests_per_interval: int = 4) -> None:
        self.store = store
        self.bn = binance
        self.backfill_ms = int(float(backfill_days) * DAY_MS)
        self.max_requests = int(max_requests_per_interval)
        self.requests = 0
        self.errors: list[str] = []

    # ------------------------------------------------------------ backoff persisted across runs
    def blocked_until(self) -> tuple[int, int]:
        row = self.store.latest("data_health", "kind='binance_backoff'")
        if not row or not isinstance(row["data"], dict):
            return 0, 0
        return int(row["data"].get("until_ms") or 0), int(row["data"].get("status") or 0)

    def _record_backoff(self, e: RateLimited) -> None:
        self.store.insert("data_health", kind="binance_backoff", data={"until_ms": e.until_ms, "status": e.status})

    # ------------------------------------------------------------ reads
    def load(self, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        """Closed candles with start_ms <= open < end_ms and close <= end_ms, from the store."""
        step = INTERVAL_MS[interval]
        rows = self.store.query(f"SELECT open_ms, open, high, low, close, volume FROM {TABLES[interval]} "
                                f"WHERE open_ms >= ? AND open_ms <= ? ORDER BY open_ms", [start_ms, end_ms - step])
        return [Candle(int(r["open_ms"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                       float(r["volume"] or 0.0), int(r["open_ms"]) + step) for r in rows]

    def last_open(self, interval: str) -> int | None:
        row = self.store.query(f"SELECT MAX(open_ms) AS m FROM {TABLES[interval]}")
        return int(row[0]["m"]) if row and row[0]["m"] is not None else None

    def first_open(self, interval: str) -> int | None:
        row = self.store.query(f"SELECT MIN(open_ms) AS m FROM {TABLES[interval]}")
        return int(row[0]["m"]) if row and row[0]["m"] is not None else None

    def backfill_older(self, interval: str, want_start_ms: int, now_ms: int, max_requests: int = 2) -> dict[str, Any]:
        """v2.3.0: history for the forecast frequencies (forecast.calib_days): candles BEFORE the oldest stored one,
        back to want_start_ms, at most max_requests x 1000 a run (a fresh install fills a year of 1h in a few runs).
        Respects a stored Binance pause; never raises."""
        out: dict[str, Any] = {"fetched": 0, "error": None}
        until, _ = self.blocked_until()
        if until > now_ms:
            out["error"] = "Binance pause in force"
            return out
        step = INTERVAL_MS[interval]
        first = self.first_open(interval)
        if first is None or first <= want_start_ms + step:
            return out
        for _ in range(max_requests):
            start = boundary(max(want_start_ms, first - 1000 * step), step)
            try:
                self.requests += 1
                bars = [c for c in self.bn.klines_since(interval, start, now_ms, 1000) if c.open_ms < first]
            except RateLimited as e:
                self._record_backoff(e)
                out["error"] = str(e)
                break
            except Exception as e:  # noqa: BLE001 - history for the forecast only
                out["error"] = f"{interval}: {e}"
                break
            if not bars:
                break
            self.store.insert_many_ignore(TABLES[interval], (_row(c) for c in bars))
            out["fetched"] += len(bars)
            first = bars[0].open_ms
            if first <= want_start_ms + step:
                break
        return out

    # ------------------------------------------------------------ incremental update
    def update(self, intervals: Sequence[str], now_ms: int) -> dict[str, Any]:
        """Fetch only candles after the newest stored one (or back-fill when empty / too old). Never raises:
        failures are returned (and the integrity check then refuses new entries)."""
        out: dict[str, Any] = {"fetched": {}, "errors": [], "blocked_until_ms": None}
        until, status = self.blocked_until()
        if until > now_ms:
            out["blocked_until_ms"] = until
            out["errors"].append(f"Binance paused us until {until} (HTTP {status} Retry-After): no request sent")
            if hasattr(self.bn, "block_until"):
                self.bn.block_until(until, status)
            return out
        for iv in intervals:
            step = INTERVAL_MS[iv]
            last = self.last_open(iv)
            start = (last + step) if last is not None else now_ms - self.backfill_ms
            start = max(start, now_ms - self.backfill_ms)
            start = boundary(start, step)
            got = 0
            try:
                for _ in range(self.max_requests):
                    if start + step > now_ms:
                        break
                    self.requests += 1
                    bars = self.bn.klines_since(iv, start, now_ms, 1000)
                    if bars:
                        self.store.insert_many_ignore(TABLES[iv], (_row(c) for c in bars))
                        got += len(bars)
                        start = bars[-1].open_ms + step
                    if len(bars) < 1000:
                        break
            except RateLimited as e:
                self._record_backoff(e)
                out["errors"].append(str(e))
                out["blocked_until_ms"] = e.until_ms
                break
            except (DataSourceError, Exception) as e:  # noqa: BLE001 - data problems must not stop protection
                out["errors"].append(f"{iv}: {e}")
                log.warning("candle update %s failed: %s", iv, e)
            out["fetched"][iv] = got
        self.errors = list(out["errors"])
        return out
