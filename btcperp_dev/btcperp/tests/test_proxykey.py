"""Proxy key tool (review v1.2.0 item 11, hardened per review v1.3.0 P1-P8): the main wallet only signs one
fixed CreateProxy message; the proxy key never leaves this computer."""

import asyncio
import http.client
import json
import os
import socket
import threading
import time

import httpx
import pytest
from eth_account import Account

from perpbot import offline_sign as osg
from perpbot import proxykey as pkm
from perpbot.paths import Paths

MAIN_KEY = "0x" + "11" * 32
MAIN = Account.from_key(MAIN_KEY).address
OTHER_KEY = "0x" + "22" * 32
OTHER = Account.from_key(OTHER_KEY).address


class FakeExchange:
    """httpx MockTransport standing in for POST /v1/account/proxy and GET /v1/account/credentials."""

    def __init__(self, reject: str = "", delay_s: float = 0.0):
        self.reject = reject
        self.delay_s = delay_s
        self.bodies = []
        self.registered: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/account/proxy":
            time.sleep(self.delay_s)
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


def _new(root, cfg, *, method="offline", days=30, owner=MAIN):
    return pkm.new_request(root, cfg, days=days, label="btcperp", owner=owner, method=method, now_ms=_now_ms())


def _fields_file(root):
    return root.data_dir / "proxykey" / "sign_fields.txt"


def _sign_from_fields(root, key=MAIN_KEY):
    """What happens on the OTHER computer: only the plain fields travel."""
    fields = osg.parse_fields(_fields_file(root).read_text(encoding="utf-8"))
    return osg.sign(osg.build(fields), key)


def _free_port():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


# ---------------------------------------------------------------- requests (P3, P4, P5)

def test_new_request_writes_only_plain_fields(root, cfg):
    p = _new(root, cfg)
    pending = root.root / pkm.PENDING_NAME
    assert pending.exists() and p.private_key in pending.read_text(encoding="utf-8")
    if os.name != "nt":
        assert oct(pending.stat().st_mode & 0o777) == "0o600"
    text = _fields_file(root).read_text(encoding="utf-8")
    assert p.private_key[2:] not in text
    lines = dict(line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#"))
    assert lines == {"addr": p.proxy, "exp": str(p.exp_ms), "salt": str(p.salt), "ts": str(p.ts_ms), "owner": MAIN}
    assert p.exp_ms - p.ts_ms == 30 * 86_400_000
    out = root.data_dir / "proxykey"
    assert not (out / "sign_request.json").exists() and not (out / "sign.html").exists()
    # a browser (hardware wallet) request writes no file to carry, and removes stale ones
    (out / "sign_request.json").write_text("{}", encoding="utf-8")
    _new(root, cfg, method="browser")
    assert not _fields_file(root).exists() and not (out / "sign_request.json").exists()


def test_request_limits_and_owner(root, cfg):
    with pytest.raises(pkm.ProxyKeyError, match="between 1 and 30"):
        _new(root, cfg, days=31)
    with pytest.raises(pkm.ProxyKeyError, match="between 1 and 30"):
        _new(root, cfg, days=0)
    with pytest.raises(pkm.ProxyKeyError, match="--owner"):
        _new(root, cfg, owner="")
    with pytest.raises(pkm.ProxyKeyError, match="--owner"):
        _new(root, cfg, owner="0x1234")
    with pytest.raises(pkm.ProxyKeyError, match="PM_WALLET_ADDRESS"):
        pkm.new_request(root, cfg, days=30, label="x", owner=OTHER, method="offline", expected_owner=MAIN)
    assert not (root.root / pkm.PENDING_NAME).exists()


def test_pending_request_expires_after_an_hour(root, cfg):
    fx = FakeExchange()
    _new(root, cfg)
    _, sig = _sign_from_fields(root)
    pending = root.root / pkm.PENDING_NAME
    old = time.time() - 2 * 3600
    os.utime(pending, (old, old))
    assert pkm.load_pending(root) is None and not pending.exists()
    with pytest.raises(pkm.ProxyKeyError, match="older than 1 hour"):
        pkm.finish(root, cfg, sig, transport=fx.transport())
    assert fx.bodies == []
    _new(root, cfg)
    assert pkm.expire_pending(root, now_s=time.time() + 30 * 60) is False and pending.exists()
    assert pkm.expire_pending(root, now_s=time.time() + 61 * 60) is True and not pending.exists()


def test_v130_request_is_discarded(root, cfg):
    (root.root / pkm.PENDING_NAME).write_text(json.dumps({"private_key": "0x" + "33" * 32, "proxy": MAIN}), encoding="utf-8")
    assert pkm.load_pending(root) is None and not (root.root / pkm.PENDING_NAME).exists()


# ---------------------------------------------------------------- finishing (P6, P7)

def test_offline_fields_register_and_write_env(root, cfg):
    fx = FakeExchange()
    p = _new(root, cfg)
    signer, sig = _sign_from_fields(root)
    assert signer == MAIN
    calls = []

    def with_lock(fn):
        calls.append("lock")
        out = fn()
        calls.append("unlock")
        return out

    res = pkm.finish(root, cfg, sig, transport=fx.transport(), with_lock=with_lock)
    assert calls == ["lock", "unlock"]
    assert res == {"proxy": p.proxy, "owner": MAIN, "expires_utc": res["expires_utc"], "method": "offline"}
    body = fx.bodies[0]
    assert body["op"] == {"type": "createProxy", "args": {"expiry": p.exp_ms, "owner": MAIN, "proxy": p.proxy}}
    assert body["sig"] == sig and body["ts"] == p.ts_ms and body["salt"] == p.salt
    assert "private" not in json.dumps(body).lower() and p.private_key[2:] not in json.dumps(body)
    from perpbot.envsecrets import load_secrets

    s = load_secrets(root.env_file)
    assert s.proxy_address == p.proxy and s.proxy_secret == "proxy-secret-abc123" and s.wallet_address == MAIN
    env = root.env_file.read_text(encoding="utf-8")
    assert "# my comment" in env and "HEALTHCHECK_PING_URL=https://hc-ping.com/keep-me" in env
    assert not (root.root / pkm.PENDING_NAME).exists()
    hist = [json.loads(x) for x in (root.data_dir / "proxykey" / "history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert hist[-1]["proxy"] == p.proxy and hist[-1]["method"] == "offline"
    assert p.private_key[2:] not in json.dumps(hist) and "proxy-secret" not in json.dumps(hist)


def test_signature_from_another_wallet_or_message_is_refused(root, cfg):
    fx = FakeExchange()
    p = _new(root, cfg)
    signer, sig_other_wallet = _sign_from_fields(root, key=OTHER_KEY)
    assert signer == OTHER
    with pytest.raises(pkm.ProxyKeyError, match="not by the main wallet"):
        pkm.finish(root, cfg, sig_other_wallet, transport=fx.transport())
    with pytest.raises(pkm.ProxyKeyError, match="130 hex"):
        pkm.finish(root, cfg, "0x1234", transport=fx.transport())
    changed = osg.build(dict(p.fields(), exp=p.exp_ms - 1))
    _, sig_changed = osg.sign(changed, MAIN_KEY)             # a different message recovers a different address
    with pytest.raises(pkm.ProxyKeyError, match="not by the main wallet"):
        pkm.finish(root, cfg, sig_changed, transport=fx.transport())
    assert fx.bodies == [] and (root.root / pkm.PENDING_NAME).exists()


def test_exchange_rejection_keeps_env_unchanged(root, cfg):
    fx = FakeExchange(reject="timestamp too old")
    _new(root, cfg)
    _, sig = _sign_from_fields(root)
    before = root.env_file.read_text(encoding="utf-8")
    with pytest.raises(pkm.ProxyKeyError, match="rejected"):
        pkm.finish(root, cfg, sig, transport=fx.transport())
    assert root.env_file.read_text(encoding="utf-8") == before and (root.root / pkm.PENDING_NAME).exists()


def test_registration_waits_for_the_bot_lock(root, cfg):
    """P7: while a bot command holds the lock nothing is registered and .env is untouched."""
    from perpbot.lockfile import FileLock, LockTimeout

    fx = FakeExchange()
    _new(root, cfg)
    _, sig = _sign_from_fields(root)
    before = root.env_file.read_text(encoding="utf-8")
    held = FileLock(root.lock_file)
    held.acquire(0)

    def with_lock(fn):
        lock = FileLock(root.lock_file)
        lock.acquire(0.2, poll=0.05)
        try:
            return fn()
        finally:
            lock.release()

    try:
        with pytest.raises(LockTimeout):
            pkm.finish(root, cfg, sig, transport=fx.transport(), with_lock=with_lock)
    finally:
        held.release()
    assert fx.bodies == [] and root.env_file.read_text(encoding="utf-8") == before
    res = pkm.finish(root, cfg, sig, transport=fx.transport(), with_lock=with_lock)
    assert res["owner"] == MAIN and len(fx.bodies) == 1


def test_status_lists_registered_proxies(root, cfg):
    from perpbot.envsecrets import load_secrets

    fx = FakeExchange()
    p = _new(root, cfg)
    _, sig = _sign_from_fields(root)
    pkm.finish(root, cfg, sig, transport=fx.transport())
    fx.registered["0x" + "ab" * 20] = _now_ms() + 86_400_000        # an older key, still valid until it expires
    lines = pkm.status_lines(root, cfg, load_secrets(root.env_file), transport=fx.transport())
    text = "\n".join(lines)
    assert "no pending request" in text and f".env proxy: {p.proxy}" in text
    assert f"registered: {p.proxy}" in text and "(in .env)" in text and "0x" + "ab" * 20 in text
    assert p.private_key[2:] not in text and "proxy-secret" not in text


# ---------------------------------------------------------------- the offline signer (P2, P5)

def test_offline_builder_matches_the_sdk(root, cfg):
    from polymarket._internal.actions.perps.signing import (build_perps_create_proxy_typed_data,
                                                            sign_owner_typed_data)

    p = _new(root, cfg, days=7)
    sdk = build_perps_create_proxy_typed_data(chain_id=137, proxy=p.proxy, expires_at_ms=p.exp_ms, salt=p.salt,
                                              timestamp_ms=p.ts_ms)
    fields = osg.parse_fields(_fields_file(root).read_text(encoding="utf-8"))
    assert osg.build(fields) == sdk == p.typed_data() == pkm.build_typed(p.proxy, p.exp_ms, p.salt, p.ts_ms)
    _, sig = osg.sign(sdk, MAIN_KEY)
    assert sig == sign_owner_typed_data(Account.from_key(MAIN_KEY), sdk, what="t")
    assert pkm.recover_signer(p, sig) == MAIN
    assert osg.DOMAIN == pkm.DOMAIN and osg.TYPES == pkm.TYPES and osg.MAX_DAYS == pkm.MAX_DAYS
    assert asyncio.iscoroutinefunction(pkm._register)


def test_offline_signer_refuses_anything_but_create_proxy(root, cfg):
    p = _new(root, cfg)
    good = p.typed_data()
    permit = {"types": {"EIP712Domain": good["types"]["EIP712Domain"],
                        "Permit": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"},
                                   {"name": "value", "type": "uint256"}, {"name": "nonce", "type": "uint256"},
                                   {"name": "deadline", "type": "uint256"}]},
              "primaryType": "Permit", "domain": good["domain"],
              "message": {"owner": MAIN, "spender": OTHER, "value": 10 ** 30, "nonce": 0, "deadline": 2 ** 40}}
    bad = [permit,
           dict(good, domain=dict(good["domain"], chainId=1)),
           dict(good, domain=dict(good["domain"], verifyingContract=OTHER)),
           dict(good, domain=dict(good["domain"], name="Polymarket CTF Exchange")),
           dict(good, message=dict(good["message"], extra=1)),
           dict(good, types=dict(good["types"], CreateProxy=good["types"]["CreateProxy"][:3])),
           dict(good, message=dict(good["message"], exp=good["message"]["ts"] + 31 * 86_400_000))]
    for req in bad:
        with pytest.raises(osg.RequestError):
            osg.sign(req, MAIN_KEY)
    with pytest.raises(osg.RequestError, match="refused"):
        osg.parse_fields(json.dumps(permit))
    with pytest.raises(osg.RequestError, match="unknown field"):
        osg.parse_fields(_fields_file(root).read_text(encoding="utf-8") + "verifyingContract=" + OTHER + "\n")
    with pytest.raises(osg.RequestError, match="missing"):
        osg.parse_fields("addr=" + p.proxy + "\n")
    assert osg.parse_fields(json.dumps(good)) == p.fields()        # a genuine full request is accepted


def test_offline_signer_script(root, cfg, capsys):
    _new(root, cfg)
    f = str(_fields_file(root))
    assert osg.main([f], ask=lambda _: "no", secret=lambda _: MAIN_KEY) == 1
    assert "Cancelled" in capsys.readouterr().out
    assert osg.main([f], ask=lambda _: "YES", secret=lambda _: OTHER_KEY) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "signature:" not in out and OTHER_KEY[2:] not in out
    assert osg.main([f], ask=lambda _: "YES", secret=lambda _: "not-a-key") == 1
    assert "FAILED" in capsys.readouterr().out
    assert osg.main([f], ask=lambda _: "YES", secret=lambda _: MAIN_KEY) == 0
    out = capsys.readouterr().out
    assert f"signed by: {MAIN}" in out and "HKT" in out and MAIN_KEY[2:] not in out
    sig = out.split("signature: ")[1].split()[0]
    fx = FakeExchange()
    assert pkm.finish(root, cfg, sig, transport=fx.transport())["owner"] == MAIN
    assert osg.main([f], ask=lambda _: "YES", secret=lambda _: MAIN_KEY, now_ms=_now_ms() + 31 * 86_400_000) == 1
    assert "expired" in capsys.readouterr().out


def test_static_offline_page_is_self_contained():
    from conftest import ROOT

    html = (ROOT / "offline_sign" / "offline_sign.html").read_text(encoding="utf-8")
    assert 'DOMAIN={name:"Polymarket",version:"1",chainId:137}' in html
    assert "connect-src 'none'" in html and "fetch(" not in html and "http.server" in html
    assert "eth_signTypedData_v4" in html and "Permit" not in html


# ---------------------------------------------------------------- the local signing page (P2, P8)

def test_signing_page_builds_the_message_itself_and_escapes():
    evil = {"addr": "</script><script>alert(1)</script>", "exp": 1, "salt": 2, "ts": 3}
    page = pkm.sign_page(evil, "</script>", token="t", post_back=True)
    assert page.count("</script>") == 1 and "<\\/script>" in page
    assert 'domain:{name:"Polymarket",version:"1",chainId:137}' in page and "primaryType:\"CreateProxy\"" in page
    assert '"types"' not in page and '"domain"' not in page         # no typed data is sent to the page


def test_local_signing_page_round_trip_and_single_submit(root, cfg):
    fx = FakeExchange(delay_s=0.3)
    p = _new(root, cfg, method="browser")
    result = {}
    token = "t0k3n"
    port = _free_port()
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
    assert r.status == 200 and p.proxy in page and MAIN in page and p.private_key[2:] not in page
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/wrong/")
    assert c.getresponse().status == 404
    _, sig = osg.sign(p.typed_data(), MAIN_KEY)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", f"/{token}/sign", body=json.dumps({"signature": sig}), headers={"X-Token": "bad"})
    assert c.getresponse().status == 403
    answers = []

    def post():
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request("POST", f"/{token}/sign", body=json.dumps({"signature": sig}),
                  headers={"X-Token": token, "Content-Type": "application/json"})
        resp = c.getresponse()
        answers.append((resp.status, json.loads(resp.read())))

    posts = [threading.Thread(target=post) for _ in range(2)]
    for t in posts:
        t.start()
    for t in posts:
        t.join(15)
    th.join(10)
    assert sorted(s for s, _ in answers) == [200, 409]                # P8: only one submission is processed
    ok = [a for s, a in answers if s == 200][0]
    assert ok["ok"] is True and result["ok"] is True and ok["proxy"] == p.proxy
    assert len(fx.bodies) == 1
    assert "proxy-secret" not in json.dumps(answers)                  # the secret never goes to the page


# ---------------------------------------------------------------- CLI

def test_cli_proxykey_status_offline_and_limits(tmp_root, capsys):
    from perpbot.cli import Factories, main
    from perpbot.timeutil import FixedClock

    from conftest import hkt

    (tmp_root / ".env").write_text(f"PM_WALLET_ADDRESS={MAIN}\n", encoding="utf-8")
    f = Factories(registered_root=lambda: None)
    clock = FixedClock(hkt(2026, 10, 5, 12, 0))
    paths = Paths(tmp_root)
    assert main(["proxykey", "status"], paths=paths, clock=clock, factories=f) == 0
    assert "no pending request" in capsys.readouterr().out
    assert main(["proxykey", "new", "--offline", "--owner", MAIN, "--days", "31"], paths=paths, clock=clock,
                factories=f) == 1
    assert "between 1 and 30" in capsys.readouterr().out
    assert main(["proxykey", "new", "--offline", "--owner", OTHER], paths=paths, clock=clock, factories=f) == 1
    assert "PM_WALLET_ADDRESS" in capsys.readouterr().out
    assert main(["proxykey", "new", "--offline", "--owner", MAIN], paths=paths, clock=clock, factories=f) == 0
    out = capsys.readouterr().out
    assert "sign_fields.txt" in out and "SHA-256" in out and "HKT" in out
    key = json.loads((tmp_root / pkm.PENDING_NAME).read_text(encoding="utf-8"))["private_key"]
    assert key[2:] not in out
    assert main(["proxykey", "status"], paths=paths, clock=clock, factories=f) == 0
    assert "pending request (offline)" in capsys.readouterr().out
    log_text = "".join(p.read_text(encoding="utf-8") for p in (tmp_root / "logs").glob("*.log"))
    assert key[2:] not in log_text
    # P4: any bot command deletes a request older than an hour
    old = time.time() - 2 * 3600
    os.utime(tmp_root / pkm.PENDING_NAME, (old, old))
    main(["alerts"], paths=paths, clock=clock, factories=f)
    assert not (tmp_root / pkm.PENDING_NAME).exists()


def test_cli_finish_refuses_while_the_bot_runs(tmp_root, capsys, monkeypatch):
    from perpbot.cli import Factories, main
    from perpbot.lockfile import FileLock
    from perpbot.timeutil import FixedClock

    from conftest import hkt

    cfg_file = tmp_root / "config" / "config.yaml"
    cfg_file.write_text(cfg_file.read_text(encoding="utf-8").replace("wait_seconds: 900", "wait_seconds: 0"),
                        encoding="utf-8")
    (tmp_root / ".env").write_text(f"PM_WALLET_ADDRESS={MAIN}\n", encoding="utf-8")
    f = Factories(registered_root=lambda: None)
    clock = FixedClock(hkt(2026, 10, 5, 12, 0))
    paths = Paths(tmp_root)
    assert main(["proxykey", "new", "--offline", "--owner", MAIN], paths=paths, clock=clock, factories=f) == 0
    _, sig = _sign_from_fields(paths)
    registered = []

    async def fake_register(*a, **k):
        registered.append(1)
        raise AssertionError("must not register while the bot runs")

    monkeypatch.setattr(pkm, "_register", fake_register)
    held = FileLock(paths.lock_file)
    held.acquire(0)
    try:
        before = (tmp_root / ".env").read_text(encoding="utf-8")
        assert main(["proxykey", "finish", "--signature", sig], paths=paths, clock=clock, factories=f) == 1
    finally:
        held.release()
    assert "a bot command is running" in capsys.readouterr().out
    assert registered == [] and (tmp_root / ".env").read_text(encoding="utf-8") == before and (tmp_root / pkm.PENDING_NAME).exists()
