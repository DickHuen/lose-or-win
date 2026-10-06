"""v2.0.0 test helpers: scripted Binance candles (15m -> 1h / 4h / 5m), a Binance fake with the incremental API and a
world that runs the 15-minute bot along the price path (the mock exchange's mark follows the path)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from perpbot.calendar_events import parse_calendar
from perpbot.config import config_from_dict
from perpbot.datasources.binance import RateLimited
from perpbot.engine import Engine
from perpbot.exchange.base import AccountConfig
from perpbot.exchange.mock import MockExchange, default_instrument
from perpbot.indicators import Candle
from perpbot.storage import Store
from perpbot.timeutil import UTC, FixedClock, from_ms, to_ms

M5, M15, H1, H4 = 300_000, 900_000, 3_600_000, 14_400_000
T0 = to_ms(datetime(2026, 9, 1, tzinfo=UTC))          # first scripted candle
LIVE_INST = dict(quantity_decimals=5, price_decimals=1, min_notional=10.0, max_leverage=50, price_bounds=0.02,
                 risk_tiers=[(0.0, 50), (250_000.0, 25), (1_000_000.0, 20)])


def bars_from_points(points: Sequence[tuple[int, float]], wick: float = 20.0, start_ms: int = T0) -> list[Candle]:
    """15m candles following straight lines between (bar index, price) points; every candle's open is the previous
    close, its high / low the body +/- wick."""
    out: list[Candle] = []
    prev = points[0][1]
    for (i0, p0), (i1, p1) in zip(points, points[1:]):
        for i in range(i0, i1):
            c = p0 + (p1 - p0) * (i + 1 - i0) / (i1 - i0)
            o = prev
            t = start_ms + i * M15
            out.append(Candle(t, o, max(o, c) + wick, min(o, c) - wick, c, 1.0, t + M15))
            prev = c
    return out


def background(n_bars: int, base: float = 84_000.0, amp: float = 300.0, period_bars: int = 24) -> list[tuple[int, float]]:
    """A sideways market (swings both ways, no trend) to warm up the indicators."""
    pts = [(0, base)]
    i, k = 0, 0
    while i + period_bars // 2 <= n_bars:
        i += period_bars // 2
        k += 1
        pts.append((i, base + (amp if k % 2 else -amp) * (1 + 0.1 * math.sin(k))))
    return pts


def agg(bars: Sequence[Candle], step: int) -> list[Candle]:
    out: dict[int, list[float]] = {}
    for c in bars:
        k = c.open_ms // step * step
        v = out.get(k)
        if v is None:
            out[k] = [c.open, c.high, c.low, c.close, c.volume, 1]
        else:
            v[1], v[2], v[3], v[4], v[5] = max(v[1], c.high), min(v[2], c.low), c.close, v[4] + c.volume, v[5] + 1
    src = bars[1].open_ms - bars[0].open_ms
    need = step // src
    return [Candle(k, v[0], v[1], v[2], v[3], v[4], k + step) for k, v in sorted(out.items()) if v[5] == need]


def split5(bars: Sequence[Candle]) -> list[Candle]:
    """Three 5m candles per 15m candle: open -> low -> high -> close for a rising candle, mirrored for a falling one."""
    out = []
    for c in bars:
        up = c.close >= c.open
        mids = (c.low, c.high) if up else (c.high, c.low)
        pts = [c.open, mids[0], mids[1], c.close]
        for j in range(3):
            o, e = pts[j], pts[j + 1]
            t = c.open_ms + j * M5
            out.append(Candle(t, o, max(o, e), min(o, e), e, c.volume / 3, t + M5))
    return out


def mirror(bars: Sequence[Candle], center: float) -> list[Candle]:
    return [Candle(c.open_ms, 2 * center - c.open, 2 * center - c.low, 2 * center - c.high, 2 * center - c.close,
                   c.volume, c.close_ms) for c in bars]


class IntradayBinance:
    """Binance fake serving only candles closed at `now` (incremental API), counting requests."""

    def __init__(self, m15: Sequence[Candle]) -> None:
        self.series = {"15m": list(m15), "1h": agg(m15, H1), "4h": agg(m15, H4), "5m": split5(m15)}
        self.requests: list[tuple[str, int]] = []
        self.fail = False
        self.limit_status: int | None = None       # 429 / 418 on the next request
        self.blocked_until_ms = 0

    def block_until(self, until_ms: int, status: int) -> None:
        self.blocked_until_ms = max(self.blocked_until_ms, until_ms)

    def _closed(self, interval: str, now_ms: int) -> list[Candle]:
        return [c for c in self.series[interval] if c.close_ms <= now_ms]

    def klines_since(self, interval: str, start_ms: int, now_ms: int, limit: int = 1000) -> list[Candle]:
        self._now_hint = now_ms
        if self.blocked_until_ms > now_ms:
            raise RateLimited(418, self.blocked_until_ms, " (not sent)")
        self.requests.append((interval, start_ms))
        if self.limit_status:
            st, self.limit_status = self.limit_status, None
            raise RateLimited(st, now_ms + 1_800_000)
        if self.fail:
            from perpbot.datasources.binance import DataSourceError

            raise DataSourceError("binance down (test)")
        return [c for c in self._closed(interval, now_ms) if c.open_ms >= start_ms][:limit]

    def klines(self, interval: str, limit: int, now_ms: int) -> list[Candle]:
        return self._closed(interval, now_ms)[-limit:]

    def klines_range(self, interval: str, start_ms: int, end_ms: int, max_pages: int = 200) -> list[Candle]:
        return [c for c in self.series[interval] if start_ms <= c.open_ms and c.close_ms <= end_ms]

    def funding(self, start_ms: int, end_ms: int) -> list[tuple[int, float, float]]:
        return [(t, 0.0001, 84_000.0) for t in range(start_ms // (8 * H1) * 8 * H1, end_ms, 8 * H1) if t >= start_ms]

    def price_at(self, t_ms: int) -> float:
        b5 = [c for c in self.series["5m"] if c.open_ms <= t_ms]
        return b5[-1].close if b5 else self.series["15m"][0].open

    def price(self) -> float:
        if self.blocked_until_ms and self.blocked_until_ms > self._now_hint:
            raise RateLimited(418, self.blocked_until_ms, " (not sent)")       # like BinanceData: never sent
        self.requests.append(("price", 0))
        return self.series["15m"][-1].close

    _now_hint = 0

    def close(self) -> None:
        pass


class IWorld:
    """The shipped v2.0.0 config (intraday on) with a live-like BTC-USD mock exchange."""

    def __init__(self, tmp_path: Path, cfg_dict: dict[str, Any], m15: Sequence[Candle], *, cash: float = 1_000.0,
                 fee_rate: float = 0.0004, basis: float = 0.0, events: list[dict[str, Any]] | None = None) -> None:
        self.cfg = config_from_dict(cfg_dict)
        self.calendar = parse_calendar({"calendar_version": "test", "timezone": "America/New_York",
                                        "coverage_end": {"FOMC": "2030-12-31", "CPI": "2030-12-31", "NFP": "2030-12-31"},
                                        "events": events or []})
        self.bn = IntradayBinance(m15)
        self.basis = basis
        start = from_ms(m15[0].open_ms)
        self.clock = FixedClock(start)
        self.store = Store(tmp_path / "db.sqlite3", self.clock, self.cfg.config_version, "test")
        inst = default_instrument()
        for k, v in LIVE_INST.items():
            setattr(inst, k, v)
        self.ex = MockExchange(clock=self.clock, instrument=inst, balance=cash, mark=m15[0].open + basis, spread=2.0,
                               fee_rate=fee_rate)
        self.ex.leverage_cfg[inst.id] = AccountConfig(inst.id, 3, False)
        from conftest import FakeTelegram

        self.tg = FakeTelegram()
        self.secrets = SimpleNamespace(wallet_address="0xOWNER", proxy_expires_at=start + timedelta(days=400),
                                       proxy_address="0xPROXY", proxy_private_key="", proxy_secret="")
        self.t_ms = m15[0].open_ms

    def engine(self) -> Engine:
        return Engine(cfg=self.cfg, calendar=self.calendar, store=self.store, exchange=self.ex, binance=self.bn,
                      telegram=self.tg, clock=self.clock, secrets=self.secrets, sleep=lambda s: None)

    def move_mark(self, t_from: int, t_to: int) -> None:
        """The exchange mark follows the 5m path (low / high in path order), firing triggers on the way."""
        for c in self.bn.series["5m"]:
            if t_from <= c.open_ms < t_to:
                for px in (c.open, c.low if c.close >= c.open else c.high, c.high if c.close >= c.open else c.low,
                           c.close):
                    self.ex.set_mark(px + self.basis)

    def run_at(self, t_ms: int, *, delay_s: int = 65, command: str = "decide") -> dict[str, Any]:
        """The scheduled run 1 minute after the 15-minute close at t_ms."""
        self.move_mark(self.t_ms, t_ms)
        self.t_ms = t_ms
        self.clock.set(from_ms(t_ms + delay_s * 1000))
        e = self.engine()
        return e.cmd_decide() if command == "decide" else e.cmd_manage()

    def run_range(self, t_from: int, t_to: int) -> list[dict[str, Any]]:
        out = []
        t = t_from
        while t <= t_to:
            out.append(self.run_at(t))
            t += M15
        return out

    def fok_calls(self) -> list[dict[str, Any]]:
        return [c[1] for c in self.ex.calls if c[0] == "place_order" and c[1]["tif"] == "fok"]

    def decisions(self) -> list[dict[str, Any]]:
        return self.store.query("SELECT utc_day, action, reason, data FROM decisions ORDER BY id")
