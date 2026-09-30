"""v1.5.3: findings from the first live smoketest W (2026-09-30).

1. The ticker was ANOTHER instrument's: the SDK's fetch_perps_ticker returns the first ticker the API sends and the
   API ignored the instrument filter (mark 7690.6 while BTC-USD's book was 83264 / 83265).
2. "price exceeds allowed significant figures": BTC-USD has price_decimals 1, but the exchange allows at most
   5 significant figures (its book quotes whole dollars).
3. price_bounds 0.02: the smoketest's resting order 2% below the bid sat on the edge of the band.
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from perpbot.exchange.base import Book, ExchangeError, Ticker, mark_vs_book
from perpbot.paths import Paths
from perpbot.risk import quantize_price
from perpbot.smoketest import run_smoketest

from conftest import hkt, ok_leverage

LIVE_BOOK = Book([(83264.0, 0.5)], [(83265.0, 0.4)])


def test_prices_have_at_most_five_significant_figures():
    assert quantize_price(83264.37, 1, "down") == Decimal("83264")
    assert quantize_price(83264.57, 1, "nearest") == Decimal("83265")
    assert quantize_price(83264.01, 1, "up") == Decimal("83265")
    assert format(quantize_price(107853.2, 1, "down"), "f") == "107850"      # never "1.0785E+5"
    assert format(quantize_price(107853.2, 1, "up"), "f") == "107860"
    assert quantize_price(9876.54, 1, "nearest") == Decimal("9876.5")      # decimals still apply below 10,000
    assert quantize_price(83264.37, 1, "down", sig_figs=0) == Decimal("83264.3")   # the v1.5.2 behaviour


def test_mark_must_match_the_book():
    assert mark_vs_book(83264.4, LIVE_BOOK, 0.02)[0]
    ok, dev = mark_vs_book(7690.6, LIVE_BOOK, 0.02)
    assert not ok and dev > 0.9
    assert not mark_vs_book(83264.4, Book([], [(83265.0, 1.0)]), 0.02)[0]


def _tick(iid, mark):
    return SimpleNamespace(instrument_id=iid, mark_price=mark, index_price=mark, last_price=mark, mid_price=mark,
                           funding_rate="0.00000625", open_interest="1", next_funding=None, timestamp=None)


class FakePublic:
    def __init__(self, filtered, full):
        self.filtered, self.full, self.calls = filtered, full, []

    async def fetch_perps_tickers(self, instrument_id=None):
        self.calls.append(instrument_id)
        return tuple(self.filtered if instrument_id is not None else self.full)


def _adapter(cfg, public):
    from perpbot.exchange.polymarket import PolymarketExchange

    ex = PolymarketExchange(cfg, SimpleNamespace(proxy_address="0x0", proxy_private_key="", proxy_secret="",
                                                 proxy_expires_at=None))
    ex._public = public
    return ex


def test_adapter_picks_the_ticker_of_the_requested_instrument(cfg):
    ex = _adapter(cfg, FakePublic([_tick(1, "7690.6"), _tick(6, "83264.4")], []))
    try:
        t = ex.get_ticker(6)
        assert t.instrument_id == 6 and t.mark == 83264.4
    finally:
        ex.close()


def test_adapter_searches_the_full_list_then_refuses(cfg):
    pub = FakePublic([_tick(1, "7690.6")], [_tick(1, "7690.6"), _tick(6, "83264.4")])
    ex = _adapter(cfg, pub)
    try:
        assert ex.get_ticker(6).mark == 83264.4 and pub.calls == [6, None]
        ex._public = FakePublic([_tick(1, "7690.6")], [_tick(1, "7690.6")])
        with pytest.raises(ExchangeError, match="ticker for instrument 6 not returned"):
            ex.get_ticker(6)
    finally:
        ex.close()


def _wrong_ticker(w):
    w.ex.get_ticker = lambda iid: Ticker(iid, 7690.6, 7689.9, 7690.6, 7690.6, 6.25e-06, 1.0, 0, 0)


def test_decide_refuses_a_mark_that_does_not_match_the_book(world):
    w = world(hkt(2026, 10, 5, 8, 30))
    ok_leverage(w.ex)
    w.bn.signal(date(2026, 10, 5), "strong_long")
    _wrong_ticker(w)
    with pytest.raises(ExchangeError, match="does not match"):
        w.decide()
    assert not [c for c in w.ex.calls if c[0] == "place_order"] and w.pos() == 0


def test_smoketest_stops_on_wrong_ticker(world, tmp_path):
    w = world(hkt(2026, 10, 5, 3, 0))
    w.ex.proxy_info = type(w.ex.proxy_info)("0xOWNER", "0xP", None)
    _wrong_ticker(w)
    paths = Paths(tmp_path / "root")
    paths.ensure()
    ok, res = run_smoketest(w.engine(), paths)
    by = {r["step"]: r for r in res}
    assert not ok and by["prices"]["ok"] is False and by["prices"]["detail"]["mark_matches_book"] is False
    assert by["basis"]["ok"] is False
    assert by["place_cancel"]["ok"] is None and not [c for c in w.ex.calls if c[0] == "place_order"]


def test_smoketest_resting_order_stays_inside_the_price_band(world, tmp_path):
    from datetime import timedelta

    from perpbot.timeutil import to_ms

    w = world(hkt(2026, 10, 5, 3, 0))
    w.ex.proxy_info = type(w.ex.proxy_info)("0xOWNER", "0xP", to_ms(w.clock.now() + timedelta(days=30)))
    w.ex.inst.price_bounds = 0.02                       # BTC-USD live; the config offset is 2%
    paths = Paths(tmp_path / "root")
    paths.ensure()
    ok, res = run_smoketest(w.engine(), paths)
    assert ok, [r for r in res if r["ok"] is False]
    rest = next(c[1] for c in w.ex.calls if c[0] == "place_order" and c[1]["tif"] == "gtc")
    assert abs(float(rest["price"]) / w.ex.bid - 1.0) <= 0.0101
