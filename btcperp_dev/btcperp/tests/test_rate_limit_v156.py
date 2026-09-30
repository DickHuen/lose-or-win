"""v1.5.6: live smoketest W - the close filled (exchange history: open 13:44:27, close 13:44:28), but the step failed
with "rate limited (retry_after=1.0)" on GET /v1/account/portfolio while it polled every 0.5 s. Reads now wait and
retry; writes never do; the smoketest polls once a second and a failed read is not a failed close."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from polymarket import errors as pe

from perpbot.exchange.base import ExchangeError
from perpbot.paths import Paths
from perpbot.smoketest import run_smoketest
from perpbot.timeutil import to_ms

from conftest import hkt


def _adapter(cfg, monkeypatch, sleeps):
    from perpbot.exchange import polymarket as pmod

    monkeypatch.setattr(pmod.time, "sleep", lambda s: sleeps.append(s))
    return pmod.PolymarketExchange(cfg, SimpleNamespace(proxy_address="0x0", proxy_private_key="", proxy_secret="",
                                                        proxy_expires_at=None))


class Limited:
    def __init__(self, fails):
        self.fails, self.calls = fails, 0

    async def fetch_perps_tickers(self, instrument_id=None):
        self.calls += 1
        if self.calls <= self.fails:
            raise pe.RateLimitError("Request was rate limited", retry_after=1.0)
        return (SimpleNamespace(instrument_id=6, mark_price="83264.4", index_price="83264", last_price="83264",
                                mid_price="83264.5", funding_rate="0.00000625", open_interest="1", next_funding=None,
                                timestamp=None),)


def test_reads_wait_and_retry_on_rate_limit(cfg, monkeypatch):
    sleeps = []
    ex = _adapter(cfg, monkeypatch, sleeps)
    try:
        ex._public = Limited(fails=2)
        assert ex.get_ticker(6).mark == 83264.4
        assert ex._public.calls == 3 and sleeps == [1.0, 1.0]
        ex._public = Limited(fails=99)
        with pytest.raises(ExchangeError, match="rate limited"):
            ex.get_ticker(6)
        assert ex._public.calls == 5                          # 1 + RATE_LIMIT_RETRIES, then give up
    finally:
        ex.close()


def test_writes_never_retry_on_rate_limit(cfg, monkeypatch):
    sleeps, calls = [], []
    ex = _adapter(cfg, monkeypatch, sleeps)

    async def command():
        calls.append(1)
        raise pe.RateLimitError("Request was rate limited", retry_after=1.0)

    try:
        with pytest.raises(ExchangeError, match="rate limited"):
            ex._run(command)
        assert calls == [1] and sleeps == []
    finally:
        ex.close()


def test_smoketest_close_survives_rate_limited_reads(world, tmp_path):
    w = world(hkt(2026, 10, 5, 3, 0))
    w.ex.proxy_info = type(w.ex.proxy_info)("0xOWNER", "0xP", to_ms(w.clock.now() + timedelta(days=30)))
    real_account, real_place, state = w.ex.get_account, w.ex.place_order, {"limited": 0}

    def place(**kw):
        if kw.get("reduce_only"):
            state["limited"] = 2                              # the next two portfolio reads are rate-limited
        return real_place(**kw)

    def flaky_account():
        if state["limited"]:
            state["limited"] -= 1
            raise ExchangeError("rate limited (retry_after=1.0)")
        return real_account()

    w.ex.place_order, w.ex.get_account = place, flaky_account
    paths = Paths(tmp_path / "root")
    paths.ensure()
    ok, res = run_smoketest(w.engine(), paths)
    by = {r["step"]: r for r in res}
    assert by["close"]["ok"] is True, by["close"]["detail"]
    assert len(by["close"]["detail"]["g3_account_read_after_close"]["read_errors"]) == 2
    assert w.pos() == 0
