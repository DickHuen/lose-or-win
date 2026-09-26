"""Backtest (review B3 / v1.2.0 item 1). Runs on the owner's computer from downloaded Binance data.

Design (pre-registered; see BACKTEST.md):
- Decisions use exactly the live code path: `strategy.day_features` + `strategy.plan_for`, fed with what the
  live `decide` would see at 00:30 UTC (08:30 HKT) of UTC day D: the last `binance.daily_candles_to_load`
  closed daily candles, the last `binance.h4_candles_to_load` closed 4h candles, Binance funding from
  `binance.funding_days_to_load` days before D, and the economic-calendar windows active at 00:30 UTC
  (config/calendar_history.yaml + config/calendar.yaml). Strategy values: this config, unchanged.
- Execution: a decision's close / entry fills at the close of the 00:00-01:00 UTC 1h candle (30 minutes after
  the decision), with `exits.entry_slippage_bps` on entries and `backtest.exit_slippage_bps` on every exit.
  Bracket SL/TP (mark-triggered in live) are checked on Binance 1h candles from the fill on; a candle that
  touches both counts as the SL (conservative). SL/TP exits fill at the trigger price minus exit slippage.
- Costs: exactly `shadow._r` (fees both sides at the configured taker rate, Binance funding over the holding
  time, positive rate = longs pay), plus the exit slippage above.
- Sizing: `risk.compute_size` (risk % x tier fraction, ramp for the first trades of each run, notional cap,
  leverage cap) on mark-to-market equity; the notional-cap binding rate is reported.
- Kill switches, checked once a day at the fill time on mark-to-market equity and after every closed trade:
  drawdown (close, pause `backtest.kill_pause_days`, then resume with the peak reset), losing streak with the
  D11 tie rule (no new entries for the pause, position kept), equity floor on the run's starting equity
  (close, stop for the rest of the run). Live runs 7 checks a day; the daily check is slightly slower.
- Windows: consecutive `backtest.window_months`-month windows from `backtest.first_window_start`, each run
  independently from `backtest.start_equity_usd`, and started `start_offsets_days` later; plus one chained
  full-period run per offset. Nothing is fitted: every threshold comes from data before the decision.
"""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from perpbot.calendar_events import EventCalendar, parse_calendar
from perpbot.exchange.base import Instrument
from perpbot.indicators import Candle
from perpbot.risk import compute_size, is_tie, losing_streak, risk_pct_for_trade
from perpbot.shadow import SimTrade, _r, _slipped
from perpbot.strategy import DayFeatures, InsufficientData, day_features, plan_for
from perpbot.timeutil import DAY_MS, HOUR_MS, MINUTE_MS, day_start_ms, fmt_utc

UTC = timezone.utc
INTERVALS = {"1d": DAY_MS, "4h": 4 * HOUR_MS, "1h": HOUR_MS}
DECISION_OFFSET_MS = 30 * MINUTE_MS          # 00:30 UTC = 08:30 HKT, the live decide time
FILL_OFFSET_MS = HOUR_MS                     # close of the 00:00-01:00 UTC candle

# name: (breakeven move, control = |score| below tier_low_max means flat, crowded funding closes, event gate on)
VARIANTS: dict[str, tuple[bool, bool, bool, bool]] = {
    "A_live": (False, False, True, True),
    "B_breakeven": (True, False, True, True),
    "C_control": (False, True, True, True),
    "A_live_fhold": (False, False, False, True),
    "B_breakeven_fhold": (True, False, False, True),
    "C_control_fhold": (False, True, False, True),
    "A_live_noevents": (False, False, True, False),      # sensitivity only: bounds calendar errors
}
PRIMARY = "A_live"


class BacktestError(Exception):
    pass


# ================================================================ data
@dataclass
class Dataset:
    daily: list[Candle]
    h4: list[Candle]
    h1: list[Candle]
    funding: list[tuple[int, float, float]]

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
                 _read_candles(_csv_path(d, "1h"), HOUR_MS), _read_funding(_csv_path(d, "funding")))
    if not (ds.daily and ds.h4 and ds.h1 and ds.funding):
        raise BacktestError(f"no downloaded data in {d}: run `backtest download` first")
    return ds


def download(bn: Any, d: Path, start: date, now_ms: int) -> dict[str, int]:
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
    return counts


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


def _synthetic_instrument(cfg: Any) -> Instrument:
    return Instrument(0, "BTC-BACKTEST", "BTC", "USD", "crypto", int(cfg.backtest.quantity_decimals), 2, 0.0, 0.0, 0.0,
                      int(cfg.risk.leverage), True, 0.0, 0, "8h")


class Simulator:
    def __init__(self, cfg: Any, ds: Dataset, feats: dict[date, DayFeatures | None], fee_rate: float) -> None:
        self.cfg, self.ds, self.feats, self.fee = cfg, ds, feats, fee_rate
        self.h1_open = [c.open_ms for c in ds.h1]
        self.funding = [(ts, r) for ts, r, _ in ds.funding]
        self.inst = _synthetic_instrument(cfg)

    def _candle_close(self, open_ms: int) -> float | None:
        i = bisect_right(self.h1_open, open_ms) - 1
        return self.ds.h1[i].close if i >= 0 and self.ds.h1[i].open_ms == open_ms else None

    def _exit_px(self, price: float, direction: int) -> float:
        return price * (1 - direction * float(self.cfg.backtest.exit_slippage_bps) / 1e4)

    def _book(self, t: BtTrade, price: float, ts: int, reason: str, res: RunResult, eq: list[float]) -> None:
        t.exit_price, t.exit_ts, t.exit_reason = self._exit_px(price, t.direction), ts, reason
        risk_unit = float(self.cfg.exits.sl_atr_multiple) * t.atr
        st = SimTrade("bt", t.entry_day, t.direction, t.qty * risk_unit, t.entry_price, t.entry_ts, t.atr, t.sl, t.tp,
                      exit_price=t.exit_price, exit_ts_ms=ts)
        t.net_pnl = _r(st, float(self.cfg.exits.sl_atr_multiple), self.fee, self.funding)
        t.r = t.net_pnl / t.risk_usd if t.risk_usd else 0.0
        eq[0] += t.net_pnl
        res.trades.append(t)

    def _walk(self, t: BtTrade, from_ms: int, to_ms: int, v2: bool) -> tuple[float, int, str] | None:
        """Bracket SL/TP on 1h candles with open time in [from_ms, to_ms)."""
        be_atr = float(self.cfg.shadow.breakeven_trigger_atr)
        i = bisect_right(self.h1_open, from_ms - 1)
        while i < len(self.ds.h1) and self.ds.h1[i].open_ms < to_ms:
            c = self.ds.h1[i]
            i += 1
            sl_hit = c.low <= t.sl if t.direction > 0 else c.high >= t.sl
            tp_hit = c.high >= t.tp if t.direction > 0 else c.low <= t.tp
            if sl_hit:
                return t.sl, c.close_ms, "BE" if t.be_moved else "SL"
            if tp_hit:
                return t.tp, c.close_ms, "TP"
            if v2 and not t.be_moved:
                fav = c.high - t.entry_price if t.direction > 0 else t.entry_price - c.low
                if fav >= be_atr * t.atr:
                    t.sl, t.be_moved = t.entry_price, True
        return None

    def run(self, variant: str, start: date, end: date, offset: int, equity0: float) -> RunResult:
        cfg = self.cfg
        v2, control, fclose, events_on = VARIANTS[variant]
        s, rk = cfg.strategy, cfg.risk
        res = RunResult(variant, start.isoformat(), end.isoformat(), offset, equity0, equity0)
        eq = [equity0]                                  # realised equity (mutable cell)
        peak = equity0
        trade: BtTrade | None = None
        pause_until: date | None = None
        pause_reason: str | None = None
        stopped = False
        since_resume: list[BtTrade] = []
        opened = 0
        last_ms = day_start_ms(start)
        pause_days = int(cfg.backtest.kill_pause_days)

        def after_close(t: BtTrade, day: date) -> None:
            nonlocal pause_until, pause_reason
            since_resume.append(t)
            st = losing_streak([{"net_pnl": x.net_pnl, "equity_at_entry": x.equity_at_entry} for x in reversed(since_resume)],
                               float(rk.kill_losing_streak_pct), float(rk.losing_streak_tie_pct))
            res.max_streak_loss_pct = max(res.max_streak_loss_pct, st.loss_pct)
            if st.triggered and pause_reason is None:
                res.kills["losing_streak"] += 1
                res.kill_log.append((day.isoformat(), "losing_streak"))
                pause_until, pause_reason = day + timedelta(days=pause_days), "kill_losing_streak"

        d = start
        while d < end:
            fill_ms = day_start_ms(d) + FILL_OFFSET_MS
            if trade is not None:
                hit = self._walk(trade, last_ms, fill_ms, v2)
                if hit is not None:
                    px, ts, why = hit
                    self._book(trade, px, ts, why, res, eq)
                    after_close(trade, d)
                    trade = None
            last_ms = fill_ms
            mark = self._candle_close(day_start_ms(d))
            if mark is None:
                d += timedelta(days=1)
                continue
            mtm = eq[0] + (trade.qty * (mark - trade.entry_price) * trade.direction if trade else 0.0)
            # ---- resume after the kill pause (peak reset, streak restarted)
            if pause_until is not None and d >= pause_until and not stopped:
                pause_until, pause_reason = None, None
                peak = mtm
                since_resume.clear()
            peak = max(peak, mtm)
            # ---- kill switches
            if not stopped and mtm < equity0 * float(rk.equity_floor_pct_of_net_funded) / 100.0:
                res.kills["equity_floor"] += 1
                res.kill_log.append((d.isoformat(), "equity_floor"))
                stopped = True
                if trade is not None:
                    self._book(trade, mark, fill_ms, "kill_switch", res, eq)
                    trade = None
            elif not stopped and pause_reason != "kill_drawdown" and peak > 0 and \
                    (peak - mtm) / peak * 100.0 >= float(rk.kill_drawdown_pct):
                res.kills["drawdown"] += 1
                res.kill_log.append((d.isoformat(), "drawdown"))
                pause_until, pause_reason = d + timedelta(days=pause_days), "kill_drawdown"
                if trade is not None:
                    self._book(trade, mark, fill_ms, "kill_switch", res, eq)
                    trade = None
            f = self.feats.get(d)
            if f is not None and not stopped:
                pos_dir = trade.direction if trade else 0
                entry_day = date.fromisoformat(trade.entry_day) if trade else d
                plan, _ = plan_for(f, cfg, position_dir=pos_dir, entry_day=entry_day, entered_today=False,
                                   paused_reason=pause_reason, funding_rule_closes=fclose, ignore_events=not events_on)
                close_reason, enter_dir, frac = plan.close_reason, plan.enter_direction, plan.enter_fraction
                if control and plan.action != "paused" and f.score.abs_score < float(s.tier_low_max):
                    enter_dir, frac = 0, 0.0
                    if trade is not None and close_reason is None:
                        close_reason = "flat_rule"
                if trade is not None and close_reason:
                    self._book(trade, mark, fill_ms, close_reason, res, eq)
                    after_close(trade, d)
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
                        trade = BtTrade(d.isoformat(), enter_dir, qty, price, fill_ms, atr, price - enter_dir * sl_d,
                                        price + enter_dir * tp_d, frac, qty * sl_d, eq[0],
                                        any("notional" in c for c in size.capped_by), f.score.score, f.gates_triggered())
                        opened += 1
            mtm = eq[0] + (trade.qty * (mark - trade.entry_price) * trade.direction if trade else 0.0)
            res.equity_curve.append((d.isoformat(), mtm))
            d += timedelta(days=1)
        if trade is not None:
            end_ms = day_start_ms(end)
            hit = self._walk(trade, last_ms, end_ms, v2)
            if hit is not None:
                self._book(trade, hit[0], hit[1], hit[2], res, eq)
            else:
                px = self._candle_close(end_ms - HOUR_MS) or trade.entry_price
                self._book(trade, px, end_ms, "end_of_run", res, eq)
        res.end_equity = eq[0]
        return res


# ================================================================ metrics
def run_metrics(r: RunResult, tie_pct: float) -> dict[str, Any]:
    ts = r.trades
    pnl = [t.net_pnl or 0.0 for t in ts]
    risk = sum(t.risk_usd for t in ts)
    wins = [x for x in pnl if x > 0]
    losses = [x for x in pnl if x <= 0]
    curve = [v for _, v in r.equity_curve] or [r.start_equity]
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak * 100.0 if peak > 0 else 0.0)
    days = max(1, len(r.equity_curve))
    held = sum(((t.exit_ts or t.entry_ts) - t.entry_ts) / DAY_MS for t in ts)
    by_exit: dict[str, int] = {}
    for t in ts:
        by_exit[t.exit_reason or "?"] = by_exit.get(t.exit_reason or "?", 0) + 1
    return {
        "variant": r.variant, "start": r.start, "end": r.end, "offset_days": r.offset_days,
        "start_equity": r.start_equity, "end_equity": r.end_equity,
        "return_pct": (r.end_equity / r.start_equity - 1) * 100.0, "max_drawdown_pct": mdd,
        "trades": len(ts), "win_rate_pct": 100.0 * len(wins) / len(ts) if ts else None,
        "ties": sum(1 for t in ts if is_tie({"net_pnl": t.net_pnl, "equity_at_entry": t.equity_at_entry}, tie_pct)),
        "net_pnl": sum(pnl), "expectancy_r": (sum(pnl) / risk) if risk else None,
        "total_r": sum(t.r or 0.0 for t in ts),
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None,
        "avg_hold_days": held / len(ts) if ts else None, "time_in_market_pct": 100.0 * held / days,
        "long_pnl": sum(t.net_pnl or 0 for t in ts if t.direction > 0),
        "short_pnl": sum(t.net_pnl or 0 for t in ts if t.direction < 0),
        "notional_cap_binding_share": (sum(1 for t in ts if t.capped) / len(ts)) if ts else None,
        "kills": dict(r.kills), "max_streak_loss_pct": r.max_streak_loss_pct, "by_exit_reason": by_exit,
        "entries_skipped": dict(r.entries_skipped), "kill_log": list(r.kill_log),
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


def _median(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def aggregate(runs: list[dict[str, Any]], variant: str) -> dict[str, Any]:
    win = [r for r in runs if r["variant"] == variant and r["kind"] == "window"]
    full = [r for r in runs if r["variant"] == variant and r["kind"] == "full"]
    offsets = sorted({r["offset_days"] for r in win})
    pos_share = []
    for o in offsets:
        ws = [r for r in win if r["offset_days"] == o]
        pos_share.append(sum(1 for r in ws if r["return_pct"] > 0) / len(ws) if ws else 0.0)
    streaks = sorted(r["max_streak_loss_pct"] for r in win + full)
    p99 = streaks[min(len(streaks) - 1, int(round(0.99 * (len(streaks) - 1))))] if streaks else None
    caps = [r["notional_cap_binding_share"] for r in full if r["notional_cap_binding_share"] is not None]
    return {
        "variant": variant, "windows": len({(r["start"]) for r in win if r["offset_days"] == 0}),
        "full_return_pct_by_offset": {r["offset_days"]: r["return_pct"] for r in full},
        "full_expectancy_r_by_offset": {r["offset_days"]: r["expectancy_r"] for r in full},
        "full_expectancy_r_min": min((r["expectancy_r"] for r in full if r["expectancy_r"] is not None), default=None),
        "full_total_r_median": _median([r["total_r"] for r in full]),
        "full_trades_median": _median([r["trades"] for r in full]),
        "full_max_drawdown_pct_max": max((r["max_drawdown_pct"] for r in full), default=None),
        "window_positive_share_by_offset": dict(zip(offsets, pos_share)),
        "window_positive_share_median": _median(pos_share),
        "window_return_pct_median": _median([r["return_pct"] for r in win]),
        "window_max_drawdown_pct_max": max((r["max_drawdown_pct"] for r in win), default=None),
        "floor_hits_total": sum(r["kills"]["equity_floor"] for r in win + full),
        "drawdown_kills_total": sum(r["kills"]["drawdown"] for r in win + full),
        "losing_streak_kills_total": sum(r["kills"]["losing_streak"] for r in win + full),
        "streak_loss_pct_p99": p99,
        "notional_cap_binding_share_median": _median(caps),
    }


def evaluate(criteria: dict[str, Any], summaries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    ops = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
           "==": lambda a, b: a == b}
    out = []
    for c in criteria.get("rules", []):
        v = summaries.get(c.get("variant", criteria.get("primary_variant", PRIMARY)), {})
        if "metric_minus" in c:
            other = summaries.get(c["metric_minus"]["variant"], {}).get(c["metric_minus"]["metric"])
            value = None if v.get(c["metric"]) is None or other is None else v[c["metric"]] - other
        else:
            value = v.get(c["metric"])
        ok = value is not None and ops[c["op"]](value, c["value"])
        out.append({"id": c["id"], "text": c["text"], "value": value, "op": c["op"], "threshold": c["value"],
                    "pass": bool(ok), "informational": bool(c.get("informational", False))})
    return out


# ================================================================ driver
def criteria_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_backtest(cfg: Any, root: Path, data_dir: Path, out_dir: Path, live_calendar: EventCalendar, fee_rate: float,
                 progress: Any = print) -> dict[str, Any]:
    import yaml

    ds = load_dataset(data_dir)
    cal = merged_calendar(cfg, root, live_calendar)
    last = ds.last_day()
    wins = windows(cfg, last)
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
    sim = Simulator(cfg, ds, feats, fee_rate)
    eq0 = float(cfg.backtest.start_equity_usd)
    tie = float(cfg.risk.losing_streak_tie_pct)
    runs: list[dict[str, Any]] = []
    trades_out: dict[str, list[BtTrade]] = {}
    for v in VARIANTS:
        progress(f"variant {v} ...")
        for ws, we in wins:
            for o in offsets:
                r = sim.run(v, ws + timedelta(days=o), we, o, eq0)
                runs.append(dict(run_metrics(r, tie), kind="window"))
        for o in offsets:
            r = sim.run(v, first + timedelta(days=o), final, o, eq0)
            runs.append(dict(run_metrics(r, tie), kind="full"))
            if o == 0:
                trades_out[v] = r.trades
    summaries = {v: aggregate(runs, v) for v in VARIANTS}
    crit_path = root / cfg.backtest.criteria_file
    criteria = yaml.safe_load(crit_path.read_text(encoding="utf-8"))
    results = evaluate(criteria, summaries)
    binding = [x for x in results if not x["informational"]]
    report = {
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config_version": cfg.config_version, "criteria_version": criteria.get("criteria_version"),
        "criteria_sha256": criteria_hash(crit_path), "calendar_version": cal.version,
        "data": {"daily": [ds.daily[0].open_ms, ds.daily[-1].open_ms], "h1_last_day": last.isoformat(),
                 "funding_records": len(ds.funding)},
        "windows": [[a.isoformat(), b.isoformat()] for a, b in wins], "offsets_days": offsets,
        "days_without_decision": missing, "fee_rate": fee_rate,
        "summaries": summaries, "criteria": results,
        "verdict": "PASS" if binding and all(x["pass"] for x in binding) else "FAIL",
    }
    digest = hashlib.sha256(json.dumps({k: v for k, v in report.items() if k != "generated_utc"}, sort_keys=True,
                                       default=str).encode()).hexdigest()
    report["result_sha256"] = digest
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    with (out_dir / "runs.csv").open("w", newline="", encoding="utf-8") as fh:
        nested = ("kills", "by_exit_reason", "entries_skipped", "kill_log")
        cols = [k for k in runs[0] if k not in nested] + list(nested)
        w = csv.writer(fh)
        w.writerow(cols)
        for r in runs:
            w.writerow([json.dumps(r[c]) if c in nested else r[c] for c in cols])
    for v, ts in trades_out.items():
        with (out_dir / f"trades_{v}.csv").open("w", newline="", encoding="utf-8") as fh:
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

    L = [f"# btcperp backtest - verdict: **{rep['verdict']}**", "",
         f"config {rep['config_version']} / criteria {rep['criteria_version']} (sha256 {rep['criteria_sha256'][:12]}) / "
         f"calendar {rep['calendar_version']}", f"windows: {len(rep['windows'])} x offsets {rep['offsets_days']}; "
         f"days without a decision (missing data): {len(rep['days_without_decision'])}", f"result sha256: {rep['result_sha256']}",
         "", "## Criteria (written and confirmed before the run)", "",
         "| id | criterion | value | threshold | result |", "|---|---|---|---|---|"]
    for c in rep["criteria"]:
        res = ("info" if c["informational"] else "PASS") if c["pass"] else ("info" if c["informational"] else "FAIL")
        L.append(f"| {c['id']} | {c['text']} | {f(c['value'])} | {c['op']} {c['threshold']} | {res} |")
    L += ["", "## Variants", "",
          "| variant | full return % (offset 0) | full expectancy R (min) | trades (median) | window positive share | "
          "worst window DD % | DD kills | streak kills | floor hits | streak loss p99 % | cap binding |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for v, s in rep["summaries"].items():
        L.append(f"| {v} | {f(s['full_return_pct_by_offset'].get(0))} | {f(s['full_expectancy_r_min'], 3)} | "
                 f"{f(s['full_trades_median'], 0)} | {f(s['window_positive_share_median'])} | "
                 f"{f(s['window_max_drawdown_pct_max'])} | {s['drawdown_kills_total']} | {s['losing_streak_kills_total']} | "
                 f"{s['floor_hits_total']} | {f(s['streak_loss_pct_p99'])} | {f(s['notional_cap_binding_share_median'])} |")
    L += ["", "Variant A_live_noevents ignores the event gate: it only shows how much the (unverified) historical "
          "calendar can matter. Details: summary.json, runs.csv (every run), trades_<variant>.csv (full period, offset 0)."]
    return "\n".join(L) + "\n"
