"""Randomized multi-day simulation with invariant checks (seeded, deterministic per seed)."""

import random
from datetime import date, timedelta

import pytest

from perpbot.exchange.base import Order
from perpbot.records import Records
from perpbot.timeutil import utc_day

from conftest import P, hkt

KINDS = ["strong_long", "strong_short", "weak_long", "weak_short", "flat"]
RUNS = [(8, 30, "decide"), (8, 50, "decide"), (12, 30, "manage"), (16, 30, "manage"), (20, 30, "manage"),
        (0, 30, "manage"), (4, 30, "manage")]


def active_sl(w):
    return [o for o in w.ex.orders.values() if o.tpsl_kind == "sl" and o.status in ("armed", "untriggered")]


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_randomized_days_keep_invariants(world, seed):
    rnd = random.Random(seed)
    start = date(2026, 10, 5)
    w = world(hkt(2026, 10, 5, 8, 30))
    for i in range(20):
        d = start + timedelta(days=i)
        w.bn.signal(d, rnd.choice(KINDS))
        if rnd.random() < 0.1:
            w.bn.set_funding(d, 0.01)
        for hh, mm, cmd in RUNS:
            dd = d + timedelta(days=1) if hh < 8 else d
            w.at(hkt(dd.year, dd.month, dd.day, hh, mm))
            if cmd == "decide" and rnd.random() < 0.15:
                w.ex.fok_outcomes.append(False)
            if rnd.random() < 0.05:
                for o in active_sl(w):
                    o.status = "cancelled"
            w.ex.set_mark(P * (1 + rnd.uniform(-0.03, 0.03)))
            if rnd.random() < 0.2:
                w.ex.apply_funding(rnd.uniform(-0.0002, 0.0002))
            (w.decide if cmd == "decide" else w.manage)()
            # invariants after every run
            if w.pos() != 0:
                assert active_sl(w), f"seed {seed} day {d} {cmd}: open position without SL"
                t = Records(w.store).open_trade()
                assert t is not None and t["direction"] == (1 if w.pos() > 0 else -1)
            assert w.state()["position_state"] in ("flat", "open")
            assert w.state()["position_state"] == ("open" if w.pos() != 0 else "flat")
    # at most one opening fill per UTC day
    per_day = {}
    for f in w.ex.fills:
        if f.previous_size == 0:
            k = utc_day(w.clock.now().fromtimestamp(f.ts_ms / 1000, tz=w.clock.now().tzinfo))
            per_day[k] = per_day.get(k, 0) + 1
    assert all(n == 1 for n in per_day.values()), per_day
    # every closed trade has a reason and numbers
    for t in Records(w.store).closed_trades():
        assert t["exit_reason"] and t["net_pnl"] is not None
    # entries only inside the 08:30-09:30 HKT window (00:30-01:30 UTC)
    for f in w.ex.fills:
        if f.previous_size == 0:
            assert 30 * 60_000 <= f.ts_ms % 86_400_000 <= 90 * 60_000
    closed = Records(w.store).closed_trades()
    assert len(closed) >= 2, "simulation should produce trades"


def test_leftover_trigger_cancelled_before_entry(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    stale = Order(9999, 1, "SELL", 0.0, 0.01, "ioc", True, "armed", 0, 0, None, "sl", "position", 90_000.0)
    w.ex.orders[stale.id] = stale
    w.bn.signal(date(2026, 10, 5), "strong_long")
    w.decide()
    assert stale.status == "cancelled" and w.pos() > 0
