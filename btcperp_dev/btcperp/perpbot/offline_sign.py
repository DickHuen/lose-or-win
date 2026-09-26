"""Sign a btcperp proxy-key request on ANOTHER computer (never on the bot computer).

Use this only if your main wallet is not in a browser wallet (e.g. an exported key from an email login).
On the other computer:
    pip install eth-account
    python offline_sign.py sign_request.json
It asks for the main wallet private key (hidden, never saved, never sent anywhere), prints the signer address
and the signature. Bring ONLY the signature back to the bot computer (Proxy_Key.bat, option F).
Standalone: needs only the eth-account package, not the bot.
"""

import getpass
import json
import sys


def sign(request: dict, key: str) -> tuple[str, str]:
    from eth_account import Account

    acct = Account.from_key(key)
    signed = acct.sign_typed_data(full_message=request)
    return acct.address, "0x" + bytes(signed.signature).hex()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    with open(sys.argv[1], encoding="utf-8") as fh:
        req = json.load(fh)
    msg = req["message"]
    print(f"Proxy address to authorise: {msg['addr']}  (check it matches what the bot computer showed)")
    address, sig = sign(req, getpass.getpass("Main wallet private key (hidden, not saved): ").strip())
    print(f"\nsigned by: {address}\nsignature: {sig}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
