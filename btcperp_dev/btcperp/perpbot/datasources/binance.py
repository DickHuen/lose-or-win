"""Binance public market data (no keys): spot klines and USD-M funding history.

Endpoints (Binance public REST):
  GET {spot}/api/v3/klines?symbol&interval&limit[&startTime&endTime]
      -> [[openTime, open, high, low, close, volume, closeTime, ...], ...]
  GET {spot}/api/v3/ticker/price?symbol -> {"symbol", "price"}
  GET {futures}/fapi/v1/fundingRate?symbol&startTime&endTime&limit(<=1000)
      -> [{"symbol", "fundingTime", "fundingRate", "markPrice"}, ...]

v2.0.0 rate limits (Binance REST docs): HTTP 429 = request limit hit, 418 = IP auto-banned after ignoring 429s. Both
carry Retry-After (seconds). A 429 with a short Retry-After waits once and retries; anything longer, and every 418,
stops ALL requests of this client until then (no other endpoint is tried to get around a limit) and raises
RateLimited with the time, which the candle cache stores so the next scheduled runs do not ask again before it.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from perpbot.indicators import Candle
from perpbot.timeutil import DAY_MS

log = logging.getLogger("perpbot.binance")

INTERVAL_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": DAY_MS}
RETRY_AFTER_WAIT_MAX_S = 10.0          # a 429 asking for at most this long is waited out once inside the run
RETRY_AFTER_DEFAULT_S = {429: 60.0, 418: 300.0}     # when the header is missing or unreadable


class DataSourceError(Exception):
    pass


class RateLimited(DataSourceError):
    """Binance asked us to stop until `until_ms` (429 / 418 with Retry-After)."""

    def __init__(self, status: int, until_ms: int, detail: str = "") -> None:
        super().__init__(f"Binance HTTP {status}: requests paused until {until_ms} (Retry-After){detail}")
        self.status = status
        self.until_ms = until_ms


def retry_after_seconds(headers: Any, status: int) -> float:
    try:
        v = float(headers.get("Retry-After"))
        if v >= 0:
            return v
    except (TypeError, ValueError):
        pass
    return RETRY_AFTER_DEFAULT_S.get(status, 60.0)


class BinanceData:
    def __init__(self, cfg: Any, client: httpx.Client | None = None, *, now_ms: Any = None,
                 sleep: Any = None) -> None:
        self._b = cfg.binance
        self._client = client or httpx.Client(timeout=float(self._b.http_timeout_seconds))
        self.retries = 0
        self.requests = 0
        self.blocked_until_ms = 0                 # v2.0.0: set by 429 / 418; no request before it
        self.blocked_status = 0
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._sleep = sleep or time.sleep

    def block_until(self, until_ms: int, status: int) -> None:
        """Honour a pause recorded by an earlier run (the candle cache stores it)."""
        if until_ms > self.blocked_until_ms:
            self.blocked_until_ms, self.blocked_status = int(until_ms), int(status)

    def _get(self, bases: list[str], path: str, params: dict[str, Any]) -> Any:
        last: Exception | None = None
        waited = False
        for base in bases:
            for attempt in range(3):
                now = self._now_ms()
                if now < self.blocked_until_ms:
                    raise RateLimited(self.blocked_status or 429, self.blocked_until_ms, " (not sent)")
                try:
                    self.requests += 1
                    r = self._client.get(base.rstrip("/") + path, params=params)
                    if r.status_code in (429, 418):
                        wait = retry_after_seconds(r.headers, r.status_code)
                        self.retries += 1
                        if r.status_code == 429 and wait <= RETRY_AFTER_WAIT_MAX_S and not waited:
                            log.warning("binance %s %s: 429, waiting Retry-After %.0fs once", base, path, wait)
                            waited = True
                            self._sleep(wait)
                            continue
                        self.block_until(now + int(wait * 1000), r.status_code)
                        log.error("binance %s %s: HTTP %d, no requests for %.0fs (Retry-After)", base, path,
                                  r.status_code, wait)
                        raise RateLimited(r.status_code, self.blocked_until_ms)
                    if r.status_code in (403, 451):
                        raise DataSourceError(f"{base} refused ({r.status_code}); trying next endpoint")
                    if r.status_code >= 500:
                        raise DataSourceError(f"{base} HTTP {r.status_code}")
                    r.raise_for_status()
                    return r.json()
                except RateLimited:
                    raise
                except (httpx.HTTPError, DataSourceError, ValueError) as e:
                    last = e
                    self.retries += 1
                    log.warning("binance %s %s attempt %d failed: %s", base, path, attempt + 1, e)
                    if isinstance(e, DataSourceError) and "refused" in str(e):
                        break
                    self._sleep(min(2 ** attempt, 4))
        raise DataSourceError(f"Binance request {path} failed on all endpoints: {last}")

    def klines(self, interval: str, limit: int, now_ms: int) -> list[Candle]:
        """Closed candles only (closeTime < now)."""
        data = self._get(list(self._b.spot_base_urls), "/api/v3/klines",
                         {"symbol": self._b.symbol, "interval": interval, "limit": min(int(limit) + 1, 1000)})
        step = INTERVAL_MS[interval]
        out = []
        for row in data:
            open_ms = int(row[0])
            if open_ms + step > now_ms:
                continue
            out.append(Candle(open_ms, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]),
                              open_ms + step))
        return out

    def klines_since(self, interval: str, start_ms: int, now_ms: int, limit: int = 1000) -> list[Candle]:
        """v2.0.0 incremental cache: closed candles with open time >= start_ms, one request (at most `limit`)."""
        step = INTERVAL_MS[interval]
        data = self._get(list(self._b.spot_base_urls), "/api/v3/klines",
                         {"symbol": self._b.symbol, "interval": interval, "startTime": int(start_ms),
                          "limit": max(1, min(int(limit), 1000))})
        out = []
        for row in data:
            open_ms = int(row[0])
            if open_ms < start_ms or open_ms + step > now_ms:
                continue
            out.append(Candle(open_ms, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]),
                              open_ms + step))
        return out

    def klines_range(self, interval: str, start_ms: int, end_ms: int, max_pages: int = 200) -> list[Candle]:
        """Closed candles with open time in [start_ms, end_ms), paged 1000 at a time (backtest download)."""
        step = INTERVAL_MS[interval]
        out: dict[int, Candle] = {}
        cur = start_ms
        for _ in range(max_pages):
            data = self._get(list(self._b.spot_base_urls), "/api/v3/klines",
                             {"symbol": self._b.symbol, "interval": interval, "startTime": cur, "endTime": end_ms - 1,
                              "limit": 1000})
            if not data:
                break
            for row in data:
                open_ms = int(row[0])
                if start_ms <= open_ms and open_ms + step <= end_ms:
                    out[open_ms] = Candle(open_ms, float(row[1]), float(row[2]), float(row[3]), float(row[4]),
                                          float(row[5]), open_ms + step)
            last = int(data[-1][0])
            if len(data) < 1000 or last + step >= end_ms:
                break
            cur = last + step
        return [out[k] for k in sorted(out)]

    def funding(self, start_ms: int, end_ms: int) -> list[tuple[int, float, float]]:
        """(fundingTime ms, fundingRate, markPrice) ascending."""
        out: list[tuple[int, float, float]] = []
        cur = start_ms
        for _ in range(200):
            data = self._get(list(self._b.futures_base_urls), "/fapi/v1/fundingRate",
                             {"symbol": self._b.symbol, "startTime": cur, "endTime": end_ms, "limit": 1000})
            if not data:
                break
            for row in data:
                mp = row.get("markPrice")
                out.append((int(row["fundingTime"]), float(row["fundingRate"]), float(mp) if mp not in (None, "") else 0.0))
            last = int(data[-1]["fundingTime"])
            if len(data) < 1000 or last >= end_ms:
                break
            cur = last + 1
        dedup = {ts: (ts, r, m) for ts, r, m in out}
        return [dedup[k] for k in sorted(dedup)]

    def price(self) -> float:
        data = self._get(list(self._b.spot_base_urls), "/api/v3/ticker/price", {"symbol": self._b.symbol})
        return float(data["price"])

    def close(self) -> None:
        self._client.close()
