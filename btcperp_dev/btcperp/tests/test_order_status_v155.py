"""v1.5.5: live smoketest W, fok_unfilled_status: accepted, but GET /v1/account/orders?client_order_id= found nothing
(raw_status null). The order-id lookup did work (place_cancel found its order "cancelled"). Also: a failed step must
never leave a test position open."""

from datetime import date, timedelta

import pytest

from perpbot.exchange.base import PlaceResult
from perpbot.paths import Paths
from perpbot.records import Records
from perpbot.smoketest import run_smoketest
from perpbot.timeutil import to_ms

from conftest import hkt


def _smoke(w, tmp_path, **kw):
    w.ex.proxy_info = type(w.ex.proxy_info)("0xOWNER", "0xP", to_ms(w.clock.now() + timedelta(days=30)))
    paths = Paths(tmp_path / "root")
    paths.ensure()
    ok, res = run_smoketest(w.engine(), paths, **kw)
    return ok, {r["step"]: r for r in res}


def test_entry_is_confirmed_when_the_client_order_id_lookup_finds_nothing(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.ex.coid_lookup_empty = True
    w.bn.signal(date(2026, 10, 5), "strong_long")
    out = w.decide()
    assert out["plan"]["action"] == "enter" and w.pos() > 0
    t = Records(w.store).open_trade()
    assert t is not None and t["direction"] == 1
    st = [r for r in w.store.query("SELECT data FROM orders WHERE event='status' AND purpose='entry'")]
    assert st and st[0]["data"]["source"] == "placement update"


def test_confirm_order_polls_by_order_id_when_the_placement_update_is_missing(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.ex.coid_lookup_empty = True
    eng = w.engine()
    res = w.ex.place_order(instrument_id=1, side="BUY", quantity="0.0010", tif="fok", price="99000",
                           reduce_only=False, client_order_id="a" * 32)
    o = eng.confirm_order("a" * 32, PlaceResult(True, res.order_id, "a" * 32), "test")
    assert o is not None and o.status == "fok_unfilled" and o.id == res.order_id


def test_smoketest_passes_when_the_client_order_id_lookup_finds_nothing(world, tmp_path):
    w = world(hkt(2026, 10, 5, 3, 0))
    w.ex.coid_lookup_empty = True
    ok, by = _smoke(w, tmp_path)
    assert ok, [(k, r["detail"]) for k, r in by.items() if r["ok"] is False]
    d = by["fok_unfilled_status"]["detail"]
    assert d["status_by_client_order_id"] is None and d["status_from_placement_update"] == "fok_unfilled"
    assert d["status_by_order_id"] == "fok_unfilled" and d["raw_status"] == "fok_unfilled"
    assert by["cleanup"]["ok"] and by["cleanup"]["detail"]["position"] is None


def test_failed_step_after_the_entry_never_leaves_a_position(world, tmp_path):
    w = world(hkt(2026, 10, 5, 3, 0))
    w.ex.bracket_without_sl = True                  # the entry fills but no SL is attached: open_bracket fails
    ok, by = _smoke(w, tmp_path)
    assert not ok and by["open_bracket"]["ok"] is False and by["close"]["ok"] is None
    assert by["cleanup"]["ok"] is True and by["cleanup"]["detail"]["cleanup"]["had_position"] is True
    assert w.pos() == 0
    assert not [o for o in w.ex.orders.values() if o.is_active]


def test_read_only_smoketest_has_no_cleanup_step(world, tmp_path):
    w = world(hkt(2026, 10, 5, 3, 0))
    ok, by = _smoke(w, tmp_path, allow_trading=False)
    assert ok and "cleanup" not in by


def test_orders_by_coid_falls_back_to_the_logged_order_id(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    w.ex.coid_lookup_empty = True
    eng = w.engine()
    res = w.ex.place_order(instrument_id=1, side="BUY", quantity="0.0010", tif="fok", price="99000",
                           reduce_only=False, client_order_id="b" * 32)
    assert eng.orders_by_coid("b" * 32) == []                       # nothing logged yet: nothing known
    eng.log_order("entry", "response", "b" * 32, res.order_id, "accepted", {})
    got = eng.orders_by_coid("b" * 32)
    assert [o.id for o in got] == [res.order_id] and got[0].status == "fok_unfilled"


@pytest.fixture
def no_coid_lookup(monkeypatch):
    """Every mock exchange behaves like the live one: ?client_order_id= finds nothing."""
    from perpbot.exchange.mock import MockExchange

    orig = MockExchange.__init__

    def init(self, *a, **k):
        orig(self, *a, **k)
        self.coid_lookup_empty = True

    monkeypatch.setattr(MockExchange, "__init__", init)


@pytest.mark.parametrize("name", [
    "test_review_v110.test_b1_entry_fills_but_sl_row_rejected_no_second_entry",
    "test_review_v110.test_b1_rejected_and_stale_position_read_no_retry",
    "test_review_v110.test_b1_rejected_and_long_stale_read_defers_to_next_run",
    "test_review_v110.test_b1_true_fok_unfilled_still_retries_once",
    "test_engine.test_flip_interrupted_halfway_completed_by_0850",
    "test_engine.test_0830_and_0850_both_fire_only_one_entry",
])
def test_entry_safety_rules_hold_without_the_client_order_id_lookup(name, world, no_coid_lookup):
    import importlib

    mod, fn = name.split(".")
    getattr(importlib.import_module(mod), fn)(world)
