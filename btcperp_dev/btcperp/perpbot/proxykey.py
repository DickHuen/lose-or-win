"""Create a Polymarket Perps proxy key WITHOUT the main wallet key ever touching this computer
(review v1.2.0 item 11; hardened per review v1.3.0 P1-P8).

How Polymarket's proxy keys work (official SDK polymarket-client 0.11.0, credentials.create_credentials):
the proxy private key is generated locally; the MAIN wallet only signs the EIP-712 message
CreateProxy{addr, exp, salt, ts} with domain {name: Polymarket, version: 1, chainId: 137}; that signature
goes to POST /v1/account/proxy, which returns the proxy secret.

  `proxykey new --owner 0xMAIN [--days N<=30]`            N: sign in this PC's browser wallet - ONLY with a
        hardware wallet (P1). A one-off page on http://127.0.0.1:<port>/<token>/ builds the message itself
        from the four fields with the fixed domain (P2), the wallet signs, the bot registers and writes .env.
  `proxykey new --owner 0xMAIN --offline`                 O: sign on ANOTHER computer (P5). This PC only writes
        data/proxykey/sign_fields.txt (four plain fields). The other computer uses ITS OWN verified copy of the
        release (offline_sign/offline_sign.html or perpbot/offline_sign.py), which rebuilds the message.
  `proxykey new --owner 0xMAIN --phone`                   P (v1.5.1): sign on your PHONE. The same one-off page is served
        on this PC's home Wi-Fi address (private network only, short one-time link, 15 minutes); the phone's
        MetaMask app opens it and signs. The main wallet key stays on the phone.
  `proxykey finish --signature 0x...`                      register that signature; the signer must be the
        wallet given at `new` (P6).
  `proxykey status`                                        pending request, .env proxy, registered proxies.

The proxy private key lives only in .proxykey_pending.json (deleted after 1 hour unused, P4) and then in .env,
written under the bot's lock (P7). Nothing secret is ever printed or logged.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import secrets as _secrets
import socket
import stat
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable

from perpbot.dashboard import CSP, REQUEST_TIMEOUT_S, DashServer, read_body
from perpbot.paths import Paths

log = logging.getLogger("perpbot.proxykey")

PENDING_NAME = ".proxykey_pending.json"
PENDING_MAX_AGE_S = 3600                  # P4
MAX_DAYS = 30                             # P3
CHAIN_ID = 137
DOMAIN = {"name": "Polymarket", "version": "1", "chainId": CHAIN_ID}
TYPES = {
    "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                     {"name": "chainId", "type": "uint256"}],
    "CreateProxy": [{"name": "addr", "type": "address"}, {"name": "exp", "type": "uint64"},
                    {"name": "salt", "type": "uint64"}, {"name": "ts", "type": "uint64"}],
}
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")


class ProxyKeyError(Exception):
    pass


def build_typed(addr: str, exp: int, salt: int, ts: int) -> dict[str, Any]:
    """The ONLY message this tool ever asks a wallet to sign (P2): fixed type, domain and fields."""
    return {"types": TYPES, "primaryType": "CreateProxy", "domain": dict(DOMAIN),
            "message": {"addr": addr, "exp": int(exp), "salt": int(salt), "ts": int(ts)}}


def check_fields(addr: str, exp: int, salt: int, ts: int) -> None:
    if not _ADDR.match(str(addr)):
        raise ProxyKeyError("proxy address must be 0x + 40 hex characters")
    if not (0 <= int(salt) < 2 ** 32) or int(ts) <= 1_600_000_000_000 or int(exp) <= int(ts):
        raise ProxyKeyError("salt / ts / exp out of range")
    if int(exp) - int(ts) > MAX_DAYS * 86_400_000:
        raise ProxyKeyError(f"expiry more than {MAX_DAYS} days after ts")


@dataclass
class Pending:
    private_key: str
    proxy: str
    exp_ms: int
    salt: int
    ts_ms: int
    chain_id: int
    label: str
    created_utc: str
    owner: str
    method: str                                  # browser (hardware wallet) | offline | phone

    def typed_data(self) -> dict[str, Any]:
        return build_typed(self.proxy, self.exp_ms, self.salt, self.ts_ms)

    def fields(self) -> dict[str, Any]:
        return {"addr": self.proxy, "exp": self.exp_ms, "salt": self.salt, "ts": self.ts_ms}


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hkt(ms: int) -> str:
    return (datetime.fromtimestamp(ms / 1000, tz=timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M HKT")


def _pending_path(paths: Paths) -> Path:
    return paths.root / PENDING_NAME


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if os.name != "nt":
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)


def expire_pending(paths: Paths, now_s: float | None = None) -> bool:
    """P4: an unfinished request older than an hour is deleted (it holds a plain proxy private key)."""
    path = _pending_path(paths)
    if not path.exists():
        return False
    if (now_s if now_s is not None else time.time()) - path.stat().st_mtime > PENDING_MAX_AGE_S:
        path.unlink()
        log.info("expired proxy key request deleted")
        return True
    return False


def new_request(paths: Paths, cfg: Any, *, days: int, label: str, owner: str, method: str,
                expected_owner: str = "", now_ms: int | None = None) -> Pending:
    from eth_account import Account

    if not 1 <= days <= MAX_DAYS:
        raise ProxyKeyError(f"--days must be between 1 and {MAX_DAYS}")
    if not _ADDR.match(owner or ""):
        raise ProxyKeyError("give your MAIN wallet address (0x + 40 hex) with --owner")
    if expected_owner and owner.lower() != expected_owner.lower():
        raise ProxyKeyError(f"{owner} differs from PM_WALLET_ADDRESS in .env ({expected_owner}): ask Claude before "
                            f"changing the main wallet")
    if int(cfg.polymarket.chain_id) != CHAIN_ID:
        raise ProxyKeyError(f"unexpected chain id {cfg.polymarket.chain_id}")
    if method not in ("browser", "offline", "phone"):
        raise ProxyKeyError("method must be browser, offline or phone")
    pk = "0x" + _secrets.token_bytes(32).hex()
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    p = Pending(private_key=pk, proxy=Account.from_key(pk).address, exp_ms=ts + days * 86_400_000,
                salt=_secrets.randbits(32), ts_ms=ts, chain_id=CHAIN_ID, label=label, created_utc=_utc(ts),
                owner=owner, method=method)
    _write_private(_pending_path(paths), json.dumps(asdict(p)))
    out = paths.data_dir / "proxykey"
    out.mkdir(parents=True, exist_ok=True)
    for old in ("sign_request.json", "sign.html", "sign_fields.txt"):   # stale / v1.3.0 files never travel
        if (out / old).exists():
            (out / old).unlink()
    if method == "offline":
        (out / "sign_fields.txt").write_text(
            f"# btcperp proxy key request - plain fields only, nothing secret. Expires {_hkt(p.exp_ms)}.\n"
            f"addr={p.proxy}\nexp={p.exp_ms}\nsalt={p.salt}\nts={p.ts_ms}\nowner={owner}\n", encoding="utf-8")
    return p


def load_pending(paths: Paths) -> Pending | None:
    expire_pending(paths)
    path = _pending_path(paths)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if "owner" not in data:                                    # a v1.3.0 request: start again
        path.unlink()
        return None
    return Pending(**data)


def recover_signer(p: Pending, signature: str) -> str:
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    sig = signature.strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{130}", sig):
        raise ProxyKeyError("the signature must be 0x followed by 130 hex characters")
    try:
        return Account.recover_message(encode_typed_data(full_message=p.typed_data()), signature=sig)
    except Exception as e:  # noqa: BLE001
        raise ProxyKeyError(f"signature does not match this request: {e}") from e


async def _register(p: Pending, signature: str, owner: str, rest_url: str, transport: Any = None) -> Any:
    from polymarket._internal.actions.perps.credentials import validate_credentials
    from polymarket._internal.actions.perps.paging import as_json_dict
    from polymarket.clients._transport import AsyncTransport
    from polymarket.models.perps.credentials import PerpsCredentials

    t = transport or AsyncTransport(base_url=rest_url)
    try:
        body: dict[str, Any] = {"op": {"type": "createProxy", "args": {"expiry": p.exp_ms, "owner": owner, "proxy": p.proxy}},
                                "salt": p.salt, "sig": signature, "ts": p.ts_ms}
        if p.label:
            body["label"] = p.label
        resp = as_json_dict(await t.post_json("/v1/account/proxy", json=body))
        secret = resp.get("secret") if resp is not None else None
        if not isinstance(secret, str) or not secret:
            raise ProxyKeyError("the exchange did not return a proxy secret")
        creds = PerpsCredentials(proxy=p.proxy, private_key=p.private_key, secret=secret,
                                 expires_at=datetime.fromtimestamp(p.exp_ms / 1000, tz=timezone.utc))
        return await validate_credentials(t, signer_address=owner, credentials=creds)
    finally:
        if transport is None:
            await t.close()


def write_env(env_path: Path, values: dict[str, str]) -> None:
    """Replace or append NAME=value lines; every other line (comments, other keys) is kept."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    done: set[str] = set()
    out = []
    for line in lines:
        name = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if name in values:
            out.append(f"{name}={values[name]}")
            done.add(name)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in done]
    _write_private(env_path, "\n".join(out) + "\n")


def finish(paths: Paths, cfg: Any, signature: str, *, transport: Any = None,
           with_lock: Callable[[Callable[[], Any]], Any] | None = None) -> dict[str, Any]:
    p = load_pending(paths)
    if p is None:
        raise ProxyKeyError("no pending proxy key request (none, or older than 1 hour): run Proxy_Key.bat again")
    owner = recover_signer(p, signature)
    if owner.lower() != p.owner.lower():                     # P6
        raise ProxyKeyError(f"signed by {owner}, not by the main wallet {p.owner} given for this request")

    def register_and_write() -> str:
        """P7: registration and the .env write happen together, never while a bot command runs."""
        if not _pending_path(paths).exists():
            raise ProxyKeyError("this request was already finished or expired")
        try:
            creds = asyncio.run(_register(p, signature.strip(), p.owner, str(cfg.polymarket.rest_url), transport))
        except ProxyKeyError:
            raise
        except Exception as e:  # noqa: BLE001 - SDK errors never contain the key
            raise ProxyKeyError(f"the exchange rejected the request: {type(e).__name__}: {e}. If it mentions time or "
                                f"timestamp, sign faster (run the tool again); if it mentions expiry, use fewer "
                                f"--days") from e
        expires = creds.expires_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_env(paths.env_file, {"PM_PROXY_PRIVATE_KEY": p.private_key, "PM_PROXY_SECRET": creds.secret,
                                   "PM_WALLET_ADDRESS": p.owner, "PM_PROXY_EXPIRES_AT": expires})
        _pending_path(paths).unlink()
        return expires

    expires = (with_lock or (lambda fn: fn()))(register_and_write)
    hist = paths.data_dir / "proxykey" / "history.jsonl"
    hist.parent.mkdir(parents=True, exist_ok=True)
    with hist.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"registered_utc": _utc(int(time.time() * 1000)), "proxy": p.proxy, "owner": p.owner,
                             "expires_utc": expires, "method": p.method}) + "\n")
    return {"proxy": p.proxy, "owner": p.owner, "expires_utc": expires, "method": p.method}


def registered_proxies(cfg: Any, secrets: Any, transport: Any = None) -> list[dict[str, Any]]:
    """All proxies registered for the owner (P7: the old one stays valid until it expires; revoking it needs a
    main-wallet signature, which never happens on this computer)."""
    from polymarket._internal.actions.perps.credentials import credential_headers
    from polymarket.clients._transport import AsyncTransport
    from polymarket.models.perps.account import PerpsCredentialsInfo
    from polymarket.models.perps.credentials import PerpsCredentials

    async def go() -> list[dict[str, Any]]:
        t = transport or AsyncTransport(base_url=str(cfg.polymarket.rest_url))
        try:
            creds = PerpsCredentials(proxy=secrets.proxy_address, private_key=secrets.proxy_private_key,
                                     secret=secrets.proxy_secret,
                                     expires_at=secrets.proxy_expires_at or datetime(2100, 1, 1, tzinfo=timezone.utc))
            info = PerpsCredentialsInfo.parse_response(await t.get_json("/v1/account/credentials",
                                                                        headers=credential_headers(creds)))
            return [{"proxy": k.proxy, "label": k.label, "expires_utc": k.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "in_env": k.proxy.lower() == secrets.proxy_address.lower()} for k in info.keys]
        finally:
            if transport is None:
                await t.close()

    return asyncio.run(go())


def status_lines(paths: Paths, cfg: Any, secrets: Any, transport: Any = None) -> list[str]:
    lines = []
    if expire_pending(paths):
        lines.append("an unfinished request older than 1 hour was deleted")
    p = load_pending(paths)
    if p:
        age = (time.time() * 1000 - p.ts_ms) / 60_000
        lines.append(f"pending request ({p.method}): proxy {p.proxy} for wallet {p.owner}, expires {_hkt(p.exp_ms)}, "
                     f"created {age:.0f} min ago (deleted after 60 min)")
    else:
        lines.append("no pending request")
    if secrets.proxy_address:
        exp = secrets.proxy_expires_at.isoformat() if secrets.proxy_expires_at else "unknown"
        lines.append(f".env proxy: {secrets.proxy_address}, owner {secrets.wallet_address or '?'}, expires {exp}")
        try:
            for k in registered_proxies(cfg, secrets, transport):
                lines.append(f"  registered: {k['proxy']} expires {k['expires_utc']}{'  (in .env)' if k['in_env'] else ''}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"  registered proxies: unavailable ({type(e).__name__})")
    else:
        lines.append(".env has no proxy key yet")
    return lines


# ---------------------------------------------------------------- one-off signing page (options N and P)

PHONE_TOKEN_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"          # no 0/o/1/l/i: easy to type on a phone
PHONE_TOKEN_LEN = 10                                           # 31^10 ~ 8e14 guesses; the page lives 15 min


HOME_NETWORKS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def is_private_lan(ip: str) -> bool:
    """A home-network address (RFC 1918: 10/8, 172.16/12, 192.168/16): never a public, loopback or other one."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and any(a in n for n in HOME_NETWORKS)


def lan_ip() -> str | None:
    """This PC's address on the home network (the interface that routes outward; a UDP connect sends nothing)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))                      # TEST-NET-1: never actually contacted
            ip = s.getsockname()[0]
    except OSError:
        return None
    return ip if is_private_lan(ip) else None


def phone_token() -> str:
    return "".join(_secrets.choice(PHONE_TOKEN_CHARS) for _ in range(PHONE_TOKEN_LEN))


def serve_signing(paths: Paths, cfg: Any, p: Pending, *, port: int, open_browser: bool, timeout_s: float = 900.0,
                  transport: Any = None, token: str | None = None,
                  with_lock: Callable[[Callable[[], Any]], Any] | None = None,
                  host: str = "127.0.0.1") -> dict[str, Any]:
    """`host` 127.0.0.1: this PC's browser (option N). A home-network address: the phone on the same Wi-Fi
    (option P); only that address:port is accepted as Host, the link carries a one-time token, one submission."""
    token = token or _secrets.token_hex(16)
    result: dict[str, Any] = {}
    done = threading.Event()
    gate = threading.Lock()                                   # P8: only one POST can ever call finish()
    loopback = host.startswith("127.") or host == "localhost"
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"} if host == "127.0.0.1" else {f"{host}:{port}"}

    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT_S

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug(fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.headers.get("Host", "") not in allowed or self.path != f"/{token}/":
                return self._send(404, b"not found", "text/plain")
            self._send(200, sign_page(p.fields(), p.owner, token=token, post_back=True).encode("utf-8"),
                       "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            body = read_body(self)                            # v1.5.4: before any answer (WinError 10053)
            if (self.headers.get("Host", "") not in allowed or self.path != f"/{token}/sign"
                    or self.headers.get("X-Token") != token):
                return self._send(403, b"forbidden", "text/plain")
            if not gate.acquire(blocking=False):
                return self._send(409, b'{"ok":false,"error":"already submitted"}', "application/json")
            try:
                sig = json.loads(body or b"{}").get("signature", "")
                res = finish(paths, cfg, sig, transport=transport, with_lock=with_lock)
                result.update(res, ok=True)
            except Exception as e:  # noqa: BLE001
                result.update(ok=False, error=str(e))
            done.set()
            self._send(200, json.dumps(result).encode(), "application/json")

    if not loopback and not is_private_lan(host):
        raise ProxyKeyError(f"{host} is not a home-network address: the signing page is never served publicly")
    httpd = DashServer((host, port), Handler)
    url = f"http://{host}:{port}/{token}/"
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    print(f"Signing page: {url}")
    if open_browser and loopback:
        webbrowser.open(url)
    try:
        if not done.wait(timeout_s):
            result.update(ok=False, error=f"no signature within {timeout_s / 60:.0f} minutes; run the tool again")
    except KeyboardInterrupt:
        result.update(ok=False, error="cancelled")
    finally:
        time.sleep(0.5)          # let the page receive the answer
        httpd.shutdown()
        httpd.server_close()
    return result


def _js_json(x: Any) -> str:
    return json.dumps(x).replace("</", "<\\/")                    # P8: never close the script tag


def sign_page(fields: dict[str, Any], owner: str, *, token: str, post_back: bool) -> str:
    return (_PAGE.replace("__FIELDS__", _js_json(fields)).replace("__OWNER__", _js_json(owner))
            .replace("__TOKEN__", _js_json(token)).replace("__POST__", "true" if post_back else "false"))


# The page builds the message ITSELF from the four fields with the fixed domain and types (P2).
_PAGE = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>btcperp proxy key</title><link rel="icon" href="data:,">
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mut:#6b7385;--line:#e3e6ec;--ok:#12805c;--bad:#c2352b;--acc:#2f5bd3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b23;--fg:#e6e9ef;--mut:#98a1b3;--line:#2a303c;--ok:#3fbf8f;--bad:#ef6b61;--acc:#7aa0ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,"Segoe UI","Microsoft JhengHei",sans-serif}
main{max-width:640px;margin:32px auto;padding:0 16px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:14px}
h1{font-size:20px;margin:0 0 6px}.mut{color:var(--mut)}code{word-break:break-all}button{font:inherit;padding:10px 18px;border-radius:8px;border:0;background:var(--acc);color:#fff;cursor:pointer}
#out{white-space:pre-wrap;word-break:break-all}.ok{color:var(--ok)}.bad{color:var(--bad)}
</style></head><body><main>
<div class="card"><h1>btcperp：授權 proxy key</h1>
<div class="mut">用你嘅<b>主錢包</b>（硬件錢包，或者手機 MetaMask）簽一個訊息，授權下面呢個 proxy 地址代你落單。主錢包私鑰唔會離開你個錢包。</div></div>
<div class="card"><div>主錢包：<code id="owner"></code></div><div>Proxy 地址：<code id="proxy"></code></div><div>到期：<span id="exp"></span></div>
<div class="mut">網絡：Polygon（chain 137）。錢包會顯示 CreateProxy、Polymarket：addr 要同上面 proxy 地址（亦即 bot 電腦顯示嗰個）一樣，否則唔好簽。如果錢包顯示 Permit、Approve、轉賬或者其他內容，一定唔好簽。</div></div>
<div class="card"><button id="go">用錢包簽名</button><p id="out" class="mut"></p></div>
</main><script>
const F=__FIELDS__, OWNER=__OWNER__, TOKEN=__TOKEN__, POST=__POST__;
const TYPES={EIP712Domain:[{name:"name",type:"string"},{name:"version",type:"string"},{name:"chainId",type:"uint256"}],
 CreateProxy:[{name:"addr",type:"address"},{name:"exp",type:"uint64"},{name:"salt",type:"uint64"},{name:"ts",type:"uint64"}]};
const TYPED={types:TYPES,primaryType:"CreateProxy",domain:{name:"Polymarket",version:"1",chainId:137},
 message:{addr:F.addr,exp:Number(F.exp),salt:Number(F.salt),ts:Number(F.ts)}};
const POLYGON={chainId:"0x89",chainName:"Polygon Mainnet",nativeCurrency:{name:"POL",symbol:"POL",decimals:18},
 rpcUrls:["https://polygon-rpc.com"],blockExplorerUrls:["https://polygonscan.com"]};
const $=id=>document.getElementById(id), show=(t,c)=>{$("out").textContent=t;$("out").className=c||"mut"};
const hkt=ms=>new Date(Number(ms)+8*3600e3).toISOString().slice(0,16).replace("T"," ")+" HKT";
$("owner").textContent=OWNER;$("proxy").textContent=F.addr;$("exp").textContent=hkt(F.exp);
$("go").onclick=async()=>{
 try{
  if(!/^0x[0-9a-fA-F]{40}$/.test(F.addr)||Number(F.exp)-Number(F.ts)>30*86400e3){show("請求內容唔正常，唔好簽。","bad");return}
  if(!window.ethereum){show("搵唔到錢包。電腦：用裝咗 MetaMask 嘅瀏覽器。手機：要喺 MetaMask App 入面嘅瀏覽器開呢個網址。","bad");return}
  const [acct]=await ethereum.request({method:"eth_requestAccounts"});
  if(acct.toLowerCase()!==OWNER.toLowerCase()){show("錢包帳戶 "+acct+" 唔係主錢包 "+OWNER+"：請喺錢包切換帳戶。","bad");return}
  if((await ethereum.request({method:"eth_chainId"})).toLowerCase()!=="0x89"){
   try{await ethereum.request({method:"wallet_switchEthereumChain",params:[{chainId:"0x89"}]})}
   catch(e){
    try{await ethereum.request({method:"wallet_addEthereumChain",params:[POLYGON]})}
    catch(e2){show("請先喺錢包切換到 Polygon 網絡，再撳一次。","bad");return}}}
  show("請喺錢包確認簽名（檢查係 CreateProxy、Polymarket，addr 同上面一樣）…");
  const sig=await ethereum.request({method:"eth_signTypedData_v4",params:[acct,JSON.stringify(TYPED)]});
  if(!POST){show("簽名（抄返去 bot 電腦，喺 Proxy_Key.bat 揀 F 貼上）：\n"+sig,"ok");return}
  show("已簽名，正在向交易所登記…");
  const r=await fetch("sign",{method:"POST",headers:{"X-Token":TOKEN,"Content-Type":"application/json"},body:JSON.stringify({signature:sig})});
  const j=await r.json();
  if(j.ok)show("完成！proxy "+j.proxy+" 已登記，到期 "+j.expires_utc+"。\n.env 已更新。可以關閉呢頁，然後行 2_Smoketest.bat。","ok");
  else show("失敗："+j.error,"bad");
 }catch(e){show("錢包錯誤："+(e&&e.message?e.message:e),"bad")}
};
</script></body></html>
"""
