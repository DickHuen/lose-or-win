"""Proxy key tool (review v1.2.0 item 11): the main wallet only signs; the proxy key never leaves this computer."""

import asyncio
import http.client
import json
import os
import threading
import time

import httpx
import pytest
from eth_account import Account

from perpbot import proxykey as pkm
from perpbot.offline_sign import sign
from perpbot.paths import Paths

MAIN_KEY = "0x" + "11" * 32
MAIN = Account.from_key(MAIN_KEY).address


class FakeExchange:
    """httpx MockTransport standing in for POST /v1/account/proxy and GET /v1/account/credentials."""

    def __init__(self, reject: str = ""):
        self.reject = reject
        self.bodies = []
        self.registered: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/account/proxy":
            body = json.loads(request.content)
            self.bodies.append(body)
            if self.reject:
                return httpx.Response(400, json={"error": self.reject})
            args = body["op"]["args"]
            self.registered[args["proxy"]] = args["expiry"]
            return httpx.Response(200, json={"secret": "proxy-secret-abc123"})
        if request.method == "GET" and request.url.path == "/v1/account/credentials":
            keys = [{"proxy": p, "label": "btcperp", "expiry": e} for p, e in self.registered.items()]
            return httpx.Response(200, json={"address": MAIN, "keys": keys})
        return httpx.Response(404)

    def transport(self):
        from polymarket.clients._transport import AsyncTransport

        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="https://perps.test")
        return AsyncTransport(base_url="https://perps.test", client=client)


@pytest.fixture
def root(tmp_path):
    paths = Paths(tmp_path / "btcperp")
    paths.ensure()
    paths.env_file.write_text("# my comment\nPM_PROXY_PRIVATE_KEY=\nPM_PROXY_SECRET=\nPM_WALLET_ADDRESS=\n"
                              "HEALTHCHECK_PING_URL=https://hc-ping.com/keep-me\n", encoding="utf-8")
    return paths


def _now_ms():
    return int(time.time() * 1000)


def test_new_request_keeps_the_proxy_key_out_of_shared_files(root, cfg):
    p = pkm.new_request(root, cfg, days=30, label="btcperp", now_ms=_now_ms())
    pending = root.root / pkm.PENDING_NAME
    assert pending.exists() and p.private_key in pending.read_text()
    if os.name != "nt":
        assert oct(pending.stat().st_mode & 0o777) == "0o600"
    for f in ("sign_request.json", "sign.html"):
        text = (root.data_dir / "proxykey" / f).read_text()
        assert p.private_key[2:] not in text and p.proxy in text
    req = json.loads((root.data_dir / "proxykey" / "sign_request.json").read_text())
    assert req["primaryType"] == "CreateProxy" and req["domain"]["chainId"] == 137
    assert req["message"]["exp"] - req["message"]["ts"] == 30 * 86_400_000


def test_offline_signature_registers_and_writes_env(root, cfg):
    fx = FakeExchange()
    p = pkm.new_request(root, cfg, days=30, label="btcperp", now_ms=_now_ms())
    req = json.loads((root.data_dir / "proxykey" / "sign_request.json").read_text())
    signer, sig = sign(req, MAIN_KEY)                       # happens on ANOTHER computer
    assert signer == MAIN
    res = pkm.finish(root, cfg, sig, transport=fx.transport())
    assert res["proxy"] == p.proxy and res["owner"] == MAIN
    body = fx.bodies[0]
    assert body["op"] == {"type": "createProxy", "args": {"expiry": p.exp_ms, "owner": MAIN, "proxy": p.proxy}}
    assert body["sig"] == sig and body["ts"] == p.ts_ms and body["salt"] == p.salt
    assert "private" not in json.dumps(body).lower() and p.private_key[2:] not in json.dumps(body)
    from perpbot.envsecrets import load_secrets

    s = load_secrets(root.env_file)
    assert s.proxy_address == p.proxy and s.proxy_secret == "proxy-secret-abc123" and s.wallet_address == MAIN
    env = root.env_file.read_text()
    assert "# my comment" in env and "HEALTHCHECK_PING_URL=https://hc-ping.com/keep-me" in env
    assert not (root.root / pkm.PENDING_NAME).exists()


def test_signature_from_wrong_wallet_or_request_is_refused(root, cfg):
    fx = FakeExchange()
    pkm.new_request(root, cfg, days=30, label="btcperp", now_ms=_now_ms())
    req = json.loads((root.data_dir / "proxykey" / "sign_request.json").read_text())
    _, sig = sign(req, MAIN_KEY)
    with pytest.raises(pkm.ProxyKeyError, match="PM_WALLET_ADDRESS"):
        pkm.finish(root, cfg, sig, expected_owner="0x000000000000000000000000000000000000dEaD", transport=fx.transport())
    with pytest.raises(pkm.ProxyKeyError):
        pkm.finish(root, cfg, "0x1234", transport=fx.transport())
    other = dict(req, message=dict(req["message"], exp=req["message"]["exp"] + 1))
    _, sig_other = sign(other, MAIN_KEY)                    # signs a different message: recovers another address
    with pytest.raises(pkm.ProxyKeyError, match="PM_WALLET_ADDRESS"):
        pkm.finish(root, cfg, sig_other, expected_owner=MAIN, transport=fx.transport())
    assert fx.bodies == [] and (root.root / pkm.PENDING_NAME).exists()


def test_exchange_rejection_keeps_env_unchanged(root, cfg):
    fx = FakeExchange(reject="timestamp too old")
    pkm.new_request(root, cfg, days=30, label="btcperp", now_ms=_now_ms())
    req = json.loads((root.data_dir / "proxykey" / "sign_request.json").read_text())
    _, sig = sign(req, MAIN_KEY)
    before = root.env_file.read_text()
    with pytest.raises(pkm.ProxyKeyError, match="rejected"):
        pkm.finish(root, cfg, sig, transport=fx.transport())
    assert root.env_file.read_text() == before


def test_local_signing_page_round_trip(root, cfg):
    fx = FakeExchange()
    p = pkm.new_request(root, cfg, days=30, label="btcperp", now_ms=_now_ms())
    result = {}
    token = "t0k3n"
    import socket

    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    th = threading.Thread(target=lambda: result.update(pkm.serve_signing(
        root, cfg, p, port=port, open_browser=False, timeout_s=20, transport=fx.transport(), token=token)))
    th.start()
    for _ in range(50):
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", f"/{token}/")
            r = c.getresponse()
            page = r.read().decode()
            break
        except OSError:
            time.sleep(0.1)
    assert r.status == 200 and p.proxy in page and p.private_key[2:] not in page
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/wrong/")
    assert c.getresponse().status == 404
    _, sig = sign(p.typed_data(), MAIN_KEY)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", f"/{token}/sign", body=json.dumps({"signature": sig}), headers={"X-Token": "bad"})
    assert c.getresponse().status == 403
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("POST", f"/{token}/sign", body=json.dumps({"signature": sig}),
              headers={"X-Token": token, "Content-Type": "application/json"})
    answer = json.loads(c.getresponse().read())
    th.join(10)
    assert answer["ok"] is True and result["ok"] is True and answer["proxy"] == p.proxy
    assert "proxy-secret" not in json.dumps(answer)                 # the secret never goes to the page


def test_cli_proxykey_status_and_offline(tmp_root, capsys):
    from perpbot.cli import Factories, main
    from perpbot.timeutil import FixedClock

    from conftest import hkt

    (tmp_root / ".env").write_text("PM_WALLET_ADDRESS=\n", encoding="utf-8")
    f = Factories(registered_root=lambda: None)
    clock = FixedClock(hkt(2026, 10, 5, 12, 0))
    assert main(["proxykey", "status"], paths=Paths(tmp_root), clock=clock, factories=f) == 0
    assert "no pending request" in capsys.readouterr().out
    assert main(["proxykey", "new", "--offline"], paths=Paths(tmp_root), clock=clock, factories=f) == 0
    out = capsys.readouterr().out
    assert "sign.html" in out and "0x" in out
    key = json.loads((tmp_root / pkm.PENDING_NAME).read_text())["private_key"]
    assert key[2:] not in out
    log_text = "".join(p.read_text() for p in (tmp_root / "logs").glob("*.log"))
    assert key[2:] not in log_text


def test_offline_sign_matches_sdk_signer(cfg, tmp_path):
    from polymarket._internal.actions.perps.signing import sign_owner_typed_data

    paths = Paths(tmp_path / "r")
    paths.ensure()
    p = pkm.new_request(paths, cfg, days=7, label="x", now_ms=_now_ms())
    _, sig = sign(p.typed_data(), MAIN_KEY)
    assert sig == sign_owner_typed_data(Account.from_key(MAIN_KEY), p.typed_data(), what="t")
    assert pkm.recover_signer(p, sig) == MAIN
    assert asyncio.iscoroutinefunction(pkm._register)
