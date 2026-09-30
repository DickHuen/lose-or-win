"""v1.5.2: live smoketest 2026-09-30 - GET /v1/info/fees listed only the "equity" category, so the fees step failed.
The bot now uses the instrument's own category and, when the exchange does not list it, the higher of the listed
rates and the config estimate; the full smoketest also records the fee actually charged."""

import json
from datetime import timedelta

from perpbot.cli import real_fee_rate
from perpbot.exchange.base import taker_fee_for
from perpbot.paths import Paths
from perpbot.smoketest import run_smoketest, summary_text
from perpbot.timeutil import to_ms

from conftest import hkt

LIVE_2026_09_30 = [{"category": "equity", "taker_fee_rate": 0.0004, "maker_fee_rate": 0.000125}]


def test_taker_fee_for_listed_and_missing_category():
    assert taker_fee_for([{"category": "crypto", "taker_fee_rate": 0.0003}], "crypto", 0.0005) == (0.0003, True)
    assert taker_fee_for(LIVE_2026_09_30, "crypto", 0.0005) == (0.0005, False)
    assert taker_fee_for(LIVE_2026_09_30, "crypto", 0.0002) == (0.0004, False)
    assert taker_fee_for([], "crypto", 0.0005) == (0.0005, False)
    assert taker_fee_for([{"category": "crypto", "taker_fee_rate": None}], "crypto", 0.0005) == (0.0005, False)


def test_smoketest_fees_step_passes_when_category_not_listed(world, tmp_path):
    w = world(hkt(2026, 10, 5, 3, 0))
    w.ex.proxy_info = type(w.ex.proxy_info)("0xOWNER", "0xP", None)
    w.ex.get_fee_schedule = lambda: list(LIVE_2026_09_30)
    paths = Paths(tmp_path / "root")
    paths.ensure()
    ok, res = run_smoketest(w.engine(), paths, allow_trading=False)
    fees = {r["step"]: r for r in res}["fees"]
    assert fees["ok"] is True
    d = fees["detail"]
    assert d["category"] == "crypto" and d["category_listed"] is False
    assert d["taker_fee_rate"] == 0.0005 and "lists no fee" in d["note"]
    assert "[PASS] fees" in summary_text(ok, res)


def test_full_smoketest_records_measured_fee_and_backtest_uses_the_higher(world, tmp_path):
    w = world(hkt(2026, 10, 5, 3, 0))
    w.ex.proxy_info = type(w.ex.proxy_info)("0xOWNER", "0xP", to_ms(w.clock.now() + timedelta(days=30)))
    w.ex.fee_rate = 0.0009                                    # charged on fills; the schedule is lower
    w.ex.get_fee_schedule = lambda: list(LIVE_2026_09_30)
    paths = Paths(tmp_path / "root")
    paths.ensure()
    ok, res = run_smoketest(w.engine(), paths)
    assert ok, [r for r in res if r["ok"] is False]
    by = {r["step"]: r for r in res}
    assert abs(by["d_funding_after_close"]["detail"]["measured_taker_fee_rate"] - 0.0009) < 1e-12
    assert abs(real_fee_rate(paths) - 0.0009) < 1e-12
    raw = next(paths.smoketest_dir.glob("smoketest_*.json")).read_text(encoding="utf-8")
    assert "真實 taker 手續費率" in raw and "\\u771f" not in raw   # readable Chinese, not \u escapes
    assert json.loads(raw)["ok"] is True
