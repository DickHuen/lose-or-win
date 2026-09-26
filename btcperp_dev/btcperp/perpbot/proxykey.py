"""Create a Polymarket Perps proxy key WITHOUT the main wallet key ever touching this computer
(review v1.2.0 item 11).

How Polymarket's proxy keys work (official SDK polymarket-client 0.11.0, credentials.create_credentials):
the proxy private key is generated locally; the MAIN wallet only signs an EIP-712 message
CreateProxy{addr: proxy address, exp, salt, ts} (domain Polymarket / 1 / chainId 137); that signature is
sent to POST /v1/account/proxy, which returns the proxy secret. This module splits those steps:

  1. `proxykey new`     generate the proxy key HERE (saved only in .proxykey_pending.json next to .env)
                        and serve a one-off signing page on http://127.0.0.1:<port>/<token>/ . The page asks
                        your browser wallet (MetaMask, ideally with a hardware wallet) to sign the message,
                        sends the signature back to this program, which registers the proxy and writes .env.
     `proxykey new --offline`  only write data/proxykey/sign_request.json + sign.html, to sign on ANOTHER
                        computer (browser wallet, or offline_sign.py with a key that never comes here).
  2. `proxykey finish --signature 0x...`  register a signature made elsewhere and write .env.
  3. `proxykey status`  show the pending request and the current .env proxy.

Nothing secret is ever printed or logged: the proxy key and secret go straight into .env.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets as _secrets
import stat
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from perpbot.dashboard import CSP, DashServer
from perpbot.paths import Paths

log = logging.getLogger("perpbot.proxykey")

PENDING_NAME = ".proxykey_pending.json"
ENV_KEYS = ("PM_PROXY_PRIVATE_KEY", "PM_PROXY_SECRET", "PM_WALLET_ADDRESS", "PM_PROXY_EXPIRES_AT")


class ProxyKeyError(Exception):
    pass


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

    def typed_data(self) -> dict[str, Any]:
        from polymarket._internal.actions.perps.signing import build_perps_create_proxy_typed_data

        return build_perps_create_proxy_typed_data(chain_id=self.chain_id, proxy=self.proxy, expires_at_ms=self.exp_ms,
                                                   salt=self.salt, timestamp_ms=self.ts_ms)

    def public(self) -> dict[str, Any]:
        return {"proxy": self.proxy, "expires_utc": _utc(self.exp_ms), "created_utc": self.created_utc,
                "chain_id": self.chain_id, "label": self.label}


def _utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pending_path(paths: Paths) -> Path:
    return paths.root / PENDING_NAME


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if os.name != "nt":
        tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)


def new_request(paths: Paths, cfg: Any, *, days: int, label: str, now_ms: int | None = None) -> Pending:
    from eth_account import Account

    if not 1 <= days <= 365:
        raise ProxyKeyError("--days must be between 1 and 365")
    pk = "0x" + _secrets.token_bytes(32).hex()
    ts = int(now_ms if now_ms is not None else time.time() * 1000)
    p = Pending(private_key=pk, proxy=Account.from_key(pk).address, exp_ms=ts + days * 86_400_000,
                salt=_secrets.randbits(32), ts_ms=ts, chain_id=int(cfg.polymarket.chain_id), label=label,
                created_utc=_utc(ts))
    _write_private(_pending_path(paths), json.dumps(asdict(p)))
    out = paths.data_dir / "proxykey"
    out.mkdir(parents=True, exist_ok=True)
    (out / "sign_request.json").write_text(json.dumps(p.typed_data(), indent=2), encoding="utf-8")
    (out / "sign.html").write_text(sign_page(p.typed_data(), token="", post_back=False), encoding="utf-8")
    return p


def load_pending(paths: Paths) -> Pending | None:
    path = _pending_path(paths)
    if not path.exists():
        return None
    return Pending(**json.loads(path.read_text(encoding="utf-8")))


def recover_signer(p: Pending, signature: str) -> str:
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    sig = signature.strip()
    if not sig.startswith("0x") or len(sig) != 132:
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


def finish(paths: Paths, cfg: Any, signature: str, *, expected_owner: str = "", transport: Any = None) -> dict[str, Any]:
    p = load_pending(paths)
    if p is None:
        raise ProxyKeyError("no pending proxy key request: run `proxykey new` first")
    owner = recover_signer(p, signature)
    if expected_owner and owner.lower() != expected_owner.lower():
        raise ProxyKeyError(f"signed by {owner}, but PM_WALLET_ADDRESS in .env is {expected_owner}; "
                            f"sign with your main wallet (or clear PM_WALLET_ADDRESS if you changed wallets)")
    try:
        creds = asyncio.run(_register(p, signature.strip(), owner, str(cfg.polymarket.rest_url), transport))
    except ProxyKeyError:
        raise
    except Exception as e:  # noqa: BLE001 - SDK errors never contain the key
        raise ProxyKeyError(f"the exchange rejected the request: {type(e).__name__}: {e}. If it mentions time or "
                            f"timestamp, sign faster (run the tool again); if it mentions expiry, use fewer --days") from e
    expires = creds.expires_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_env(paths.env_file, {"PM_PROXY_PRIVATE_KEY": p.private_key, "PM_PROXY_SECRET": creds.secret,
                               "PM_WALLET_ADDRESS": owner, "PM_PROXY_EXPIRES_AT": expires})
    _pending_path(paths).unlink()
    return {"proxy": p.proxy, "owner": owner, "expires_utc": expires}


def status_lines(paths: Paths, secrets: Any) -> list[str]:
    p = load_pending(paths)
    lines = []
    if p:
        lines.append(f"pending request: proxy {p.proxy}, expires {_utc(p.exp_ms)}, created {p.created_utc} "
                     f"(signature not registered yet)")
    else:
        lines.append("no pending request")
    if secrets.proxy_address:
        exp = secrets.proxy_expires_at.isoformat() if secrets.proxy_expires_at else "unknown"
        lines.append(f".env proxy: {secrets.proxy_address}, owner {secrets.wallet_address or '?'}, expires {exp}")
    else:
        lines.append(".env has no proxy key yet")
    return lines


# ---------------------------------------------------------------- one-off local signing page

def serve_signing(paths: Paths, cfg: Any, p: Pending, *, port: int, open_browser: bool, expected_owner: str = "",
                  timeout_s: float = 900.0, transport: Any = None, token: str | None = None) -> dict[str, Any]:
    token = token or _secrets.token_hex(16)
    result: dict[str, Any] = {}
    done = threading.Event()
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
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
            self._send(200, sign_page(p.typed_data(), token=token, post_back=True).encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802
            if (self.headers.get("Host", "") not in allowed or self.path != f"/{token}/sign"
                    or self.headers.get("X-Token") != token or done.is_set()):
                return self._send(403, b"forbidden", "text/plain")
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 10_000)
                sig = json.loads(self.rfile.read(n) or b"{}").get("signature", "")
                res = finish(paths, cfg, sig, expected_owner=expected_owner, transport=transport)
                result.update(res, ok=True)
            except Exception as e:  # noqa: BLE001
                result.update(ok=False, error=str(e))
            done.set()
            self._send(200, json.dumps(result).encode(), "application/json")

    httpd = DashServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/{token}/"
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    print(f"Signing page: {url}")
    if open_browser:
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


def sign_page(typed: dict[str, Any], *, token: str, post_back: bool) -> str:
    msg = typed["message"]
    exp = datetime.fromtimestamp(int(msg["exp"]) / 1000, tz=timezone.utc) + timedelta(hours=8)
    info = {"proxy": msg["addr"], "expires_hkt": exp.strftime("%Y-%m-%d %H:%M HKT"), "chain": typed["domain"]["chainId"]}
    return (_PAGE.replace("__TYPED__", json.dumps(typed)).replace("__TOKEN__", token)
            .replace("__POST__", "true" if post_back else "false").replace("__INFO__", json.dumps(info)))


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
<div class="mut">用你嘅<b>主錢包</b>簽一個訊息，授權下面呢個 proxy 地址代你落單。主錢包私鑰唔會離開你個錢包／硬件錢包。</div></div>
<div class="card"><div>Proxy 地址：<code id="proxy"></code></div><div>到期：<span id="exp"></span></div>
<div class="mut">網絡：Polygon（chain <span id="chain"></span>）。錢包會顯示 CreateProxy 訊息：addr 要同上面 proxy 地址一樣。</div></div>
<div class="card"><button id="go">用錢包簽名</button><p id="out" class="mut"></p></div>
</main><script>
const TYPED=__TYPED__, TOKEN="__TOKEN__", POST=__POST__, INFO=__INFO__;
const $=id=>document.getElementById(id), show=(t,c)=>{$("out").textContent=t;$("out").className=c||"mut"};
$("proxy").textContent=INFO.proxy;$("exp").textContent=INFO.expires_hkt;$("chain").textContent=INFO.chain;
$("go").onclick=async()=>{
 try{
  if(!window.ethereum){show("搵唔到瀏覽器錢包（例如 MetaMask）。請用有主錢包嘅瀏覽器開呢頁。","bad");return}
  const [acct]=await ethereum.request({method:"eth_requestAccounts"});
  const want="0x"+Number(TYPED.domain.chainId).toString(16);
  if((await ethereum.request({method:"eth_chainId"})).toLowerCase()!==want){
   try{await ethereum.request({method:"wallet_switchEthereumChain",params:[{chainId:want}]})}
   catch(e){show("請先喺錢包切換到 Polygon 網絡，再撳一次。","bad");return}}
  show("請喺錢包確認簽名…");
  const sig=await ethereum.request({method:"eth_signTypedData_v4",params:[acct,JSON.stringify(TYPED)]});
  if(!POST){show("簽名（複製返去 bot 電腦，喺 Proxy_Key.bat 揀 F 貼上）：\n"+sig,"ok");return}
  show("已簽名，正在向交易所登記…");
  const r=await fetch("sign",{method:"POST",headers:{"X-Token":TOKEN,"Content-Type":"application/json"},body:JSON.stringify({signature:sig})});
  const j=await r.json();
  if(j.ok)show("完成！proxy "+j.proxy+" 已登記，到期 "+j.expires_utc+"。\n.env 已更新。可以關閉呢頁，然後行 2_Smoketest.bat。","ok");
  else show("失敗："+j.error,"bad");
 }catch(e){show("錢包錯誤："+(e&&e.message?e.message:e),"bad")}
};
</script></body></html>
"""
