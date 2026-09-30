"""Backtest (review B3). Runs on the owner's computer from downloaded Binance data. Design: BACKTEST.md.

- Decisions use exactly the live code path (`strategy.day_features` + `strategy.plan_for`) fed with what the
  live `decide` sees at 00:30 UTC (08:30 HKT). Strategy values: this config, unchanged.
- Execution (conservative): fills at the close of the 00:00-01:00 UTC 1h candle; entry and exit slippage; bracket
  SL/TP on 1h candles, a candle touching both counts as the SL; a candle that OPENS beyond the SL fills at its open
  (gap); an isolated loss never exceeds the margin (review v1.3.0 BT1/BT6).
- Costs: `shadow._r` (fees both sides, Binance funding) plus exit slippage; each candidate has a stress twin with
  double fees, 30 bps exit slippage and double paid funding (BT3). Fee = max(config estimate, smoketest taker fee).
- Equity and drawdown are tracked hour by hour while a position is open (BT2). Kill switches are checked once a
  day (live: 7 times) - conservative: they stop later.
- 6-month windows at full risk from the first trade; the chained full-period runs keep the live ramp (S6).
- Pass/fail: config/backtest_criteria.yaml (C0-C7), locked with the code, config, calendars, data slice and fee
  by `backtest confirm` (R1). Variant choice is pre-registered (S5): `select_variant`.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import statistics
from bisect import bisect_right
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from perpbot.calendar_events import EventCalendar, parse_calendar
from perpbot.exchange.base import Instrument, mark_vs_book
from perpbot.indicators import Candle
from perpbot.risk import compute_size, is_tie, losing_streak, risk_pct_for_trade
from perpbot.shadow import SimTrade, _r, _slipped
from perpbot.strategy import (
    H4_MS,
    DayFeatures,
    InsufficientData,
    day_features,
    h4_gate_window,
    period_features,
    period_key,
    plan_for,
    rolling_score,
    sc_day,
)
from perpbot.timeutil import DAY_MS, HOUR_MS, MINUTE_MS, day_start_ms, fmt_utc

log = logging.getLogger(__name__)

UTC = timezone.utc
INTERVALS = {"1d": DAY_MS, "4h": 4 * HOUR_MS, "1h": HOUR_MS}
DECISION_OFFSET_MS = 30 * MINUTE_MS          # 00:30 UTC = 08:30 HKT, the live decide time
FILL_OFFSET_MS = HOUR_MS                     # close of the 00:00-01:00 UTC candle

@dataclass(frozen=True)
class Variant:
    name: str
    breakeven: bool = False        # V2: one-time SL move to breakeven at +1 ATR
    control: bool = False          # |score| < tier_low_max means flat
    funding_close: bool = True     # crowded funding closes the position (live) or only blocks entries
    events: bool = True            # event gate on
    stress: bool = False           # double fees, 30 bps exit slippage, double paid funding (review BT3)
    kills: bool = True             # kill switches on
    role: str = "candidate"        # candidate | stress | sensitivity
    twin: str | None = None        # stress twin of a candidate
    cadence: str = "daily"         # daily | rolling_4h (v1.5.0 option B: decide every 4h on daily candles ending then)
    flip_confirm: int = 1          # rolling_4h: consecutive opposite periods a flip needs


def _variants() -> dict[str, Variant]:
    r = "rolling_4h"
    base = [Variant("R4h_live", cadence=r), Variant("R4h_confirm", cadence=r, flip_confirm=2),
            Variant("A_live"), Variant("B_breakeven", breakeven=True), Variant("C_control", control=True),
            Variant("A_live_fhold", funding_close=False), Variant("B_breakeven_fhold", breakeven=True, funding_close=False),
            Variant("C_control_fhold", control=True, funding_close=False)]
    out: dict[str, Variant] = {}
    for v in base:
        out[v.name] = replace(v, twin=f"{v.name}_stress")
        out[f"{v.name}_stress"] = replace(v, name=f"{v.name}_stress", stress=True, role="stress")
    out["R4h_live_noevents"] = Variant("R4h_live_noevents", events=False, role="sensitivity", cadence=r)   # calendar errors
    out["R4h_live_nokill"] = Variant("R4h_live_nokill", kills=False, role="sensitivity", cadence=r)        # BT4 streaks
    return out


VARIANTS: dict[str, Variant] = _variants()
PRIMARY = "R4h_live"            # v1.5.0: the owner chose option B (rolling 4h); A_live = the v1.4 daily cadence


class BacktestError(Exception):
    pass


# ================================================================ data
@dataclass
class Dataset:
    daily: list[Candle]
    h4: list[Candle]
    h1: list[Candle]
    funding: list[tuple[int, float, float]]
    pm_h1: list[Candle] = field(default_factory=list)      # Polymarket's own 1h candles (I7), may be empty

    def last_day(self) -> date:
        """Last UTC day that has a complete decision + fill (its 00:00-01:00 1h candle)."""
        last_ms = self.h1[-1].open_ms if self.h1 else 0
        return datetime.fromtimestamp(last_ms / 1000, tz=UTC).date() - timedelta(days=1)


def _csv_path(d: Path, name: str) -> Path:
    return d / f"binance_{name}.csv"


def _read_candles(path: Path, step: int) -> list[Candle]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            o = int(row["open_ms"])
            out.append(Candle(o, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]),
                              float(row["volume"]), o + step))
    return out


def _write_candles(path: Path, candles: list[Candle]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["open_ms", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.open_ms, repr(c.open), repr(c.high), repr(c.low), repr(c.close), repr(c.volume)])
    tmp.replace(path)


def _read_funding(path: Path) -> list[tuple[int, float, float]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [(int(r["ts_ms"]), float(r["rate"]), float(r["mark"])) for r in csv.DictReader(fh)]


def _write_funding(path: Path, rows: list[tuple[int, float, float]]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts_ms", "rate", "mark"])
        for ts, r, m in rows:
            w.writerow([ts, repr(r), repr(m)])
    tmp.replace(path)


def load_dataset(d: Path) -> Dataset:
    ds = Dataset(_read_candles(_csv_path(d, "1d"), DAY_MS), _read_candles(_csv_path(d, "4h"), 4 * HOUR_MS),
                 _read_candles(_csv_path(d, "1h"), HOUR_MS), _read_funding(_csv_path(d, "funding")),
                 _read_candles(d / "polymarket_1h.csv", HOUR_MS))
    if not (ds.daily and ds.h4 and ds.h1 and ds.funding):
        raise BacktestError(f"no downloaded data in {d}: run `backtest download` first")
    return ds


def download(bn: Any, d: Path, start: date, now_ms: int, pm: Any = None, cfg: Any = None) -> dict[str, int]:
    """Incremental download of Binance history into CSV files (public endpoints, no keys)."""
    d.mkdir(parents=True, exist_ok=True)
    counts = {}
    start_ms = day_start_ms(start)
    for name, step in INTERVALS.items():
        path = _csv_path(d, name)
        have = _read_candles(path, step)
        begin = have[-1].open_ms + step if have else start_ms
        end = (now_ms // step) * step                                  # closed candles only
        new = bn.klines_range(name, begin, end) if begin < end else []
        merged = {c.open_ms: c for c in have + new}
        rows = [merged[k] for k in sorted(merged)]
        _write_candles(path, rows)
        counts[name] = len(rows)
    fpath = _csv_path(d, "funding")
    have_f = _read_funding(fpath)
    fbegin = have_f[-1][0] + 1 if have_f else start_ms
    newf = bn.funding(fbegin, now_ms) if fbegin < now_ms else []
    mf = {ts: (ts, r, m) for ts, r, m in have_f + newf}
    _write_funding(fpath, [mf[k] for k in sorted(mf)])
    counts["funding"] = len(mf)
    if pm is not None:
        counts["polymarket_1h"] = download_polymarket(pm, d, now_ms, cfg)
    return counts


def download_polymarket(pm: Any, d: Path, now_ms: int, cfg: Any) -> int:
    """I7: Polymarket's own 1h candles for BTC-PERP (public data), as far back as the exchange serves them."""
    insts = pm.get_instruments()
    m = cfg.market
    by_sym = {i.symbol: i for i in insts}
    inst = next((by_sym[s] for s in m.symbol_candidates if s in by_sym), None)
    if inst is None:
        cands = [i for i in insts if i.base_asset == m.base_asset and i.quote_asset == m.quote_asset]
        if len(cands) != 1:
            return 0
        inst = cands[0]
    path = d / "polymarket_1h.csv"
    have = _read_candles(path, HOUR_MS)
    begin = have[-1].open_ms + HOUR_MS if have else day_start_ms(date.fromisoformat(str(cfg.backtest.polymarket_start)))
    end = (now_ms // HOUR_MS) * HOUR_MS
    new: list[Candle] = []
    cur = begin
    while cur < end:                                   # 30-day pages
        stop = min(end, cur + 30 * DAY_MS)
        new += [c for c in pm.get_klines(inst.id, "1h", cur, stop) if c.open_ms + HOUR_MS <= end]
        cur = stop
    if new:                     # v1.5.3: the candles must be THIS instrument's (the live ticker once was another's)
        book = pm.get_book(inst.id, 10)
        ok, dev = mark_vs_book(new[-1].close, book, 0.10)
        if not ok:
            log.warning("Polymarket 1h candles skipped: last close %s does not match the %s order book (%s)",
                        new[-1].close, inst.symbol, dev)
            return len(have)
    merged = {c.open_ms: c for c in have + new}
    _write_candles(path, [merged[k] for k in sorted(merged)])
    return len(merged)


def merged_calendar(cfg: Any, root: Path, live_calendar: EventCalendar) -> EventCalendar:
    import yaml

    raw = yaml.safe_load((root / cfg.backtest.calendar_history_file).read_text(encoding="utf-8"))
    hist = parse_calendar(raw)
    events = tuple(sorted({(e.release_utc, e.type): e for e in hist.events + live_calendar.events}.values(),
                          key=lambda e: e.release_utc))
    return EventCalendar(version=f"{hist.version}+{live_calendar.version}", events=events,
                         coverage_end=live_calendar.coverage_end)


# ================================================================ decision inputs (identical to live decide)
class DayInputs:
    """Slices the downloaded data exactly like the live `gather_inputs` at 00:30 UTC of day D."""

    def __init__(self, cfg: Any, ds: Dataset, calendar: EventCalendar) -> None:
        self.cfg, self.ds, self.cal = cfg, ds, calendar
        self._d_close = [c.close_ms for c in ds.daily]
        self._h4_close = [c.close_ms for c in ds.h4]
        self._f_ts = [f[0] for f in ds.funding]

    def inputs(self, day_d: date) -> tuple[list[Candle], list[Candle], list[tuple[int, float]], list[dict[str, Any]]]:
        cfg = self.cfg
        t_dec = day_start_ms(day_d) + DECISION_OFFSET_MS
        i = bisect_right(self._d_close, t_dec)
        daily = self.ds.daily[max(0, i - int(cfg.binance.daily_candles_to_load)):i]
        j = bisect_right(self._h4_close, t_dec)
        h4 = self.ds.h4[max(0, j - int(cfg.binance.h4_candles_to_load)):j]
        f0 = day_start_ms(day_d) - int(cfg.binance.funding_days_to_load) * DAY_MS
        a, b = bisect_right(self._f_ts, f0 - 1), bisect_right(self._f_ts, t_dec)
        funding = [(ts, r) for ts, r, _ in self.ds.funding[a:b]]
        at = datetime.fromtimestamp(t_dec / 1000, tz=UTC)
        active = self.cal.active_windows(at, cfg.gates.event_anchor_hkt, float(cfg.gates.event_post_release_hours))
        events = [{"type": e.type, "release_utc": fmt_utc(e.release_utc), "window_start_utc": fmt_utc(s),
                   "window_end_utc": fmt_utc(en), "note": e.note} for e, s, en in active]
        return daily, h4, funding, events

    def features(self, day_d: date) -> DayFeatures | None:
        daily, h4, funding, events = self.inputs(day_d)
        try:
            return day_features(self.cfg, day_d, daily, h4, funding, events)
        except InsufficientData:
            return None


class PeriodInputs:
    """rolling_4h: features at each 4h boundary T exactly like the live decide at T + 30 min. The daily candles
    ending at T are the same shifted candles live builds from 4h data (strategy.shifted_daily), taken here from
    six precomputed phase series; scores are cached, which only saves time (a test checks equality with live)."""

    def __init__(self, cfg: Any, ds: Dataset, calendar: EventCalendar) -> None:
        self.cfg, self.ds, self.cal = cfg, ds, calendar
        self.h4 = sorted(ds.h4, key=lambda c: c.open_ms)
        self.h4_open = [c.open_ms for c in self.h4]
        self._f_ts = [f[0] for f in ds.funding]
        self.n_days = int(cfg.binance.daily_candles_to_load)
        self._phase: dict[int, tuple[list[Candle], list[int]]] = {}
        self._cache: dict[int, Any] = {}

    def _series(self, phase_ms: int) -> tuple[list[Candle], list[int]]:
        if phase_ms not in self._phase:
            buckets: dict[int, list[Candle]] = {}
            for c in self.h4:
                buckets.setdefault((c.open_ms - phase_ms) // DAY_MS, []).append(c)
            out = []
            for k in sorted(buckets):
                b = buckets[k]
                o = k * DAY_MS + phase_ms
                out.append(Candle(o, b[0].open, max(c.high for c in b), min(c.low for c in b), b[-1].close,
                                  sum(c.volume for c in b), o + DAY_MS))
            self._phase[phase_ms] = (out, [c.open_ms for c in out])
        return self._phase[phase_ms]

    def score_at(self, t_ms: int) -> Any:
        if t_ms not in self._cache:
            series, opens = self._series(t_ms % DAY_MS)
            a, b = bisect_right(opens, t_ms - self.n_days * DAY_MS - 1), bisect_right(opens, t_ms - DAY_MS)
            try:
                self._cache[t_ms] = rolling_score(series[a:b], t_ms, self.cfg.strategy)
            except InsufficientData as e:
                self._cache[t_ms] = e
        v = self._cache[t_ms]
        if isinstance(v, InsufficientData):
            raise v
        return v

    def features(self, t_ms: int) -> DayFeatures | None:
        cfg = self.cfg
        t_dec = t_ms + DECISION_OFFSET_MS
        gate = h4_gate_window(self.h4, t_ms, int(cfg.binance.h4_candles_to_load), opens=self.h4_open)
        f0 = day_start_ms(sc_day(t_ms)) - int(cfg.binance.funding_days_to_load) * DAY_MS
        a, b = bisect_right(self._f_ts, f0 - 1), bisect_right(self._f_ts, t_dec)
        funding = [(ts, r) for ts, r, _ in self.ds.funding[a:b]]
        at = datetime.fromtimestamp(t_dec / 1000, tz=UTC)
        active = self.cal.active_windows(at, cfg.gates.event_anchor_hkt, float(cfg.gates.event_post_release_hours))
        events = [{"type": e.type, "release_utc": fmt_utc(e.release_utc), "window_start_utc": fmt_utc(st),
                   "window_end_utc": fmt_utc(en), "note": e.note} for e, st, en in active]
        try:
            return period_features(cfg, t_ms, self.score_at, gate, funding, events)
        except InsufficientData:
            return None


# ================================================================ simulation
@dataclass
class BtTrade:
    entry_day: str
    direction: int
    qty: float
    entry_price: float
    entry_ts: int
    atr: float
    sl: float
    tp: float
    fraction: float
    risk_usd: float
    equity_at_entry: float
    capped: bool
    score: float
    gates: list[str]
    be_moved: bool = False
    exit_price: float | None = None
    exit_ts: int | None = None
    exit_reason: str | None = None
    net_pnl: float | None = None
    r: float | None = None
    gap_fill: bool = False


@dataclass
class RunResult:
    variant: str
    start: str
    end: str
    offset_days: int
    start_equity: float
    end_equity: float
    trades: list[BtTrade] = field(default_factory=list)
    equity_curve: list[tuple[str, float]] = field(default_factory=list)
    kills: dict[str, int] = field(default_factory=lambda: {"drawdown": 0, "losing_streak": 0, "equity_floor": 0})
    entries_skipped: dict[str, int] = field(default_factory=dict)
    max_streak_loss_pct: float = 0.0
    kill_log: list[tuple[str, str]] = field(default_factory=list)
    max_dd_pct: float = 0.0                       # intraday: hourly adverse extremes while holding (review BT2)
    holding_gaps: list[tuple[str, float]] = field(default_factory=list)   # (start UTC, hours) missing while holding


class _Tracker:
    """Hour-by-hour mark-to-market peak and drawdown (review BT2)."""

    def __init__(self, equity: float) -> None:
        self.peak = equity
        self.mdd = 0.0

    def observe(self, adverse: float, close: float | None = None) -> None:
        if adverse > 0 and self.peak > 0:
            self.mdd = max(self.mdd, (self.peak - adverse) / self.peak * 100.0)
        self.peak = max(self.peak, close if close is not None else adverse)


def _synthetic_instrument(cfg: Any) -> Instrument:
    return Instrument(0, "BTC-BACKTEST", "BTC", "USD", "crypto", int(cfg.backtest.quantity_decimals), 2, 0.0, 0.0, 0.0,
                      int(cfg.risk.leverage), True, 0.0, 0, "8h")


class Simulator:
    def __init__(self, cfg: Any, h1: list[Candle], funding: list[tuple[int, float, float]],
                 feats: dict[date, DayFeatures | None], fee_rate: float,
                 pfeats: dict[int, DayFeatures | None] | None = None) -> None:
        self.cfg, self.h1, self.feats, self.fee = cfg, h1, feats, fee_rate
        self.pfeats = pfeats or {}                         # rolling_4h features by period start T (ms)
        self.h1_open = [c.open_ms for c in h1]
        self.funding = [(ts, r) for ts, r, _ in funding]
        self._f_ts = [ts for ts, _ in self.funding]
        self.inst = _synthetic_instrument(cfg)

    def _candle_close(self, open_ms: int) -> float | None:
        i = bisect_right(self.h1_open, open_ms) - 1
        return self.h1[i].close if i >= 0 and self.h1[i].open_ms == open_ms else None

    def _exit_px(self, t: BtTrade, price: float, v: Variant) -> float:
        bps = float(self.cfg.backtest.stress_exit_slippage_bps if v.stress else self.cfg.backtest.exit_slippage_bps)
        px = price * (1 - t.direction * bps / 1e4)
        cap = t.entry_price * (1 - t.direction / float(self.cfg.risk.leverage))      # isolated: loss <= margin (BT6)
        return max(px, cap) if t.direction > 0 else min(px, cap)

    def _pnl(self, t: BtTrade, v: Variant) -> float:
        sl_mult = float(self.cfg.exits.sl_atr_multiple)
        if not v.stress:
            st = SimTrade("bt", t.entry_day, t.direction, t.qty * sl_mult * t.atr, t.entry_price, t.entry_ts, t.atr,
                          t.sl, t.tp, exit_price=t.exit_price, exit_ts_ms=t.exit_ts)
            return _r(st, sl_mult, self.fee, self.funding)                        # exactly the shadow cost model
        gross = (float(t.exit_price) - t.entry_price) * t.direction * t.qty
        fees = 2 * (2 * self.fee) * t.entry_price * t.qty
        a, b = bisect_right(self._f_ts, t.entry_ts), bisect_right(self._f_ts, int(t.exit_ts or t.entry_ts))
        paid = 0.0
        for _, rate in self.funding[a:b]:
            c = rate * t.entry_price * t.direction * t.qty
            paid += 2 * c if c > 0 else c                                         # paid funding x2, received unchanged
        return gross - fees - paid

    def _book(self, t: BtTrade, price: float, ts: int, reason: str, v: Variant, res: RunResult, eq: list[float],
              tr: _Tracker) -> None:
        t.exit_price, t.exit_ts, t.exit_reason = self._exit_px(t, price, v), ts, reason
        t.net_pnl = self._pnl(t, v)
        t.r = t.net_pnl / t.risk_usd if t.risk_usd else 0.0
        eq[0] += t.net_pnl
        tr.observe(eq[0])
        res.trades.append(t)

    def _walk(self, t: BtTrade, from_ms: int, to_ms: int, v: Variant, eq: float, tr: _Tracker,
              res: RunResult) -> tuple[float, int, str] | None:
        """Bracket SL/TP on 1h candles with open time in [from_ms, to_ms). Tracks equity and gaps while holding."""
        be_atr = float(self.cfg.shadow.breakeven_trigger_atr)
        i = bisect_right(self.h1_open, from_ms - 1)
        expect = from_ms
        long_ = t.direction > 0
        while i < len(self.h1) and self.h1[i].open_ms < to_ms:
            c = self.h1[i]
            i += 1
            if c.open_ms > expect:
                res.holding_gaps.append((fmt_utc(datetime.fromtimestamp(expect / 1000, tz=UTC)),
                                         (c.open_ms - expect) / HOUR_MS))
            expect = c.open_ms + HOUR_MS
            gap_sl = c.open <= t.sl if long_ else c.open >= t.sl
            sl_hit = gap_sl or (c.low <= t.sl if long_ else c.high >= t.sl)
            tp_hit = c.high >= t.tp if long_ else c.low <= t.tp
            if sl_hit:                                            # SL first when both touch (conservative)
                if gap_sl:
                    t.gap_fill = True
                    return c.open, c.open_ms, "BE" if t.be_moved else "SL"       # gap: fill at the open (BT1)
                return t.sl, c.close_ms, "BE" if t.be_moved else "SL"
            if tp_hit:
                return t.tp, c.close_ms, "TP"
            adverse = c.low if long_ else c.high
            tr.observe(eq + t.qty * (adverse - t.entry_price) * t.direction,
                       eq + t.qty * (c.close - t.entry_price) * t.direction)
            if v.breakeven and not t.be_moved:
                fav = c.high - t.entry_price if long_ else t.entry_price - c.low
                if fav >= be_atr * t.atr:
                    t.sl, t.be_moved = t.entry_price, True
        if expect < to_ms and i >= len(self.h1):
            pass                                                   # end of data: not a gap
        elif expect < to_ms:
            res.holding_gaps.append((fmt_utc(datetime.fromtimestamp(expect / 1000, tz=UTC)), (to_ms - expect) / HOUR_MS))
        return None

    def run(self, variant: str | Variant, start: date, end: date, offset: int, equity0: float,
            ramp: bool = False) -> RunResult:
        v = VARIANTS[variant] if isinstance(variant, str) else variant
        cfg = self.cfg
        s, rk = cfg.strategy, cfg.risk
        res = RunResult(v.name, start.isoformat(), end.isoformat(), offset, equity0, equity0)
        eq = [equity0]                                  # realised equity (mutable cell)
        tr = _Tracker(equity0)
        peak = equity0
        trade: BtTrade | None = None
        pause_until: int | None = None                  # ms
        pause_reason: str | None = None
        stopped = False
        since_resume: list[BtTrade] = []
        opened = 0 if ramp else int(rk.ramp_trades)     # review S6: windows at full risk from the first trade
        last_ms = day_start_ms(start)
        pause_days = int(cfg.backtest.kill_pause_days)
        rolling = v.cadence == "rolling_4h"
        step = H4_MS if rolling else DAY_MS

        def after_close(t: BtTrade, day: date, t_ms: int) -> None:
            nonlocal pause_until, pause_reason
            since_resume.append(t)
            st = losing_streak([{"net_pnl": x.net_pnl, "equity_at_entry": x.equity_at_entry} for x in reversed(since_resume)],
                               float(rk.kill_losing_streak_pct), float(rk.losing_streak_tie_pct))
            res.max_streak_loss_pct = max(res.max_streak_loss_pct, st.loss_pct)
            if v.kills and st.triggered and pause_reason is None:
                res.kills["losing_streak"] += 1
                res.kill_log.append((day.isoformat(), "losing_streak"))
                pause_until, pause_reason = t_ms + pause_days * DAY_MS, "kill_losing_streak"

        t_now, t_end = day_start_ms(start), day_start_ms(end)
        while t_now < t_end:
            d = sc_day(t_now)
            key = period_key(t_now) if rolling else d.isoformat()
            fill_ms = t_now + FILL_OFFSET_MS
            if trade is not None:
                hit = self._walk(trade, last_ms, fill_ms, v, eq[0], tr, res)
                if hit is not None:
                    self._book(trade, hit[0], hit[1], hit[2], v, res, eq, tr)
                    after_close(trade, d, t_now)
                    trade = None
            last_ms = fill_ms
            mark = self._candle_close(t_now)
            if mark is None:
                t_now += step
                continue
            mtm = eq[0] + (trade.qty * (mark - trade.entry_price) * trade.direction if trade else 0.0)
            if pause_until is not None and t_now >= pause_until and not stopped:
                pause_until, pause_reason = None, None          # automatic resume: peak reset, streak restarted
                peak = mtm
                since_resume.clear()
            peak = max(peak, mtm)
            if v.kills and not stopped and mtm < equity0 * float(rk.equity_floor_pct_of_net_funded) / 100.0:
                res.kills["equity_floor"] += 1
                res.kill_log.append((d.isoformat(), "equity_floor"))
                stopped = True
                if trade is not None:
                    self._book(trade, mark, fill_ms, "kill_switch", v, res, eq, tr)
                    trade = None
            elif v.kills and not stopped and pause_reason != "kill_drawdown" and peak > 0 and \
                    (peak - mtm) / peak * 100.0 >= float(rk.kill_drawdown_pct):
                res.kills["drawdown"] += 1
                res.kill_log.append((d.isoformat(), "drawdown"))
                pause_until, pause_reason = t_now + pause_days * DAY_MS, "kill_drawdown"
                if trade is not None:
                    self._book(trade, mark, fill_ms, "kill_switch", v, res, eq, tr)
                    trade = None
            f = self.pfeats.get(t_now) if rolling else self.feats.get(d)
            if f is not None and not stopped:
                pos_dir = trade.direction if trade else 0
                entry_day = date.fromisoformat(trade.entry_day[:10]) if trade else d
                plan, _ = plan_for(f, cfg, position_dir=pos_dir, entry_day=entry_day, entered_today=False,
                                   paused_reason=pause_reason, funding_rule_closes=v.funding_close,
                                   ignore_events=not v.events, entry_key=trade.entry_day if trade else key,
                                   flip_confirm_periods=v.flip_confirm)
                close_reason, enter_dir, frac = plan.close_reason, plan.enter_direction, plan.enter_fraction
                if v.control and plan.action != "paused" and f.score.abs_score < float(s.tier_low_max):
                    enter_dir, frac = 0, 0.0
                    if trade is not None and close_reason is None:
                        close_reason = "flat_rule"
                if trade is not None and close_reason:
                    self._book(trade, mark, fill_ms, close_reason, v, res, eq, tr)
                    after_close(trade, d, t_now)
                    trade = None
                if enter_dir and trade is None and pause_reason is None:
                    price = _slipped(mark, enter_dir, cfg)
                    pct, _ = risk_pct_for_trade(rk, opened)
                    size = compute_size(equity=eq[0], risk_pct=pct, fraction=frac, price=price, atr=f.score.atr,
                                        sl_atr_multiple=float(cfg.exits.sl_atr_multiple),
                                        notional_cap_pct=float(rk.notional_cap_pct_equity), leverage=int(rk.leverage),
                                        inst=self.inst)
                    if not size.ok or size.qty <= Decimal(0):
                        key = size.reject_reason or "size"
                        res.entries_skipped[key] = res.entries_skipped.get(key, 0) + 1
                    else:
                        qty = float(size.qty)
                        atr = f.score.atr
                        sl_d, tp_d = float(cfg.exits.sl_atr_multiple) * atr, float(cfg.exits.tp_atr_multiple) * atr
                        trade = BtTrade(key, enter_dir, qty, price, fill_ms, atr, price - enter_dir * sl_d,
                                        price + enter_dir * tp_d, frac, qty * sl_d, eq[0],
                                        any("notional" in c for c in size.capped_by), f.score.score, f.gates_triggered())
                        opened += 1
            mtm = eq[0] + (trade.qty * (mark - trade.entry_price) * trade.direction if trade else 0.0)
            if t_now % DAY_MS == 0:
                res.equity_curve.append((d.isoformat(), mtm))                 # one point per UTC day
            t_now += step
        if trade is not None:
            end_ms = day_start_ms(end)
            hit = self._walk(trade, last_ms, end_ms, v, eq[0], tr, res)
            if hit is not None:
                self._book(trade, hit[0], hit[1], hit[2], v, res, eq, tr)
            else:
                px = self._candle_close(end_ms - HOUR_MS) or trade.entry_price
                self._book(trade, px, end_ms, "end_of_run", v, res, eq, tr)
        res.end_equity = eq[0]
        res.max_dd_pct = tr.mdd
        return res


# ================================================================ data quality (review R2 / C0)
def data_end_day(ds: Dataset) -> date:
    return ds.last_day() + timedelta(days=1)          # exclusive end: last UTC day with a complete decision + fill


def _series_quality(candles: list[Candle], step: int, start_ms: int, end_ms: int) -> dict[str, Any]:
    have = [c.open_ms for c in candles if start_ms <= c.open_ms < end_ms]
    expected = max(0, (end_ms - start_ms) // step)
    gaps = []
    prev = start_ms - step
    for o in have:
        if o - prev > step:
            gaps.append([fmt_utc(datetime.fromtimestamp((prev + step) / 1000, tz=UTC)), (o - prev - step) / HOUR_MS])
        prev = o
    if have and end_ms - have[-1] > step:
        gaps.append([fmt_utc(datetime.fromtimestamp((have[-1] + step) / 1000, tz=UTC)), (end_ms - have[-1] - step) / HOUR_MS])
    missing = expected - len(set(have))
    return {"expected": expected, "present": len(set(have)), "missing": missing,
            "missing_share": (missing / expected) if expected else None, "gaps": gaps[:100], "gap_count": len(gaps),
            "max_gap_hours": max((g[1] for g in gaps), default=0.0)}


def _slice_sha(rows: list[tuple[Any, ...]]) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update((",".join(repr(x) for x in r) + "\n").encode())
    return h.hexdigest()


def data_quality(cfg: Any, ds: Dataset, first: date, end: date) -> dict[str, Any]:
    end_ms = day_start_ms(end)
    t0 = day_start_ms(first)
    fund0 = t0 - int(cfg.gates.funding_lookback_days) * DAY_MS
    f_in = [f for f in ds.funding if fund0 <= f[0] < end_ms]
    f_gaps = [[fmt_utc(datetime.fromtimestamp(a[0] / 1000, tz=UTC)), (b[0] - a[0]) / HOUR_MS]
              for a, b in zip(f_in, f_in[1:]) if b[0] - a[0] > 8 * HOUR_MS + 5 * MINUTE_MS]
    return {
        "range": [first.isoformat(), end.isoformat()],
        "h1": _series_quality(ds.h1, HOUR_MS, t0, end_ms),
        "h4": _series_quality(ds.h4, 4 * HOUR_MS, t0, end_ms),
        "d1": _series_quality(ds.daily, DAY_MS, t0, end_ms),
        "funding_gaps_over_8h": f_gaps,
        "slice_sha256": {
            "1d": _slice_sha([(c.open_ms, c.open, c.high, c.low, c.close) for c in ds.daily if c.open_ms < end_ms]),
            "4h": _slice_sha([(c.open_ms, c.open, c.high, c.low, c.close) for c in ds.h4 if c.open_ms < end_ms]),
            "1h": _slice_sha([(c.open_ms, c.open, c.high, c.low, c.close) for c in ds.h1 if c.open_ms < end_ms]),
            "funding": _slice_sha([(ts, r) for ts, r, _ in ds.funding if ts < end_ms]),
        },
    }


# ================================================================ metrics
def run_metrics(r: RunResult, tie_pct: float) -> dict[str, Any]:
    ts = r.trades
    pnl = [t.net_pnl or 0.0 for t in ts]
    risk = sum(t.risk_usd for t in ts)
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    days = max(1, len(r.equity_curve))
    held = sum(((t.exit_ts or t.entry_ts) - t.entry_ts) / DAY_MS for t in ts)
    by_exit: dict[str, int] = {}
    for t in ts:
        by_exit[t.exit_reason or "?"] = by_exit.get(t.exit_reason or "?", 0) + 1
    return {
        "variant": r.variant, "start": r.start, "end": r.end, "offset_days": r.offset_days,
        "start_equity": r.start_equity, "end_equity": r.end_equity,
        "return_pct": (r.end_equity / r.start_equity - 1) * 100.0, "max_drawdown_pct": r.max_dd_pct,
        "trades": len(ts), "win_rate_pct": 100.0 * len(wins) / len(ts) if ts else None,
        "ties": sum(1 for t in ts if is_tie({"net_pnl": t.net_pnl, "equity_at_entry": t.equity_at_entry}, tie_pct)),
        "net_pnl": sum(pnl), "expectancy_r": (sum(pnl) / risk) if risk else None,
        "total_r": sum(t.r or 0.0 for t in ts),
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None,
        "avg_hold_days": held / len(ts) if ts else None, "time_in_market_pct": 100.0 * held / days,
        "long_pnl": sum(t.net_pnl or 0 for t in ts if t.direction > 0),
        "short_pnl": sum(t.net_pnl or 0 for t in ts if t.direction < 0),
        "notional_cap_binding_share": (sum(1 for t in ts if t.capped) / len(ts)) if ts else None,
        "gap_fills": sum(1 for t in ts if t.gap_fill),
        "holding_gap_hours_max": max((h for _, h in r.holding_gaps), default=0.0),
        "holding_gap_hours_total": sum(h for _, h in r.holding_gaps),
        "kills": dict(r.kills), "max_streak_loss_pct": r.max_streak_loss_pct, "by_exit_reason": by_exit,
        "entries_skipped": dict(r.entries_skipped), "kill_log": list(r.kill_log), "holding_gaps": list(r.holding_gaps),
    }


def windows(cfg: Any, last_day: date) -> list[tuple[date, date]]:
    out = []
    start = date.fromisoformat(str(cfg.backtest.first_window_start))
    months = int(cfg.backtest.window_months)
    while True:
        y, m = start.year + (start.month - 1 + months) // 12, (start.month - 1 + months) % 12 + 1
        end = date(y, m, start.day)
        if end > last_day + timedelta(days=1):
            break
        out.append((start, end))
        start = end
    return out


def _median(xs: list[Any]) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _swe(ts: list[BtTrade]) -> float | None:
    """Size-weighted expectancy: sum(pnl) / sum(risk) = risk-weighted mean R."""
    risk = sum(t.risk_usd for t in ts)
    return (sum(t.net_pnl or 0.0 for t in ts) / risk) if risk else None


def _pct(xs: list[float], q: float) -> float | None:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))] if xs else None


def _t_stat(rs: list[float]) -> float | None:
    if len(rs) < 3:
        return None
    sd = statistics.stdev(rs)
    return (statistics.mean(rs) / (sd / math.sqrt(len(rs)))) if sd > 0 else None


def segments(cfg: Any) -> list[tuple[date, date]]:
    return [(date.fromisoformat(str(a)), date.fromisoformat(str(b))) for a, b in cfg.backtest.segments]


def aggregate(runs: list[dict[str, Any]], full_trades: dict[tuple[str, int], list[BtTrade]], variant: str,
              cfg: Any) -> dict[str, Any]:
    win = [r for r in runs if r["variant"] == variant and r["kind"] == "window"]
    full = [r for r in runs if r["variant"] == variant and r["kind"] == "full"]
    offsets = sorted({r["offset_days"] for r in full})
    pos_share = []
    for o in offsets:
        ws = [r for r in win if r["offset_days"] == o]
        pos_share.append(sum(1 for r in ws if r["return_pct"] > 0) / len(ws) if ws else 0.0)
    streaks = [r["max_streak_loss_pct"] for r in win + full]
    caps = [r["notional_cap_binding_share"] for r in full if r["notional_cap_binding_share"] is not None]
    exp_by = {r["offset_days"]: r["expectancy_r"] for r in full}
    segs = segments(cfg)
    seg_exp: dict[str, dict[int, float | None]] = {}
    for a, b in segs:
        key = f"{a.isoformat()}..{b.isoformat()}"
        seg_exp[key] = {o: _swe([t for t in full_trades.get((variant, o), [])
                                  if a.isoformat() <= t.entry_day < b.isoformat()]) for o in offsets}
    seg_median = {k: _median(list(v.values())) for k, v in seg_exp.items()}
    ranked = sorted((o for o in offsets if exp_by.get(o) is not None), key=lambda o: exp_by[o])
    med_off = ranked[len(ranked) // 2] if ranked else None
    t_stat = _t_stat([t.r or 0.0 for t in full_trades.get((variant, med_off), [])]) if med_off is not None else None
    direction: dict[str, dict[str, Any]] = {}
    for o in offsets:
        ts = full_trades.get((variant, o), [])
        direction[str(o)] = {d: {"trades": len([t for t in ts if t.direction == s]),
                                 "expectancy_r": _swe([t for t in ts if t.direction == s])}
                             for d, s in (("long", 1), ("short", -1))}
    short_exps = [direction[str(o)]["short"]["expectancy_r"] for o in offsets]
    rolling: list[float] = []
    n_roll = int(cfg.backtest.rolling_trades)
    for o in offsets:
        ts = full_trades.get((variant, o), [])
        for k in range(0, max(0, len(ts) - n_roll + 1)):
            x = _swe(ts[k:k + n_roll])
            if x is not None:
                rolling.append(x)
    return {
        "variant": variant, "windows": len({r["start"] for r in win if r["offset_days"] == offsets[0]}) if offsets else 0,
        "full_return_pct_by_offset": {r["offset_days"]: r["return_pct"] for r in full},
        "full_expectancy_r_by_offset": exp_by,
        "full_expectancy_r_min": min((x for x in exp_by.values() if x is not None), default=None),
        "full_total_r_by_offset": {r["offset_days"]: r["total_r"] for r in full},
        "full_total_r_min": min((r["total_r"] for r in full), default=None),
        "full_total_r_median": _median([r["total_r"] for r in full]),
        "full_trades_median": _median([r["trades"] for r in full]),
        "full_max_drawdown_pct_max": max((r["max_drawdown_pct"] for r in full), default=None),
        "window_positive_share_by_offset": dict(zip(offsets, pos_share)),
        "window_positive_share_median": _median(pos_share),
        "window_return_pct_median": _median([r["return_pct"] for r in win]),
        "window_max_drawdown_pct_max": max((r["max_drawdown_pct"] for r in win), default=None),
        "floor_hits_total": sum(r["kills"]["equity_floor"] for r in win + full),
        "drawdown_kills_total": sum(r["kills"]["drawdown"] for r in win + full),
        "drawdown_kills_full_median": _median([r["kills"]["drawdown"] for r in full]),
        "losing_streak_kills_total": sum(r["kills"]["losing_streak"] for r in win + full),
        "segment_expectancy_r": seg_exp, "segment_expectancy_r_median": seg_median,
        "segments_positive": sum(1 for x in seg_median.values() if x is not None and x > 0),
        "t_stat_offset": med_off, "t_stat_median_offset": t_stat,
        "direction": direction,
        "short_all_offsets_negative": bool(short_exps) and all(x is not None and x < 0 for x in short_exps),
        "rolling_expectancy_r": {"trades": n_roll, "count": len(rolling), "p5": _pct(rolling, 0.05),
                                 "p10": _pct(rolling, 0.10), "p50": _pct(rolling, 0.50)},
        "rolling_expectancy_r_p5": _pct(rolling, 0.05),
        "streak_loss_pct_p99": _pct(streaks, 0.99),
        "notional_cap_binding_share_median": _median(caps),
        "holding_gap_hours_max": max((r["holding_gap_hours_max"] for r in win + full), default=0.0),
        "holding_gap_hours_total_full_max": max((r["holding_gap_hours_total"] for r in full), default=0.0),
        "gap_fills_full_median": _median([r["gap_fills"] for r in full]),
    }


_OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b}


def evaluate(criteria: dict[str, Any], summaries: dict[str, dict[str, Any]], data: dict[str, Any] | None = None,
             variant: str | None = None) -> list[dict[str, Any]]:
    """Evaluate every rule for `variant` (default: the primary). `scope: data` rules read the data-quality block;
    `use_twin: true` reads the variant's stress twin; `op: report` never passes or fails."""
    target = variant or criteria.get("primary_variant", PRIMARY)
    out = []
    for c in criteria.get("rules", []):
        name = c.get("variant", target)
        if c.get("use_twin"):
            name = VARIANTS[target].twin or target
        if c.get("scope") == "data":
            value: Any = (data or {}).get(c["metric"])
        elif "metric_minus" in c:
            base = summaries.get(name, {}).get(c["metric"])
            other = summaries.get(c["metric_minus"]["variant"], {}).get(c["metric_minus"]["metric"])
            value = None if base is None or other is None else base - other
        else:
            value = summaries.get(name, {}).get(c["metric"])
        report_only = c.get("op") == "report"
        ok = (not report_only) and value is not None and _OPS[c["op"]](value, c["value"])
        out.append({"id": c["id"], "text": c["text"], "value": value, "op": c["op"], "threshold": c.get("value"),
                    "pass": bool(ok), "informational": bool(c.get("informational", False)) or report_only})
    return out


def passes(results: list[dict[str, Any]]) -> bool:
    binding = [x for x in results if not x["informational"]]
    return bool(binding) and all(x["pass"] for x in binding)


def select_variant(criteria: dict[str, Any], summaries: dict[str, dict[str, Any]], data: dict[str, Any]) -> dict[str, Any]:
    """Pre-registered choice (review S5). Live uses A_live. Another candidate may replace it only if it (a) passes
    C0-C7 itself, (b) beats A_live's full-period total R at EVERY start offset, (c) beats A_live in at least two of
    the three segments (median over offsets), (d) its stress twin beats A_live_stress at every offset. If several
    qualify: the highest worst-offset total R, then a committee review. If A_live fails, nothing is approved
    automatically: a qualifying candidate needs a new committee meeting and a shadow forward period."""
    prim = criteria.get("primary_variant", PRIMARY)
    ps, pst = summaries[prim], summaries[VARIANTS[prim].twin or prim]
    primary_pass = passes(evaluate(criteria, summaries, data, prim))
    qualified = []
    detail = {}
    for name, v in VARIANTS.items():
        if v.role != "candidate" or name == prim:
            continue
        s, st = summaries[name], summaries[v.twin or name]
        a = passes(evaluate(criteria, summaries, data, name))
        b = all(s["full_total_r_by_offset"].get(o, -1e18) > ps["full_total_r_by_offset"].get(o, 1e18)
                for o in ps["full_total_r_by_offset"])
        segs_better = sum(1 for k, x in s["segment_expectancy_r_median"].items()
                          if x is not None and ps["segment_expectancy_r_median"].get(k) is not None
                          and x > ps["segment_expectancy_r_median"][k])
        c = segs_better >= 2
        dd = all(st["full_total_r_by_offset"].get(o, -1e18) > pst["full_total_r_by_offset"].get(o, 1e18)
                 for o in pst["full_total_r_by_offset"])
        detail[name] = {"a_passes_c0_c7": a, "b_beats_every_offset": b, "c_segments_better": segs_better,
                        "d_stress_beats_every_offset": dd}
        if a and b and c and dd:
            qualified.append(name)
    qualified.sort(key=lambda n: summaries[n]["full_total_r_min"] or -1e18, reverse=True)
    if primary_pass and not qualified:
        decision, note = prim, f"{prim} passes and no other variant qualifies: live keeps {prim}."
    elif primary_pass:
        decision, note = prim, (f"{prim} passes; {qualified[0]} qualifies under S5 - a review decides "
                                f"whether to switch (live keeps {prim} until then).")
    elif qualified:
        decision, note = None, (f"{prim} FAILS; {qualified[0]} qualifies under S5 - NOT approved automatically: "
                                f"new review plus a shadow forward period.")
    else:
        decision, note = None, f"{prim} FAILS and no variant qualifies: no-go."
    return {"primary_passes": primary_pass, "qualified_replacements": qualified, "live_variant": decision,
            "note": note, "detail": detail}


# ================================================================ lock (review R1)
MANIFEST_CODE = ("backtest.py", "strategy.py", "risk.py", "indicators.py", "shadow.py", "calendar_events.py")


def _sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def manifest(cfg: Any, root: Path, data_q: dict[str, Any], data_end: date, fee_rate: float) -> dict[str, Any]:
    """Everything that decides the result. `backtest confirm` records it; `backtest run` refuses any change."""
    c = cfg.to_dict()
    conf = {k: c[k] for k in ("strategy", "gates", "exits", "risk", "backtest")}
    conf["binance"] = {k: c["binance"][k] for k in ("daily_candles_to_load", "h4_candles_to_load", "funding_days_to_load")}
    conf["shadow"] = {k: c["shadow"][k] for k in ("fee_rate_estimate", "breakeven_trigger_atr")}
    parts = {
        "criteria": _sha_file(root / cfg.backtest.criteria_file),
        "config": hashlib.sha256(json.dumps(conf, sort_keys=True, default=str).encode()).hexdigest(),
        "calendar_history": _sha_file(root / cfg.backtest.calendar_history_file),
        "calendar": _sha_file(root / cfg.gates.calendar_file),
        "version": (root / "VERSION").read_text(encoding="utf-8").strip(),
        "code": {n: _sha_file(root / "perpbot" / n) for n in MANIFEST_CODE},
        "data_end_utc": data_end.isoformat(),
        "data": data_q["slice_sha256"],
        "fee_rate": fee_rate,
    }
    return {"sha256": hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest(), "parts": parts}


def manifest_diff(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    return sorted(k for k in set(a["parts"]) | set(b["parts"]) if a["parts"].get(k) != b["parts"].get(k))


# ================================================================ driver
def criteria_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(cfg: Any, root: Path, data_dir: Path, live_calendar: EventCalendar,
            data_end: date | None = None) -> tuple[Dataset, EventCalendar, date, dict[str, Any]]:
    ds = load_dataset(data_dir)
    cal = merged_calendar(cfg, root, live_calendar)
    end = data_end or data_end_day(ds)
    first = date.fromisoformat(str(cfg.backtest.first_window_start))
    return ds, cal, end, data_quality(cfg, ds, first, end)


def pm_replay(cfg: Any, ds: Dataset, feats: dict[date, DayFeatures | None], fee: float, first: date, end: date,
              equity0: float, pfeats: dict[int, DayFeatures | None] | None = None,
              variant: str = PRIMARY) -> dict[str, Any]:
    """I7: the same decisions, SL/TP and fills on Polymarket's own 1h candles vs Binance, trade by trade."""
    pm = ds.pm_h1
    if not pm:
        return {"available": False, "note": "no Polymarket candles downloaded"}
    start = max(first, datetime.fromtimestamp(pm[0].open_ms / 1000, tz=UTC).date() + timedelta(days=1))
    stop = min(end, datetime.fromtimestamp(pm[-1].open_ms / 1000, tz=UTC).date())
    if (stop - start).days < int(cfg.backtest.pm_replay_min_days):
        return {"available": False, "note": f"Polymarket candles cover only {(stop - start).days} days",
                "range": [start.isoformat(), stop.isoformat()]}
    bn = Simulator(cfg, ds.h1, ds.funding, feats, fee, pfeats).run(variant, start, stop, 0, equity0)
    pmr = Simulator(cfg, pm, ds.funding, feats, fee, pfeats).run(variant, start, stop, 0, equity0)
    by_entry = {(t.entry_day, t.direction): t for t in pmr.trades}
    matched = [(t, by_entry.get((t.entry_day, t.direction))) for t in bn.trades]
    same = sum(1 for a, b in matched if b is not None and a.exit_reason == b.exit_reason
               and datetime.fromtimestamp(int(a.exit_ts or 0) / 1000, tz=UTC).date()
               == datetime.fromtimestamp(int(b.exit_ts or 0) / 1000, tz=UTC).date())
    return {"available": True, "range": [start.isoformat(), stop.isoformat()], "binance_trades": len(bn.trades),
            "polymarket_trades": len(pmr.trades), "exit_agreement": (same / len(bn.trades)) if bn.trades else None,
            "binance_expectancy_r": _swe(bn.trades), "polymarket_expectancy_r": _swe(pmr.trades)}


def run_backtest(cfg: Any, root: Path, data_dir: Path, out_dir: Path, live_calendar: EventCalendar, fee_rate: float,
                 progress: Any = print, data_end: date | None = None, run_number: int = 1,
                 manifest_sha: str | None = None) -> dict[str, Any]:
    import yaml

    ds, cal, end, dq = prepare(cfg, root, data_dir, live_calendar, data_end)
    wins = windows(cfg, end - timedelta(days=1))
    if not wins:
        raise BacktestError("not enough data for one window: download more history")
    offsets = [int(x) for x in cfg.backtest.start_offsets_days]
    first, final = wins[0][0], wins[-1][1]
    di = DayInputs(cfg, ds, cal)
    progress(f"computing daily decisions {first} .. {final} ...")
    feats: dict[date, DayFeatures | None] = {}
    d = first
    while d < final:
        feats[d] = di.features(d)
        d += timedelta(days=1)
    missing = [k.isoformat() for k, v in feats.items() if v is None]
    pfeats: dict[int, DayFeatures | None] = {}
    if any(v.cadence == "rolling_4h" for v in VARIANTS.values()):
        progress("computing 4-hour decisions (rolling_4h) ...")
        pi = PeriodInputs(cfg, ds, cal)
        t = day_start_ms(first)
        while t < day_start_ms(final):
            pfeats[t] = pi.features(t)
            t += H4_MS
    missing_periods = [period_key(k) for k, v in pfeats.items() if v is None]
    h1 = [c for c in ds.h1 if c.open_ms < day_start_ms(end)]
    sim = Simulator(cfg, h1, ds.funding, feats, fee_rate, pfeats)
    eq0 = float(cfg.backtest.start_equity_usd)
    tie = float(cfg.risk.losing_streak_tie_pct)
    runs: list[dict[str, Any]] = []
    full_trades: dict[tuple[str, int], list[BtTrade]] = {}
    for name in VARIANTS:
        progress(f"variant {name} ...")
        for ws, we in wins:
            for o in offsets:
                r = sim.run(name, ws + timedelta(days=o), we, o, eq0, ramp=False)
                runs.append(dict(run_metrics(r, tie), kind="window"))
        for o in offsets:
            r = sim.run(name, first + timedelta(days=o), final, o, eq0, ramp=True)
            runs.append(dict(run_metrics(r, tie), kind="full"))
            full_trades[(name, o)] = r.trades
    summaries = {v: aggregate(runs, full_trades, v, cfg) for v in VARIANTS}
    crit_path = root / cfg.backtest.criteria_file
    criteria = yaml.safe_load(crit_path.read_text(encoding="utf-8"))
    pm = pm_replay(cfg, ds, feats, fee_rate, first, final, eq0, pfeats)
    data_metrics = {"h1_missing_share": dq["h1"]["missing_share"], "h4_missing_share": dq["h4"]["missing_share"],
                    "d1_missing_share": dq["d1"]["missing_share"], "pm_exit_agreement": pm.get("exit_agreement"),
                    "funding_gaps_over_8h": len(dq["funding_gaps_over_8h"])}
    results = evaluate(criteria, summaries, data_metrics)
    selection = select_variant(criteria, summaries, data_metrics)
    report = {
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config_version": cfg.config_version, "criteria_version": criteria.get("criteria_version"),
        "criteria_sha256": criteria_hash(crit_path), "calendar_version": cal.version,
        "manifest_sha256": manifest_sha, "run_number_under_confirmation": run_number,
        "data_end_utc": end.isoformat(), "data_quality": dq, "polymarket_replay": pm,
        "windows": [[a.isoformat(), b.isoformat()] for a, b in wins], "offsets_days": offsets,
        "days_without_decision": missing, "periods_without_decision": missing_periods[:500],
        "periods_without_decision_count": len(missing_periods), "fee_rate": fee_rate,
        "primary_variant": criteria.get("primary_variant", PRIMARY), "config_cadence": str(cfg.strategy.cadence),
        "summaries": summaries, "criteria": results, "selection": selection,
        "verdict": "PASS" if passes(results) else "FAIL",
    }
    digest = hashlib.sha256(json.dumps({k: v for k, v in report.items()
                                        if k not in ("generated_utc", "run_number_under_confirmation")},
                                       sort_keys=True, default=str).encode()).hexdigest()
    report["result_sha256"] = digest
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    with (out_dir / "runs.csv").open("w", newline="", encoding="utf-8") as fh:
        nested = ("kills", "by_exit_reason", "entries_skipped", "kill_log", "holding_gaps")
        cols = [k for k in runs[0] if k not in nested] + list(nested)
        w = csv.writer(fh)
        w.writerow(cols)
        for r in runs:
            w.writerow([json.dumps(r[c]) if c in nested else r[c] for c in cols])
    for name, v in VARIANTS.items():
        if v.role == "stress":
            continue
        ts = full_trades[(name, offsets[0])]
        with (out_dir / f"trades_{name}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            cols = list(asdict(ts[0]).keys()) if ts else ["none"]
            w.writerow(cols)
            for t in ts:
                w.writerow([json.dumps(x) if isinstance(x, list) else x for x in asdict(t).values()])
    (out_dir / "summary.md").write_text(summary_md(report), encoding="utf-8")
    return report


def summary_md(rep: dict[str, Any]) -> str:
    def f(x: Any, d: int = 2) -> str:
        return "-" if x is None else (f"{x:.{d}f}" if isinstance(x, float) else str(x))

    dq = rep["data_quality"]
    prim = rep.get("primary_variant", PRIMARY)
    L = [f"# btcperp backtest - verdict: **{rep['verdict']}** ({prim})", "",
         f"**Run #{rep['run_number_under_confirmation']} under this confirmation** (the committee uses run #1). "
         f"manifest {str(rep['manifest_sha256'])[:12]}, result sha256 {rep['result_sha256'][:16]}",
         f"config {rep['config_version']} / criteria {rep['criteria_version']} / calendar {rep['calendar_version']} / "
         f"fee {rep['fee_rate']} / data to {rep['data_end_utc']} (exclusive)",
         f"windows: {len(rep['windows'])} x offsets {rep['offsets_days']}; days without a decision: "
         f"{len(rep['days_without_decision'])}; 4-hour periods without a decision: "
         f"{rep.get('periods_without_decision_count', 0)}", "",
         f"Config strategy.cadence: {rep.get('config_cadence')} (primary variant {prim}). `R4h_*` = rolling_4h "
         f"(decide every 4 h on daily candles ending then); the others decide once a day at 08:30 HKT (v1.4).", "",
         f"**Variant choice (pre-registered, S5):** {rep['selection']['note']}", "",
         "## Criteria (confirmed before the run)", "", "| id | criterion | value | threshold | result |",
         "|---|---|---|---|---|"]
    for c in rep["criteria"]:
        res = "info" if c["informational"] else ("PASS" if c["pass"] else "FAIL")
        L.append(f"| {c['id']} | {c['text']} | {f(c['value'], 4)} | {c['op']} {c['threshold'] if c['threshold'] is not None else ''} | {res} |")
    L += ["", "## Data quality", "", "| series | expected | missing | share | gaps | longest gap h |", "|---|---|---|---|---|---|"]
    for k in ("h1", "h4", "d1"):
        q = dq[k]
        L.append(f"| {k} | {q['expected']} | {q['missing']} | {f(q['missing_share'], 5)} | {q['gap_count']} | {f(q['max_gap_hours'], 1)} |")
    L.append(f"\nFunding gaps over 8 h: {len(dq['funding_gaps_over_8h'])} {dq['funding_gaps_over_8h'][:20]}")
    L.append(f"Slice SHA-256: {json.dumps(dq['slice_sha256'])}")
    L += ["", "## Variants", "",
          "| variant | full return % (off 0) | exp R min | total R min | trades med | win+ share | win ret med % | "
          "worst win DD % | full DD % max | DD kills (full med) | streak kills | floor | segments >0 | t | roll30 p5 |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for v, s in rep["summaries"].items():
        L.append(f"| {v} | {f(s['full_return_pct_by_offset'].get(0))} | {f(s['full_expectancy_r_min'], 3)} | "
                 f"{f(s['full_total_r_min'], 1)} | {f(s['full_trades_median'], 0)} | {f(s['window_positive_share_median'])} | "
                 f"{f(s['window_return_pct_median'])} | {f(s['window_max_drawdown_pct_max'])} | "
                 f"{f(s['full_max_drawdown_pct_max'])} | {f(s['drawdown_kills_full_median'], 0)} | "
                 f"{s['losing_streak_kills_total']} | {s['floor_hits_total']} | {s['segments_positive']} | "
                 f"{f(s['t_stat_median_offset'])} | {f(s['rolling_expectancy_r_p5'], 3)} |")
    a = rep["summaries"].get(prim, {})
    L += ["", f"## {prim} by direction (I5) and segment", "", "```", json.dumps(a.get("direction"), indent=1),
          json.dumps(a.get("segment_expectancy_r"), indent=1, default=str), "```",
          f"Short expectancy negative at every offset: {a.get('short_all_offsets_negative')} (if true: committee discussion).",
          "", f"Polymarket replay (I7): {json.dumps(rep['polymarket_replay'], default=str)}", "",
          "`*_stress` = double fees, 30 bps exit slippage, double paid funding. `R4h_confirm` flips only after two "
          "opposite 4-hour periods. `R4h_live_noevents` ignores the event gate; `R4h_live_nokill` runs without kill "
          "switches (untruncated streaks, BT4). Details: summary.json, runs.csv, trades_<variant>.csv (full period, "
          "first offset)."]
    return "\n".join(L) + "\n"
