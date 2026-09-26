"""Binance public market data (no keys): spot klines and USD-M funding history.

Endpoints (Binance public REST):
  GET {spot}/api/v3/klines?symbol&interval&limit[&startTime&endTime]
      -> [[openTime, open, high, low, close, volume, closeTime, ...], ...]
  GET {spot}/api/v3/ticker/price?symbol -> {"symbol", "price"}
  GET {futures}/fapi/v1/fundingRate?symbol&startTime&endTime&limit(<=1000)
      -> [{"symbol", "fundingTime", "fundingRate", "markPrice"}, ...]
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from perpbot.indicators import Candle
from perpbot.timeutil import DAY_MS

log = logging.getLogger("perpbot.binance")

INTERVAL_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": DAY_MS}


class DataSourceError(Exception):
    pass


class BinanceData:
    def __init__(self, cfg: Any, client: httpx.Client | None = None) -> None:
        self._b = cfg.binance
        self._client = client or httpx.Client(timeout=float(self._b.http_timeout_seconds))
        self.retries = 0

    def _get(self, bases: list[str], path: str, params: dict[str, Any]) -> Any:
        last: Exception | None = None
        for base in bases:
            for attempt in range(3):
                try:
                    r = self._client.get(base.rstrip("/") + path, params=params)
                    if r.status_code in (403, 451):
                        raise DataSourceError(f"{base} refused ({r.status_code}); trying next endpoint")
                    if r.status_code == 429 or r.status_code >= 500:
                        raise DataSourceError(f"{base} HTTP {r.status_code}")
                    r.raise_for_status()
                    return r.json()
                except (httpx.HTTPError, DataSourceError, ValueError) as e:
                    last = e
                    self.retries += 1
                    log.warning("binance %s %s attempt %d failed: %s", base, path, attempt + 1, e)
                    if isinstance(e, DataSourceError) and "refused" in str(e):
                        break
                    time.sleep(min(2 ** attempt, 4))
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
