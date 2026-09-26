"""Create a 30-day Polymarket Perps proxy key. Run on YOUR OWN computer, never on the bot computer.
Needs: python3 -m pip install polymarket-client==0.11.0
Your main wallet key is typed in hidden, used once to sign, never saved or sent anywhere except as a signature."""
import asyncio
import getpass
from datetime import timedelta

from eth_account import Account
from polymarket._internal.actions.perps.credentials import create_credentials
from polymarket.clients._transport import AsyncTransport


async def main(transport=None, key=None):
    signer = Account.from_key(key or getpass.getpass("Main wallet private key (hidden, not saved): "))
    t = transport or AsyncTransport(base_url="https://api.perpetuals.polymarket.com")
    try:
        c = await create_credentials(t, signer=signer, chain_id=137, expires_in=timedelta(days=30), label="btcperp")
    finally:
        await t.close()
    print("\nGive ONLY these to Grok Bot (never the main wallet key):")
    print(f"PM_PROXY_PRIVATE_KEY={c.private_key}")
    print(f"PM_PROXY_SECRET={c.secret}")
    print(f"PM_WALLET_ADDRESS={signer.address}")
    print(f"PM_PROXY_EXPIRES_AT={c.expires_at.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    return c


if __name__ == "__main__":
    asyncio.run(main())
