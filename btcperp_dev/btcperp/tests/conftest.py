"""Shared fixtures: a simulated world with a mock exchange, fake Binance data and fake Telegram."""

from __future__ import annotations

import math
import os
import random
import re
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["BTCPERP_NO_TOAST"] = "1"      # never pop real Windows notifications from the test suite
os.environ["BTCPERP_NO_SCHTASKS"] = "1"   # never touch the real Windows scheduled tasks (selftest runs on the bot PC)

from perpbot.calendar_events import parse_calendar  # noqa: E402
from perpbot.config import config_from_dict  # noqa: E402
from perpbot.engine import Engine  # noqa: E402
from perpbot.exchange.base import AccountConfig  # noqa: E402
from perpbot.exchange.mock import MockExchange  # noqa: E402
from perpbot.indicators import Candle  # noqa: E402
from perpbot.storage import Store  # noqa: E402
from perpbot.timeutil import DAY_MS, UTC, FixedClock, day_start_ms, to_ms  # noqa: E402

P = 100_000.0
A = 1_000.0  # half range of a flat candle -> ATR ~ 2000


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    seed = os.environ.get("BTCPERP_TEST_SEED")
    if seed:
        random.Random(int(seed)).shuffle(items)


def hkt(y: int, m: int, d: int, hh: int, mm: int, ss: int = 5) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone(timedelta(hours=8))).astimezone(UTC)


DAILY_DECIDE = ["08:30", "08:50"]
DAILY_MANAGE = ["12:30", "16:30", "20:30", "00:30", "04:30"]


R4H_DECIDE = ["00:30", "00:50", "04:30", "04:50", "08:30", "08:50", "12:30", "12:50", "16:30", "16:50", "20:30", "20:50"]
R4H_MANAGE = ["02:30", "06:30", "10:30", "14:30", "18:30", "22:30"]


def as_rolling_4h(d: dict[str, Any]) -> dict[str, Any]:
    """The v1.5 - v1.8 rolling_4h cadence (test_rolling.py). v1.9.0 ships rolling_1h (test_hourly_v190.py)."""
    d["strategy"]["cadence"] = "rolling_4h"
    d["schedule"]["decide_times_hkt"] = list(R4H_DECIDE)
    d["schedule"]["manage_times_hkt"] = list(R4H_MANAGE)
    d["schedule"]["period_entry_end_minutes"] = 90
    return d


def as_daily(d: dict[str, Any]) -> dict[str, Any]:
    """The v1.4 daily cadence. Most tests exercise the daily rules; the rolling_4h tests (test_rolling.py) use
    the shipped config (strategy.cadence rolling_4h) through `rolling_cfg_dict`."""
    d["strategy"]["cadence"] = "daily"
    d["schedule"]["decide_times_hkt"] = list(DAILY_DECIDE)
    d["schedule"]["manage_times_hkt"] = list(DAILY_MANAGE)
    return d


# The rule tests were written for the v1.5.6 sizing and kill switches (1.5% risk, 10 half-size ramp trades, 30%
# notional cap, no raise to the exchange minimum, 15% drawdown / 8% losing-streak kills, 3x, 2 x SL liquidation
# distance), the 1.5 / 3 ATR brackets and the v1.4 score tiers (30 / 50 -> 25% / 50% / 100%, entry at any score). Later versions ship the owner's
# values: test_sizing_v157.py, test_bold_v170.py and test_sizing_v180.py test those.
TEST_RISK = {"risk_per_trade_pct": 1.5, "ramp_trades": 10, "ramp_factor": 0.5, "notional_cap_pct_equity": 30,
             "raise_to_min_notional": False, "kill_drawdown_pct": 15, "kill_losing_streak_pct": 8, "leverage": 3,
             "equity_floor_pct_of_net_funded": 75, "permanent_floor_pct_of_cumulative_funded": 50,
             "permanent_floor_lowered_in": None, "live_review_expectancy_floor_r": -0.196,
             "notional_multiple_full_tier": None, "liq_min_sl_multiple": 2.0, "liq_after_fill_sl_multiple": None}
TEST_STRATEGY = {"min_entry_abs_score": 0, "tier_low_max": 30, "tier_mid_max": 50, "tier_low_fraction": 0.25,
                 "tier_mid_fraction": 0.50, "tier_high_fraction": 1.00, "size_tiers": None}
TEST_BOLD = {"enabled": False}
TEST_EXITS = {"sl_atr_multiple": 1.5, "tp_atr_multiple": 3.0}         # v1.8.1 ships 1.0 / 1.5
TEST_RISK_YAML = (("risk_per_trade_pct: 5.0 ", "risk_per_trade_pct: 1.5 "), ("ramp_trades: 0 ", "ramp_trades: 10 "),
                  ("notional_cap_pct_equity: 150 ", "notional_cap_pct_equity: 30 "),
                  ("raise_to_min_notional: true ", "raise_to_min_notional: false "),
                  ("kill_drawdown_pct: 95 ", "kill_drawdown_pct: 15 "),
                  ("kill_losing_streak_pct: 95 ", "kill_losing_streak_pct: 8 "),
                  ("  leverage: 25\n", "  leverage: 3\n"),
                  ("liq_min_sl_multiple: 1.3 ", "liq_min_sl_multiple: 2.0 "),
                  ("liq_after_fill_sl_multiple: 1.15 ", "liq_after_fill_sl_multiple: null "),
                  ("size_tiers: [[0, 0.15], [40, 0.30], [50, 0.50], [75, 1.00]]", "size_tiers: null"),
                  ("equity_floor_pct_of_net_funded: 5 ", "equity_floor_pct_of_net_funded: 75 "),
                  ("permanent_floor_pct_of_cumulative_funded: 5 ", "permanent_floor_pct_of_cumulative_funded: 50 "),
                  ('permanent_floor_lowered_in: "1.9.0"', "permanent_floor_lowered_in: null"),
                  ("sl_atr_multiple: 1.0", "sl_atr_multiple: 1.5"), ("tp_atr_multiple: 1.5", "tp_atr_multiple: 3.0"),
                  ("notional_multiple_full_tier: 20", "notional_multiple_full_tier: null"),
                  ("live_review_expectancy_floor_r: null", "live_review_expectancy_floor_r: -0.196"),
                  ("min_entry_abs_score: 20", "min_entry_abs_score: 0"), ("tier_low_max: 40", "tier_low_max: 30"),
                  ("tier_mid_max: 70", "tier_mid_max: 50"), ("tier_low_fraction: 0.15", "tier_low_fraction: 0.25"),
                  ("analysis_toast_only_actions: true ", "analysis_toast_only_actions: false "))


def with_test_risk(d: dict[str, Any]) -> dict[str, Any]:
    d["risk"].update(TEST_RISK)
    d["strategy"].update(TEST_STRATEGY)
    d["bold"].update(TEST_BOLD)
    d["exits"].update(TEST_EXITS)
    d["notifications"]["analysis_toast_only_actions"] = False       # v1.9.0 ships true
    return d


def shipped_config() -> dict[str, Any]:
    import yaml

    return yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def cfg_dict() -> dict[str, Any]:
    return with_test_risk(as_daily(shipped_config()))


@pytest.fixture
def rolling_cfg_dict() -> dict[str, Any]:
    return with_test_risk(as_rolling_4h(shipped_config()))


@pytest.fixture
def hourly_cfg_dict() -> dict[str, Any]:
    return with_test_risk(shipped_config())


@pytest.fixture
def cfg() -> Any:
    return config_from_dict(with_test_risk(as_daily(shipped_config())))


class FakeBinance:
    """Daily/4h candles and 8h funding. Candles are flat at P unless shaped for a given decision day."""

    def __init__(self, end_day: date, days: int = 620) -> None:
        self.daily: dict[int, Candle] = {}
        self.h4: dict[int, Candle] = {}
        self.fund: dict[int, float] = {}
        start = end_day - timedelta(days=days)
        self._span = (start, end_day + timedelta(days=30))
        self._h1: dict[int, Candle] | None = None             # v1.9.0 rolling_1h: built on first use
        d = start
        while d <= end_day + timedelta(days=30):
            o = day_start_ms(d)
            self.daily[o] = Candle(o, P, P + A, P - A, P, 1.0, o + DAY_MS)
            for h in range(6):
                ho = o + h * 4 * 3_600_000
                self.h4[ho] = Candle(ho, P, P + A / 3, P - A / 3, P, 1.0, ho + 4 * 3_600_000)
            for i, h in enumerate((0, 8, 16)):
                ts = o + h * 3_600_000
                n = len(self.fund)
                self.fund[ts] = 0.0001 + 0.00005 * math.sin(n * 0.7)
            d += timedelta(days=1)
        self.fail = False

    # shaping ------------------------------------------------------------
    def set_candle(self, decision_day: date, o: float, h: float, l: float, c: float) -> None:
        ms = day_start_ms(decision_day - timedelta(days=1))
        self.daily[ms] = Candle(ms, o, h, l, c, 1.0, ms + DAY_MS)

    def signal(self, decision_day: date, kind: str) -> None:
        """Shape the candle that closes at decision_day 00:00 UTC."""
        if kind == "strong_long":
            self.set_candle(decision_day, P, P + 3 * A, P - A, P + 3 * A)
        elif kind == "strong_short":
            self.set_candle(decision_day, P, P + A, P - 3 * A, P - 3 * A)
        elif kind == "weak_long":
            self.set_candle(decision_day, P, P + A, P - A, P + 0.4 * A)
        elif kind == "weak_short":
            self.set_candle(decision_day, P, P + A, P - A, P - 0.4 * A)
        elif kind == "flat":
            self.set_candle(decision_day, P, P + A, P - A, P)
        else:
            raise ValueError(kind)

    def set_funding(self, decision_day: date, rate: float) -> None:
        self.fund[day_start_ms(decision_day)] = rate

    @property
    def h1(self) -> dict[int, Candle]:
        """1h candles, flat at P with the daily range (+/- A), so daily candles built from them match `daily`."""
        if self._h1 is None:
            self._h1 = {}
            d, end = self._span
            while d <= end:
                o = day_start_ms(d)
                for h in range(24):
                    ho = o + h * 3_600_000
                    self._h1[ho] = Candle(ho, P, P + A, P - A, P, 1.0, ho + 3_600_000)
                d += timedelta(days=1)
        return self._h1

    def signal_1h(self, t_ms: int, kind: str) -> None:
        """rolling_1h: shape the 1h candle that closes at t_ms, so the daily candle ending at t_ms gives the signal."""
        o = t_ms - 3_600_000
        shapes = {"strong_long": (P, P + 3 * A, P - A / 3, P + 3 * A), "strong_short": (P, P + A / 3, P - 3 * A, P - 3 * A),
                  "weak_long": (P, P + A, P - A, P + 0.4 * A), "flat": (P, P + A, P - A, P)}
        op, hi, lo, cl = shapes[kind]
        self.h1[o] = Candle(o, op, hi, lo, cl, 1.0, t_ms)

    def _src(self, interval: str) -> dict[int, Candle]:
        return {"1d": self.daily, "1h": self.h1}.get(interval, self.h4)

    # BinanceData interface ---------------------------------------------------
    def klines(self, interval: str, limit: int, now_ms: int) -> list[Candle]:
        if self.fail:
            from perpbot.datasources.binance import DataSourceError

            raise DataSourceError("binance down (test)")
        src = self._src(interval)
        closed = [c for k, c in sorted(src.items()) if c.close_ms <= now_ms]
        return closed[-limit:]

    def klines_range(self, interval: str, start_ms: int, end_ms: int, max_pages: int = 200) -> list[Candle]:
        if self.fail:
            from perpbot.datasources.binance import DataSourceError

            raise DataSourceError("binance down (test)")
        src = self._src(interval)
        return [c for k, c in sorted(src.items()) if start_ms <= c.open_ms and c.close_ms <= end_ms]

    def signal_4h(self, t_ms: int, kind: str) -> None:
        """rolling_4h: shape the 4h candle that closes at t_ms, so the daily candle ending at t_ms gives the signal."""
        o = t_ms - 4 * 3_600_000
        shapes = {"strong_long": (P, P + 3 * A, P - A / 3, P + 3 * A), "strong_short": (P, P + A / 3, P - 3 * A, P - 3 * A),
                  "flat": (P, P + A / 3, P - A / 3, P)}
        op, hi, lo, cl = shapes[kind]
        self.h4[o] = Candle(o, op, hi, lo, cl, 1.0, t_ms)

    def funding(self, start_ms: int, end_ms: int) -> list[tuple[int, float, float]]:
        return [(ts, r, P) for ts, r in sorted(self.fund.items()) if start_ms <= ts <= end_ms]

    def price(self) -> float:
        return P

    def close(self) -> None:
        pass


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.docs: list[str] = []
        self.updates: list[dict[str, Any]] = []
        self.chat_id = "42"
        self.enabled = True

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    def send_document(self, path: Path, caption: str = "") -> bool:
        self.docs.append(str(path))
        return True

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        return [u for u in self.updates if offset is None or u["update_id"] >= offset]

    def push(self, text: str, chat: str = "42") -> None:
        uid = 1000 + len(self.updates)
        self.updates.append({"update_id": uid, "message": {"chat": {"id": int(chat)}, "text": text}})

    def close(self) -> None:
        pass

    def has(self, needle: str) -> bool:
        return any(needle.lower() in m.lower() for m in self.sent)


class World:
    def __init__(self, tmp_path: Path, start: datetime, cfg_dict: dict[str, Any], events: list[dict[str, Any]] | None = None,
                 **overrides: Any) -> None:
        for dotted, value in overrides.items():
            cur = cfg_dict
            parts = dotted.split("__")
            for p in parts[:-1]:
                cur = cur[p]
            cur[parts[-1]] = value
        self.cfg = config_from_dict(cfg_dict)
        self.calendar = parse_calendar({"calendar_version": "test", "timezone": "America/New_York",
                                        "coverage_end": {"FOMC": "2030-12-31", "CPI": "2030-12-31", "NFP": "2030-12-31"},
                                        "events": events or []})
        self.clock = FixedClock(start)
        self.store = Store(tmp_path / "db.sqlite3", self.clock, self.cfg.config_version, "test")
        self.ex = MockExchange(clock=self.clock, balance=10_000.0, mark=P, spread=10.0)
        self.bn = FakeBinance(start.date())
        self.tg = FakeTelegram()
        self.secrets = SimpleNamespace(wallet_address="0xOWNER", proxy_expires_at=start + timedelta(days=30),
                                       proxy_address="0xPROXY", proxy_private_key="", proxy_secret="")

    def engine(self) -> Engine:
        return Engine(cfg=self.cfg, calendar=self.calendar, store=self.store, exchange=self.ex, binance=self.bn,
                      telegram=self.tg, clock=self.clock, secrets=self.secrets, sleep=lambda s: None)

    def at(self, t: datetime) -> "World":
        self.clock.set(t)
        return self

    def decide(self) -> dict[str, Any]:
        return self.engine().cmd_decide()

    def manage(self) -> dict[str, Any]:
        return self.engine().cmd_manage()

    def pos(self) -> float:
        return self.ex.pos_size

    def fok_calls(self) -> list[dict[str, Any]]:
        return [c[1] for c in self.ex.calls if c[0] == "place_order" and c[1]["tif"] == "fok"]

    def state(self) -> dict[str, Any]:
        from perpbot.records import Records

        return Records(self.store).state()


@pytest.fixture
def world(tmp_path: Path, cfg_dict: dict[str, Any]) -> Any:
    def make(start: datetime, events: list[dict[str, Any]] | None = None, **overrides: Any) -> World:
        return World(tmp_path, start, cfg_dict, events, **overrides)
    return make


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    root = tmp_path / "btcperp"
    (root / "config").mkdir(parents=True)
    text = (ROOT / "config" / "config.yaml").read_text(encoding="utf-8")
    text, n1 = re.subn(r'decide_times_hkt: \[[^\]]*\]', 'decide_times_hkt: ["08:30", "08:50"]', text)
    text, n2 = re.subn(r'manage_times_hkt: \[[^\]]*\]', 'manage_times_hkt: ["12:30", "16:30", "20:30", "00:30", "04:30"]',
                       text)
    assert n1 == 1 and n2 == 1
    for a, b in (('cadence: "rolling_1h"', 'cadence: "daily"'),) + TEST_RISK_YAML:
        assert a in text, a
        text = text.replace(a, b)
    (root / "config" / "config.yaml").write_text(text, encoding="utf-8")     # daily cadence, like cfg_dict
    shutil.copy2(ROOT / "config" / "calendar.yaml", root / "config" / "calendar.yaml")
    (root / "tests").mkdir()
    (root / "VERSION").write_text("test\n", encoding="utf-8")
    return root


def make_env(root: Path) -> str:
    from eth_account import Account

    key = "0x" + os.urandom(32).hex()
    acct = Account.from_key(key)
    (root / ".env").write_text(
        f"PM_PROXY_PRIVATE_KEY={key}\nPM_PROXY_SECRET=test-secret-value-123\nPM_WALLET_ADDRESS=0xOWNER\n"
        f"PM_PROXY_EXPIRES_AT=2030-01-01T00:00:00Z\nTELEGRAM_BOT_TOKEN=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij\n"
        f"TELEGRAM_CHAT_ID=42\n", encoding="utf-8")
    return acct.address


def ok_leverage(ex: MockExchange) -> None:
    ex.leverage_cfg[ex.inst.id] = AccountConfig(ex.inst.id, 3, False)


__all__ = ["A", "P", "FakeBinance", "FakeTelegram", "World", "hkt", "make_env", "ok_leverage", "to_ms"]
