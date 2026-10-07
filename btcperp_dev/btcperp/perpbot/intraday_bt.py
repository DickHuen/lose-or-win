"""v2.0.0 intraday backtest (owner 2026-10-06: "回測與實盤共用訊號、倉位計算、逐筆槓桿、止盈止損及費用邏輯。處理 :30／:50
決策時序、數量取整及清算距離。缺少細周期成交資料時明確標示近似，不能宣稱已精確還原實盤").

Uses exactly the live functions: intraday.evaluate (signal), intraday.position_size / split_legs (owner's position map,
per-trade leverage with the volatility cut, quantity rounded down to 0.00001 BTC, $10 minimum), intraday.exit_signal /
next_stop (invalidation, time, no-progress, break-even, trail), costs.CostEstimate (gate), candles.check (data gaps),
the same event blackout. Timing like live: one run 1 minute after each 15-minute close (decisions see only candles
closed by then); market orders fill at the open of the next 5-minute candle; exchange stops / targets act between
runs on the 5-minute path. It is an APPROXIMATION of live trading - see APPROXIMATIONS, printed in every report.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

from perpbot import costs
from perpbot import intraday as idy
from perpbot.candles import check
from perpbot.exchange.base import Instrument
from perpbot.indicators import Candle
from perpbot.risk import estimate_liquidation, liquidation_ok, quantize_qty
from perpbot.strategy import tier_fraction

M5 = 300_000
M15 = idy.M15_MS
H1 = idy.H1_MS
DAY = 86_400_000
RUN_DELAY_MS = 60_000          # the live task runs at HH:01 / :16 / :31 / :46, one minute after the close

# Cost scenarios, basis points PER SIDE (fee, slippage) - the same four as the Codex report of 2026-10-06.
SCENARIOS = {"zero": (0.0, 0.0), "optimistic": (2.0, 1.0), "base": (4.0, 5.0), "stress": (8.0, 15.0)}
SIZINGS = ("owner", "risk3")    # owner = config position map x3..x20; risk3 = 3% risk at the stop, position <= 10x

APPROXIMATIONS = [
    "Prices are Binance BTCUSDT spot, not Polymarket BTC-USD: the basis, Polymarket's own book depth, its stop "
    "triggers (on Polymarket's mark) and its funding differ.",
    "Inside a 5-minute candle the order of high and low is unknown: if a stop and a target fall in the same candle, "
    "the STOP is assumed first; a candle opening beyond the stop fills at its open.",
    "Fills: market orders at the open of the 5-minute candle after the run, plus the scenario's slippage; the live FOK "
    "limit (10 bps) can leave an order unfilled, which the backtest never does. No rejections, latency, outages, "
    "missed runs or manual actions are simulated.",
    "Funding uses Binance USD-M 8-hour funding / 8 per hour as a proxy for Polymarket's hourly funding.",
    "Without 5-minute candles the path falls back to 15-minute candles (flagged as path_resolution 15m): stops and "
    "targets are then judged even more coarsely.",
    "The exchange liquidation is approximated with the bot's estimate (1 / leverage - maintenance margin); only a "
    "candle opening beyond it counts as a liquidation.",
]

BTC_USD = dict(id=0, symbol="BTC-USD", base_asset="BTC", quote_asset="USD", category="crypto", quantity_decimals=5,
               price_decimals=1, min_notional=10.0, max_market_notional=1_000_000.0, max_limit_notional=5_000_000.0,
               max_leverage=50, isolated_only=False, price_bounds=0.02, max_order_count=200, funding_interval="1h",
               risk_tiers=[(0.0, 50), (250_000.0, 25), (1_000_000.0, 20)])


class BacktestError(Exception):
    pass


def instrument() -> Instrument:
    return Instrument(**BTC_USD)


# ---------------------------------------------------------------- data
@dataclass
class Data:
    m5: list[Candle]
    m15: list[Candle]
    h1: list[Candle]
    h4: list[Candle]
    funding: list[tuple[int, float]]          # (time, 8h rate)
    releases: list[int] = field(default_factory=list)


FILES = {"5m": "klines_5m.csv", "15m": "klines_15m.csv", "1h": "klines_1h.csv", "4h": "klines_4h.csv"}
STEP = {"5m": M5, "15m": M15, "1h": H1, "4h": 4 * H1}


def _write(path: Path, bars: Sequence[Candle]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["open_ms", "open", "high", "low", "close", "volume"])
        for c in bars:
            w.writerow([c.open_ms, c.open, c.high, c.low, c.close, c.volume])


def _read(path: Path, step: int) -> list[Candle]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            o = int(r["open_ms"])
            out.append(Candle(o, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                              float(r["volume"] or 0.0), o + step))
    return out


def download(bn: Any, out_dir: Path, start_ms: int, end_ms: int, warmup_days: int = 15) -> dict[str, Any]:
    """Binance candles 5m / 15m / 1h / 4h and USD-M funding from start - warmup to end (closed candles only)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    s0 = start_ms - warmup_days * DAY
    counts: dict[str, Any] = {}
    for iv, name in FILES.items():
        bars = bn.klines_range(iv, s0 // STEP[iv] * STEP[iv], end_ms, max_pages=400)
        _write(out_dir / name, bars)
        counts[iv] = len(bars)
    fund = bn.funding(s0, end_ms)
    with (out_dir / "funding.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["fund_ms", "rate"])
        for ts, r, _ in fund:
            w.writerow([ts, r])
    counts["funding"] = len(fund)
    meta = {"start_ms": start_ms, "end_ms": end_ms, "warmup_days": warmup_days, "counts": counts,
            "downloaded_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def load(out_dir: Path) -> tuple[Data, dict[str, Any]]:
    meta_p = out_dir / "meta.json"
    if not meta_p.exists():
        raise BacktestError(f"no downloaded data in {out_dir}: run `intraday-backtest download` first")
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    bars = {iv: _read(out_dir / name, STEP[iv]) for iv, name in FILES.items()}
    fund: list[tuple[int, float]] = []
    fp = out_dir / "funding.csv"
    if fp.exists():
        with fp.open(encoding="utf-8") as fh:
            fund = [(int(r["fund_ms"]), float(r["rate"])) for r in csv.DictReader(fh)]
    return Data(bars["5m"], bars["15m"], bars["1h"], bars["4h"], fund), meta


def gaps(bars: Sequence[Candle], step: int) -> list[tuple[int, int]]:
    return [(a.open_ms, b.open_ms) for a, b in zip(bars, bars[1:]) if b.open_ms - a.open_ms != step]


# ---------------------------------------------------------------- simulation
class Series:
    """Fast 'closed at or before t' windows."""

    def __init__(self, bars: Sequence[Candle], step: int) -> None:
        self.bars = list(bars)
        self.step = step
        self.opens = [c.open_ms for c in self.bars]

    def upto(self, t_ms: int, n: int) -> list[Candle]:
        i = bisect.bisect_right(self.opens, t_ms - self.step)
        return self.bars[max(0, i - n):i]

    def between(self, a_ms: int, b_ms: int) -> list[Candle]:
        i = bisect.bisect_left(self.opens, a_ms)
        j = bisect.bisect_left(self.opens, b_ms)
        return self.bars[i:j]

    def open_at(self, t_ms: int) -> float | None:
        i = bisect.bisect_left(self.opens, t_ms)
        return self.bars[i].open if i < len(self.bars) and self.bars[i].open_ms < t_ms + self.step else None


@dataclass
class Trade:
    direction: int
    setup: str
    leg_id: str
    score: float
    entry_ms: int
    entry: float
    qty_a: float
    qty_b: float
    r: float
    stop0: float
    tp1: float | None
    tp2: float
    multiple: float
    leverage: int
    liq: float
    equity_before: float
    cost_r_est: float
    exits: list[dict[str, Any]] = field(default_factory=list)
    fees: float = 0.0
    funding: float = 0.0
    exit_ms: int | None = None
    reason: str = ""
    net: float = 0.0
    gross: float = 0.0
    r_multiple: float = 0.0
    mfe_r: float = 0.0
    mae_r: float = 0.0


@dataclass
class Open:
    t: Trade
    stop: float
    stage: str
    invalidation: float | None
    best: float
    worst: float
    cost_unit: float
    left_a: float
    left_b: float
    tp1_done: bool = False


class Sim:
    def __init__(self, cfg: Any, data: Data, scenario: str, sizing: str, start_equity: float,
                 inst: Instrument | None = None) -> None:
        self.cfg = cfg
        self.p = idy.Params.from_cfg(cfg)
        self.ic = cfg.intraday
        self.fee_bps, self.slip_bps = SCENARIOS[scenario]
        self.scenario, self.sizing = scenario, sizing
        self.start_equity = float(start_equity)
        self.inst = inst or instrument()
        self.m15 = Series(data.m15, M15)
        self.h1 = Series(data.h1, H1)
        self.h4 = Series(data.h4, 4 * H1)
        self.path_res = "5m" if data.m5 else "15m"
        self.path = Series(data.m5, M5) if data.m5 else self.m15
        self.fund_t = [x[0] for x in data.funding]
        self.fund_r = [x[1] for x in data.funding]
        self.releases = sorted(data.releases)

    # ------------------------------------------------------------ helpers
    def funding_hourly(self, t_ms: int) -> float | None:
        i = bisect.bisect_right(self.fund_t, t_ms) - 1
        return self.fund_r[i] / 8.0 if i >= 0 else None

    def fill(self, px: float, side: int) -> float:
        """side +1 buys, -1 sells: the scenario's slippage is always against us."""
        return px * (1 + side * self.slip_bps / 1e4)

    def fee(self, px: float, qty: float) -> float:
        return px * qty * self.fee_bps / 1e4

    def cost_unit(self, d: int, entry: float, t_ms: int) -> float:
        est = costs.from_scenario(fee_bps_side=self.fee_bps, slip_bps_side=self.slip_bps,
                                  funding_rate_hourly=self.funding_hourly(t_ms), direction=d,
                                  funding_hold_hours=float(self.ic.funding_hold_hours), name=self.scenario)
        return est.per_unit(entry)

    def data_ok(self, t_ms: int) -> bool:
        at = t_ms + RUN_DELAY_MS
        p = self.p
        return (check(self.m15.upto(at, p.need_15m()), "15m", p.need_15m(), at).ok
                and check(self.h1.upto(at, p.need_1h()), "1h", p.need_1h(), at).ok
                and check(self.h4.upto(at, p.need_4h()), "4h", p.need_4h(), at).ok)

    # ------------------------------------------------------------ exits
    def _exit(self, o: Open, qty: float, px_raw: float, ms: int, label: str, equity: list[float]) -> None:
        d = o.t.direction
        px = self.fill(px_raw, -d)
        o.t.exits.append({"ms": ms, "qty": qty, "price": px, "label": label})
        o.t.fees += self.fee(px, qty)

    def _close_all(self, o: Open, px_raw: float, ms: int, label: str, equity: list[float]) -> None:
        left = o.left_a + o.left_b
        if left > 0:
            self._exit(o, left, px_raw, ms, label, equity)
        o.left_a = o.left_b = 0.0

    def path_step(self, o: Open, bar: Candle, equity: list[float]) -> bool:
        """Exchange-side stop / targets inside one path candle. True when the position is gone."""
        d = o.t.direction
        label = "SL" if o.stage == "initial" else ("BE_stop" if (o.stop - o.t.entry) * d <= o.cost_unit * 1.0001 else "trail_stop")
        if (bar.open - o.t.liq) * d <= 0:
            self._close_all(o, o.t.liq, bar.open_ms, "liquidation", equity)
            return True
        if (bar.open - o.stop) * d <= 0:
            self._close_all(o, bar.open, bar.open_ms, label, equity)
            return True
        adverse = bar.low if d > 0 else bar.high
        if (adverse - o.stop) * d <= 0:
            self._close_all(o, o.stop, bar.open_ms, label, equity)
            return True
        fav = bar.high if d > 0 else bar.low
        o.best = max(o.best, fav) if d > 0 else min(o.best, fav)
        o.worst = min(o.worst, adverse) if d > 0 else max(o.worst, adverse)
        if o.t.tp1 is not None and not o.tp1_done and o.left_a > 0 and (fav - o.t.tp1) * d >= 0:
            self._exit(o, o.left_a, o.t.tp1, bar.open_ms, "TP1", equity)
            o.left_a = 0.0
            o.tp1_done = True
        if (fav - o.t.tp2) * d >= 0:
            self._close_all(o, o.t.tp2, bar.open_ms, "TP2", equity)
            return True
        return False

    def finish(self, o: Open, equity: list[float], ms: int) -> Trade:
        t = o.t
        d = t.direction
        t.exit_ms = ms
        t.gross = sum((x["price"] - t.entry) * x["qty"] * d for x in t.exits)
        t.net = t.gross - t.fees + t.funding
        qty = t.qty_a + t.qty_b
        t.r_multiple = t.net / (t.r * qty) if t.r and qty else 0.0
        labels = [x["label"] for x in t.exits]
        last = labels[-1] if labels else "unknown"
        t.reason = last if last == "TP1" or "TP1" not in labels else f"TP1+{last}"
        t.mfe_r = max((o.best - t.entry) * d, 0.0) / t.r if t.r else 0.0
        t.mae_r = max((t.entry - o.worst) * d, 0.0) / t.r if t.r else 0.0
        equity[0] += t.net
        return t

    # ------------------------------------------------------------ main loop
    def run(self, start_ms: int, end_ms: int) -> dict[str, Any]:
        p = self.p
        equity = [self.start_equity]
        peak, max_dd = self.start_equity, 0.0
        trades: list[Trade] = []
        o: Open | None = None
        hist = idy.History()
        legs_time: list[tuple[int, str]] = []
        counters = {"decisions": 0, "data_gap": 0, "blackout": 0, "signals": 0, "too_small": 0, "liq_refused": 0,
                    "stopped_floor": 0}
        rejects: dict[str, int] = {}
        floor = self.start_equity * float(self.cfg.risk.equity_floor_pct_of_net_funded) / 100.0
        t = (start_ms + M15 - 1) // M15 * M15
        prev = t
        while t < end_ms:
            if o is not None:
                gone = False
                for bar in self.path.between(prev, t):
                    if bar.open_ms + self.path.step > t:
                        break
                    if bar.open_ms % H1 == 0 and o is not None:      # hourly funding while holding
                        rate = self.funding_hourly(bar.open_ms)
                        if rate:
                            o.t.funding -= o.t.direction * rate * (o.left_a + o.left_b) * bar.open
                    if self.path_step(o, bar, equity):
                        trades.append(self.finish(o, equity, bar.open_ms))
                        hist.last_exit_ms = bar.open_ms
                        o = None
                        gone = True
                        break
                if o is not None and not gone:
                    o = self.manage(o, t, equity, trades, hist)
            if o is None and equity[0] > floor:
                o = self.decide(t, equity, hist, legs_time, counters, rejects)
            elif equity[0] <= floor:
                counters["stopped_floor"] += 1
            peak = max(peak, equity[0])
            max_dd = max(max_dd, (peak - equity[0]) / peak * 100.0 if peak > 0 else 0.0)
            prev = t
            t += M15
        if o is not None:
            px = self.path.open_at(end_ms) or o.t.entry
            self._close_all(o, px, end_ms, "end_of_test", equity)
            trades.append(self.finish(o, equity, end_ms))
        return {"trades": trades, "equity": equity[0], "start_equity": self.start_equity, "max_dd_pct": max_dd,
                "counters": counters, "rejects": rejects, "path_resolution": self.path_res}

    def manage(self, o: Open, t: int, equity: list[float], trades: list[Trade], hist: idy.History) -> Open | None:
        p = self.p
        d = o.t.direction
        bars = [c for c in self.m15.between(o.t.entry_ms // M15 * M15, t)]
        for c in bars:
            o.best = max(o.best, c.high) if d > 0 else min(o.best, c.low)
            o.worst = min(o.worst, c.low) if d > 0 else max(o.worst, c.high)
        inv = self.h1.upto(t + RUN_DELAY_MS, 1) if p.invalidation_timeframe == "1h" else self.m15.upto(t + RUN_DELAY_MS, 1)
        last = idy.invalidation_bar(inv, o.t.entry_ms, p)
        st = idy.ManageState(direction=d, entry=o.t.entry, r=o.t.r, entry_ms=o.t.entry_ms, invalidation=o.invalidation,
                             stage=o.stage, stop=o.stop, best=o.best, worst=o.worst, two_legs=o.t.tp1 is not None,
                             cost_unit=o.cost_unit)
        px = self.path.open_at(t)
        if px is None:
            return o
        fresh = self.data_ok(t)                   # like live: stale candles -> only the time stop, no stop moves
        reason, _ = idy.exit_signal(st, last, t + RUN_DELAY_MS, p, fresh=fresh)
        if reason:
            self._close_all(o, px, t, reason, equity)
            trades.append(self.finish(o, equity, t))
            hist.last_exit_ms = t
            return None
        if not fresh:
            return o
        atr1h = idy.last_atr(self.h1.upto(t + RUN_DELAY_MS, p.need_1h()), p.atr_period)
        atr15 = idy.last_atr(self.m15.upto(t + RUN_DELAY_MS, p.need_15m()), p.atr_period)
        if not atr1h or not atr15:
            return o
        stage, new = idy.next_stop(st, atr1h, atr15, p, o.tp1_done)
        o.stage = stage
        if new is None:
            return o
        if (px - new) * d <= 0:
            label = "BE_stop" if st.stage == "initial" else "trail_stop"
            self._close_all(o, px, t, label, equity)
            trades.append(self.finish(o, equity, t))
            hist.last_exit_ms = t
            return None
        o.stop = new
        return o

    def decide(self, t: int, equity: list[float], hist: idy.History, legs_time: list[tuple[int, str]],
               counters: dict[str, int], rejects: dict[str, int]) -> Open | None:
        p = self.p
        counters["decisions"] += 1
        at = t + RUN_DELAY_MS
        if not self.data_ok(t):
            counters["data_gap"] += 1
            return None
        day0 = t // DAY * DAY
        hist.leg_counts = idy.History.from_legs([leg for ms, leg in legs_time if ms >= t - 3 * DAY]).leg_counts
        hist.entries_today = sum(1 for ms, _ in legs_time if ms >= day0)
        dec = idy.evaluate(p, t, self.m15.upto(at, p.need_15m()), self.h1.upto(at, p.need_1h()),
                           self.h4.upto(at, p.need_4h()), cost_unit_fn=lambda d, e: self.cost_unit(d, e, t),
                           history=hist)
        if dec.action != "enter":
            for s in dec.setups:
                key = f"{s['kind']}: {re.sub(r'[-+]?[0-9][0-9.,]*', '#', str(s['reason']))[:70]}"
                rejects[key] = rejects.get(key, 0) + 1
            return None
        counters["signals"] += 1
        if idy.blackout(self.releases, at, float(self.ic.event_block_before_minutes),
                        float(self.ic.event_block_after_minutes)):
            counters["blackout"] += 1
            return None
        if dec.score < float(self.cfg.strategy.min_entry_abs_score):
            return None
        d = dec.direction
        px = self.path.open_at(t)
        if px is None:
            counters["data_gap"] += 1
            return None
        entry = self.fill(px, d)
        r = float(dec.r or 0.0)
        eq = equity[0]
        if self.sizing == "owner":
            sz = idy.position_size(self.cfg, eq, tier_fraction(dec.score, self.cfg.strategy), entry, r, self.inst)
            qty, lev, mult = sz["qty"], int(sz["leverage"]), float(sz["multiple"])
        else:
            q = min(eq * 0.03 / r, eq * 10.0 / entry) if r > 0 else 0.0
            qty = quantize_qty(q, self.inst.quantity_decimals)
            mult = float(qty) * entry / eq if eq else 0.0
            lev = max(1, math.ceil(mult / (float(self.cfg.risk.max_margin_use_pct) / 100.0)))
        if lev < 1 or qty <= 0 or float(qty) * entry < self.inst.min_notional:
            counters["too_small"] += 1
            return None
        notional = float(qty) * entry
        liq = estimate_liquidation(entry, d, lev, self.inst, notional, float(self.cfg.risk.liq_estimate_mmr_divisor))
        if not liquidation_ok(entry, liq, r, float(self.cfg.risk.liq_min_sl_multiple)):
            counters["liq_refused"] += 1
            return None
        legs = dict(idy.split_legs(qty, entry, self.inst, float(self.ic.tp1_fraction)))
        qa, qb = float(legs.get("A", Decimal(0))), float(legs["B"])
        two = qa > 0
        tr = Trade(d, str(dec.setup), str(dec.leg_id), dec.score, t, entry, qa, qb, r, entry - d * r,
                   (entry + d * float(self.ic.tp1_r) * r) if two else None, entry + d * float(self.ic.tp2_r) * r,
                   mult, lev, liq, eq, float(dec.cost_r or 0.0))
        tr.fees += self.fee(entry, qa + qb)
        legs_time.append((t, str(dec.leg_id)))
        return Open(tr, tr.stop0, "initial", dec.invalidation, entry, entry, float(dec.cost_per_unit or 0.0), qa, qb)


# ---------------------------------------------------------------- statistics / report
def stats(res: dict[str, Any]) -> dict[str, Any]:
    tr: list[Trade] = res["trades"]
    n = len(tr)
    out: dict[str, Any] = {"trades": n, "net_return_pct": (res["equity"] / res["start_equity"] - 1) * 100.0,
                           "end_equity": res["equity"], "max_drawdown_pct": res["max_dd_pct"],
                           "path_resolution": res["path_resolution"], **res["counters"]}
    if not n:
        return out
    wins = [t for t in tr if t.net > 0]
    out.update({
        "longs": sum(1 for t in tr if t.direction > 0), "shorts": sum(1 for t in tr if t.direction < 0),
        "win_rate_pct": len(wins) / n * 100.0, "avg_net_r": sum(t.r_multiple for t in tr) / n,
        "avg_hold_h": sum(((t.exit_ms or t.entry_ms) - t.entry_ms) / H1 for t in tr) / n,
        "fees": sum(t.fees for t in tr), "funding": sum(t.funding for t in tr),
        "avg_mfe_r": sum(t.mfe_r for t in tr) / n, "avg_mae_r": sum(t.mae_r for t in tr) / n,
        "avg_cost_r_est": sum(t.cost_r_est for t in tr) / n,
        "avg_multiple": sum(t.multiple for t in tr) / n,
    })
    for key, fn in (("by_setup", lambda t: t.setup), ("by_side", lambda t: "long" if t.direction > 0 else "short"),
                    ("by_exit", lambda t: t.reason)):
        groups: dict[str, list[Trade]] = {}
        for t in tr:
            groups.setdefault(fn(t), []).append(t)
        out[key] = {k: {"trades": len(v), "avg_net_r": sum(x.r_multiple for x in v) / len(v),
                        "net": sum(x.net for x in v)} for k, v in sorted(groups.items())}
    return out


def config_hash(cfg: Any) -> str:
    keys = ("intraday", "risk", "strategy")
    blob = json.dumps({k: cfg.to_dict()[k] if hasattr(cfg, "to_dict") else None for k in keys}, sort_keys=True,
                      default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def run_all(cfg: Any, data: Data, start_ms: int, end_ms: int, start_equity: float = 100.0) -> dict[str, Any]:
    """Every scenario x sizing on the full period, and the base scenario on the first 2/3 and the last 1/3."""
    out: dict[str, Any] = {"period": [start_ms, end_ms], "start_equity": start_equity, "full": {}, "segments": {},
                           "approximations": APPROXIMATIONS,
                           "data": {iv: {"bars": len(b), "gaps": len(gaps(b, STEP[iv]))}
                                    for iv, b in (("5m", data.m5), ("15m", data.m15), ("1h", data.h1), ("4h", data.h4))}}
    trades: dict[str, list[dict[str, Any]]] = {}
    for sc in SCENARIOS:
        for sz in SIZINGS:
            res = Sim(cfg, data, sc, sz, start_equity).run(start_ms, end_ms)
            out["full"][f"{sc}/{sz}"] = stats(res)
            trades[f"{sc}/{sz}"] = [asdict(t) for t in res["trades"]]
    cut = start_ms + (end_ms - start_ms) * 2 // 3
    for name, (a, b) in (("first_two_thirds", (start_ms, cut)), ("last_third", (cut, end_ms))):
        for sz in SIZINGS:
            out["segments"][f"{name}/base/{sz}"] = stats(Sim(cfg, data, "base", sz, start_equity).run(a, b))
    out["trades"] = trades
    return out


def _d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def summary_md(rep: dict[str, Any], cfg: Any) -> str:
    a, b = rep["period"]
    L = [f"# btcperp v2.0.0 intraday backtest {_d(a)} -> {_d(b)}", "",
         f"Start equity {rep['start_equity']:g} USD per run, compounding, flat at the start of every run. Config hash "
         f"{config_hash(cfg)[:12]}. Path resolution: {next(iter(rep['full'].values()))['path_resolution']}.", "",
         "**This is an approximation, not a replay of Polymarket fills:**", ""]
    L += [f"- {x}" for x in rep["approximations"]]
    L += ["", "Data: " + ", ".join(f"{k} {v['bars']} candles ({v['gaps']} gaps)" for k, v in rep["data"].items()), "",
          "## Full period", "",
          "| scenario / sizing | net % | trades (long/short) | win % | avg net R | avg hold h | max DD % | MFE R | MAE R |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for k, s in rep["full"].items():
        L.append(f"| {k} | {s['net_return_pct']:+.2f} | {s['trades']} ({s.get('longs', 0)}/{s.get('shorts', 0)}) | "
                 f"{s.get('win_rate_pct', 0):.1f} | {s.get('avg_net_r', 0):+.3f} | {s.get('avg_hold_h', 0):.2f} | "
                 f"{s['max_drawdown_pct']:.1f} | {s.get('avg_mfe_r', 0):.2f} | {s.get('avg_mae_r', 0):.2f} |")
    L += ["", "## Segments (base cost)", "", "| segment | net % | trades | win % | avg net R | max DD % |",
          "|---|---:|---:|---:|---:|---:|"]
    for k, s in rep["segments"].items():
        L.append(f"| {k} | {s['net_return_pct']:+.2f} | {s['trades']} | {s.get('win_rate_pct', 0):.1f} | "
                 f"{s.get('avg_net_r', 0):+.3f} | {s['max_drawdown_pct']:.1f} |")
    base = rep["full"].get("base/owner", {})
    for sec in ("by_setup", "by_side", "by_exit"):
        if base.get(sec):
            L += ["", f"## base/owner {sec}", "", "| group | trades | avg net R | net USD |", "|---|---:|---:|---:|"]
            L += [f"| {g} | {v['trades']} | {v['avg_net_r']:+.3f} | {v['net']:+.2f} |" for g, v in base[sec].items()]
    L += ["", "## Decision counters (base/owner)", "", "```", json.dumps({k: base.get(k) for k in (
        "decisions", "data_gap", "signals", "blackout", "too_small", "liq_refused", "stopped_floor")}, indent=1), "```",
          "", "Reading it: a positive zero-cost result that turns negative with costs means the edge does not pay for "
          "the trading; more trades or more shorts are not evidence of profit. Judge the base and stress rows and "
          "both segments.", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- live vs replay (intraday-replay)
def replay(store: Any, cfg: Any, since_ms: int) -> dict[str, Any]:
    """Recompute every stored live 15-minute decision since `since_ms` from the cached candles and the inputs the
    decision logged (costs per direction, no-chasing history). Any difference means the data or the code changed
    between the live run and now; it is listed, never hidden."""
    from perpbot.candles import CandleCache

    p = idy.Params.from_cfg(cfg)
    cache = CandleCache(store, None, backfill_days=float(cfg.intraday.cache_backfill_days))
    rows = store.query("SELECT utc_day, action, data, config_version FROM decisions WHERE ts_ms >= ? "
                       "AND utc_day LIKE '%/15m' ORDER BY id", [since_ms])
    out = {"checked": 0, "same": 0, "skipped": 0, "different": []}
    for r in rows:
        rec = (r["data"] or {}).get("intraday") if isinstance(r["data"], dict) else None
        if not rec or not rec.get("data_ok") or not rec.get("replay") or r["config_version"] != cfg.config_version:
            out["skipped"] += 1                   # older config version: other rule values, not comparable
            continue
        live = rec["decision"]
        t = int(live["t_ms"])
        at = int(rec["replay"]["now_ms"])
        m15 = cache.load("15m", at - (p.need_15m() + 4) * M15, at)
        h1 = cache.load("1h", at - (p.need_1h() + 4) * H1, at)
        h4 = cache.load("4h", at - (p.need_4h() + 4) * 4 * H1, at)
        logged = {int(k): float(v) for k, v in (rec["replay"].get("cost_per_unit_by_dir") or {}).items()}
        h = rec["replay"].get("history") or {}
        hist = idy.History.from_legs(h.get("traded_legs") or [], h.get("last_exit_ms"), int(h.get("entries_today") or 0))
        again = idy.evaluate(p, t, m15, h1, h4, cost_unit_fn=lambda d, e: logged.get(d, e), history=hist,
                             position_dir=int(rec["replay"].get("position_dir") or 0))
        out["checked"] += 1
        keys = ("action", "direction", "setup", "leg_id")
        diff = {k: (live.get(k), getattr(again, k)) for k in keys if live.get(k) != getattr(again, k)}
        for k in ("stop", "r", "tp1", "tp2"):
            a, b = live.get(k), getattr(again, k)
            if (a is None) != (b is None) or (a is not None and abs(float(a) - float(b)) > 1e-6):
                diff[k] = (a, b)
        if diff:
            out["different"].append({"key": r["utc_day"], "diff": diff})
        else:
            out["same"] += 1
    return out
