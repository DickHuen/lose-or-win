"""Binance and Telegram clients against httpx.MockTransport (no network)."""

import json

import httpx

from perpbot.datasources.binance import BinanceData
from perpbot.telegram import Telegram, parse_command
from perpbot.timeutil import DAY_MS


def test_binance_klines_closed_only_and_fallback_on_451(cfg, monkeypatch):
    monkeypatch.setattr("perpbot.datasources.binance.time.sleep", lambda s: None)
    now = 1_790_000_000_000
    day0 = (now // DAY_MS) * DAY_MS
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.host)
        if req.url.host == "api.binance.com":
            return httpx.Response(451, json={"msg": "restricted location"})
        rows = [[day0 - 2 * DAY_MS, "1", "2", "0.5", "1.5", "10", day0 - DAY_MS - 1, "0", 5, "0", "0", "0"],
                [day0 - DAY_MS, "1.5", "3", "1", "2.5", "11", day0 - 1, "0", 6, "0", "0", "0"],
                [day0, "2.5", "4", "2", "3", "1", day0 + DAY_MS - 1, "0", 1, "0", "0", "0"]]   # still open
        return httpx.Response(200, json=rows)

    b = BinanceData(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))
    kl = b.klines("1d", 500, now)
    assert [c.close for c in kl] == [1.5, 2.5]
    assert kl[-1].close_ms == day0
    assert seen[:2] == ["api.binance.com", "data-api.binance.vision"]
    assert b.retries >= 1


def test_binance_funding_pagination(cfg):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        start = int(req.url.params["startTime"])
        calls.append(start)
        if len(calls) == 1:
            rows = [{"symbol": "BTCUSDT", "fundingTime": start + i * 28_800_000, "fundingRate": "0.0001",
                     "markPrice": "100000"} for i in range(1000)]
        else:
            rows = [{"symbol": "BTCUSDT", "fundingTime": start + 5, "fundingRate": "-0.0002", "markPrice": ""}]
        return httpx.Response(200, json=rows)

    b = BinanceData(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = b.funding(0, 10**13)
    assert len(out) == 1001 and out[-1][1] == -0.0002 and out[-1][2] == 0.0
    assert len(calls) == 2 and calls[1] == out[999][0] + 1


def test_telegram_send_and_updates(cfg, caplog):
    sent = []
    token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/sendMessage"):
            sent.append(req.content.decode())
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"ok": True, "result": [
            {"update_id": 7, "message": {"chat": {"id": 42}, "text": "/kill@mybot"}},
            {"update_id": 8, "message": {"chat": {"id": 5}, "text": "/pause"}}]})

    tg = Telegram(cfg, token, "42", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert tg.send("x" * 9000)
    assert len(sent) == 3                              # split into chunks under the Telegram limit
    ups = tg.get_updates(None)
    assert parse_command(ups[0], "42")[1] == "kill"
    assert parse_command(ups[1], "42")[1] is None      # other chat ignored
    assert token not in caplog.text
    assert json.dumps(parse_command({"update_id": 1, "message": {"chat": {"id": 42}, "text": "/foo"}}, "42")[1]) == '"unknown"'
