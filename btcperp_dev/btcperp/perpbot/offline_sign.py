"""Sign a btcperp proxy-key request on ANOTHER computer (option O; never on the bot computer).

Use YOUR OWN copy of the release on that computer (checked with the zip's SHA-256), never a file carried over
from the bot computer: only the four plain fields travel (data/proxykey/sign_fields.txt). This script builds
the one message it will ever sign itself - CreateProxy{addr, exp, salt, ts} with the fixed Polymarket domain on
Polygon (chain 137) - so a changed file cannot make it sign anything else (review v1.3.0 P2/P5).

On the other computer:
    pip install eth-account
    python offline_sign.py sign_fields.txt
or  python offline_sign.py --addr 0x... --exp 1790000000000 --salt 12345 --ts 1788000000000

It shows the proxy address and expiry, asks you to type YES, then asks for the main wallet private key
(hidden, never saved, never sent anywhere), and prints the signer address and the signature. Bring ONLY the
signature back to the bot computer (Proxy_Key.bat, option F). Standalone: needs only eth-account.
"""

import argparse
import getpass
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

CHAIN_ID = 137
DOMAIN = {"name": "Polymarket", "version": "1", "chainId": CHAIN_ID}
TYPES = {
    "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                     {"name": "chainId", "type": "uint256"}],
    "CreateProxy": [{"name": "addr", "type": "address"}, {"name": "exp", "type": "uint64"},
                    {"name": "salt", "type": "uint64"}, {"name": "ts", "type": "uint64"}],
}
MAX_DAYS = 30
FIELDS = ("addr", "exp", "salt", "ts")
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")


class RequestError(Exception):
    pass


def build(fields: dict) -> dict:
    """The only message this tool signs: fixed type and domain, four checked fields."""
    addr, exp, salt, ts = (fields.get(k) for k in FIELDS)
    if not _ADDR.match(str(addr)):
        raise RequestError("addr must be 0x + 40 hex characters")
    try:
        exp, salt, ts = int(exp), int(salt), int(ts)
    except (TypeError, ValueError) as e:
        raise RequestError("exp, salt and ts must be whole numbers") from e
    if not 0 <= salt < 2 ** 32 or ts <= 1_600_000_000_000 or exp <= ts:
        raise RequestError("salt / ts / exp out of range")
    if exp - ts > MAX_DAYS * 86_400_000:
        raise RequestError(f"expiry more than {MAX_DAYS} days after ts: refused")
    return {"types": TYPES, "primaryType": "CreateProxy", "domain": dict(DOMAIN),
            "message": {"addr": addr, "exp": exp, "salt": salt, "ts": ts}}


def check_request(req: dict) -> dict:
    """A full typed-data request is accepted only if it is exactly a CreateProxy for Polymarket on chain 137
    (never a Permit, a transfer, another chain or an extra domain field). Returns the four fields."""
    if not isinstance(req, dict) or req.get("primaryType") != "CreateProxy":
        raise RequestError(f"not a CreateProxy request (primaryType={req.get('primaryType') if isinstance(req, dict) else None!r}): refused")
    if req.get("domain") != DOMAIN:
        raise RequestError(f"unexpected domain {req.get('domain')!r} (must be Polymarket v1 on chain 137): refused")
    if req.get("types") != TYPES:
        raise RequestError("unexpected message types: refused")
    msg = req.get("message")
    if not isinstance(msg, dict) or set(msg) != set(FIELDS):
        raise RequestError("the message must have exactly addr, exp, salt, ts: refused")
    return {k: msg[k] for k in FIELDS}


def parse_fields(text: str) -> dict:
    """sign_fields.txt: name=value lines (addr, exp, salt, ts, and optionally owner = the main wallet that must
    sign); '#' lines are comments. A JSON typed-data request is also accepted, but only through check_request."""
    stripped = text.strip()
    if stripped.startswith("{"):
        return check_request(json.loads(stripped))
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RequestError(f"cannot read line {line!r}")
        k, v = (s.strip() for s in line.split("=", 1))
        if k not in FIELDS and k != "owner":
            raise RequestError(f"unknown field {k!r}: refused")
        if k == "owner" and not _ADDR.match(v):
            raise RequestError("owner must be 0x + 40 hex characters")
        out[k] = v
    missing = [k for k in FIELDS if k not in out]
    if missing:
        raise RequestError(f"missing field(s): {', '.join(missing)}")
    return out


def hkt(ms: int) -> str:
    return (datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M HKT")


def sign(request: dict, key: str) -> tuple:
    """Sign a request after the whitelist check: returns (signer address, 0x signature)."""
    from eth_account import Account

    typed = build(check_request(request))
    acct = Account.from_key(key)
    signed = acct.sign_typed_data(full_message=typed)
    return acct.address, "0x" + bytes(signed.signature).hex()


def main(argv: list | None = None, *, ask=input, secret=getpass.getpass, now_ms: int | None = None) -> int:
    ap = argparse.ArgumentParser(description="sign a btcperp proxy-key request (CreateProxy only)")
    ap.add_argument("file", nargs="?", help="sign_fields.txt from the bot computer")
    for k in FIELDS:
        ap.add_argument(f"--{k}")
    ap.add_argument("--owner", help="the main wallet that must sign (optional check)")
    a = ap.parse_args(argv)
    try:
        if a.file:
            with open(a.file, encoding="utf-8") as fh:
                fields = parse_fields(fh.read())
        else:
            fields = {k: getattr(a, k) for k in FIELDS}
            if a.owner:
                fields["owner"] = a.owner
            if None in fields.values():
                ap.print_help()
                return 2
        typed = build(fields)
        m = typed["message"]
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        if m["exp"] <= now:
            raise RequestError("this request has already expired: make a new one on the bot computer")
        if abs(now - m["ts"]) > 3_600_000:
            print("WARNING: this request was made more than 1 hour ago; the bot computer deletes unfinished requests "
                  "after 1 hour, so make a new one if finishing fails.")
    except (OSError, ValueError, RequestError) as e:
        print(f"REFUSED: {e}")
        return 1
    print("You are about to authorise this proxy key to trade Polymarket Perps for your main wallet:")
    print(f"  proxy address : {m['addr']}   (must match what the bot computer showed)")
    print(f"  expires       : {hkt(m['exp'])}")
    if fields.get("owner"):
        print(f"  main wallet   : {fields['owner']}")
    print("  message       : CreateProxy, Polymarket, Polygon chain 137 (nothing else can be signed here)")
    if ask("Type YES to sign: ").strip() != "YES":
        print("Cancelled.")
        return 1
    try:
        from eth_account import Account

        key = secret("Main wallet private key (hidden, not saved): ").strip()
        address = Account.from_key(key).address
        if fields.get("owner") and address.lower() != str(fields["owner"]).lower():
            print(f"REFUSED: that key belongs to {address}, not to the main wallet {fields['owner']}. "
                  f"Nothing was signed.")
            return 1
        address, sig = sign(typed, key)
    except Exception as e:  # noqa: BLE001 - never echo the key
        print(f"FAILED: {type(e).__name__} (check the key)")
        return 1
    finally:
        key = ""
    print(f"\nsigned by: {address}\nsignature: {sig}")
    print("Check 'signed by' is your MAIN wallet, then type the signature into Proxy_Key.bat option F.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
