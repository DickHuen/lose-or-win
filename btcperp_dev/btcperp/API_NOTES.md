# API_NOTES - Polymarket Perps (BTC-PERP)

Written 2026-09-26 for btcperp v1.0.0. **Every answer below is `untested`** until `smoketest`
confirms it live on the Grok Bot computer (the smoketest prints a live answer for (a)-(e)).

## Sources and how they were read

The build machine's network policy blocked `docs.polymarket.com` and `api.perpetuals.polymarket.com`
directly, so:

1. **Primary source: the official Polymarket Python SDK `polymarket-client==0.11.0`** (published by
   Polymarket Engineering on PyPI 2026-09-23, repo `Polymarket/py-sdk`, MIT). The bot uses this SDK
   (pinned) for all exchange access, so endpoint paths, field names, signing and wire formats are the
   SDK's, not guesses. Files read: `_internal/actions/perps/{public,account,trading,signing,credentials,funds}.py`,
   `_internal/perps_session.py`, `models/perps/*.py`, `clients/_transport.py`, `environments.py`.
2. **Docs excerpts** from docs.polymarket.com pages (`perps/trading`, `perps/faq`, `perps/errors`,
   `perps/account-management`, `perps/market-data`, `perps/rate-limits`, `api-reference/geoblock`,
   `changelog/perps`) obtained through web search.
3. No public API call could be made from the build machine (egress blocked), so response parsing was
   verified against the SDK's own pydantic models with wire-format samples (`tests/test_robustness.py`).
   The smoketest is the first live call.

## Endpoints used by the bot

Base REST `https://api.perpetuals.polymarket.com`, WebSocket `wss://ws.perpetuals.polymarket.com/v1/ws`,
chain id 137 (SDK `environments.py`).

| Purpose | Endpoint (SDK method) | Auth |
|---|---|---|
| Instruments (every start) | `GET /v1/info/instruments` (`fetch_perps_instruments`) | public |
| Ticker: mark, index, funding | `GET /v1/info/tickers?instrument_id` + `GET /v1/info/statistics` (`fetch_perps_ticker`) | public |
| Order book | `GET /v1/info/book?instrument_id&depth` (depth 10/100/500/1000) | public |
| Klines 1h/1d | `GET /v1/info/klines?instrument_id&interval&start_timestamp&end_timestamp` -> `{data:[[ts,o,h,l,c,v,trades]],more}` | public |
| Funding history | `GET /v1/info/funding?instrument_id&start_timestamp&end_timestamp` | public |
| Fee schedule | `GET /v1/info/fees` | public |
| Server time (smoketest only) | `GET /v1/info/time` (docs: sync before signing; body shape not in SDK) | public |
| Region | `GET https://polymarket.com/api/geoblock` -> `{blocked, ip, country, region}` | public |
| Credentials / proxy expiry | `GET /v1/account/credentials` -> `{address, keys:[{proxy,label,expiry}]}` | headers `POLYMARKET-PROXY`, `POLYMARKET-SECRET` |
| Balances | `GET /v1/account/balances` -> `[{asset, balance, value}]` | proxy headers |
| Portfolio | `GET /v1/account/portfolio` -> `{positions:[{instrument_id,size(signed),entry_price,leverage,cross,initial_margin,maintenance_margin,position_value,liquidation_price,unrealized_pnl,return_on_equity,cumulative_funding}],margin:{total_account_value,...},withdrawable,in_liquidation,timestamp}` | proxy headers |
| Leverage / margin mode | `GET /v1/account/config?instrument_id` -> `[{instrument_id, leverage, cross}]` | proxy headers |
| Open orders | `GET /v1/account/open-orders?instrument_id` | proxy headers |
| Order status | `GET /v1/account/orders?order_id|client_order_id|instrument_id&start_timestamp&end_timestamp` | proxy headers |
| Fills | `GET /v1/account/fills?start_timestamp&end_timestamp&sort&cursor` | proxy headers |
| Funding payments | `GET /v1/account/funding?instrument_id&start_timestamp&end_timestamp` | proxy headers |
| Deposits / withdrawals (drawdown flow adjustment) | `GET /v1/account/deposits`, `GET /v1/account/withdrawals` | proxy headers |
| Set 3x isolated | signed op `updateLeverage [iid, 3, false]` (SDK session; REST equivalent `PATCH /v1/trade/leverage`) | proxy key signature |
| Orders (FOK bracket, reduce-only IOC, position TP/SL) | signed op `createOrders` (SDK session; REST equivalent `POST /v1/trade/orders`), `grp:"order"` for a bracket, `grp:"position"` + `qty:"0"` for position TP/SL | proxy key signature |
| Cancel by id | signed op `cancelOrders [ids]` | proxy key signature |

Signed commands: EIP-712 `Op{data: keccak(msgpack(op)), salt, ts}` with domain `{name:"Polymarket", version:"1", chainId:137}`,
signed by the proxy private key (SDK `signing.py`). Order keys on the wire: `iid, buy, p, qty, tif (gtc|ioc|fok), po, ro, c
(client order id = 32 lowercase hex), tr{tpsl: tp|sl, trp, market}`.

**Never used:** cancel-all (`DELETE /v1/trade/orders/all`), auto-cancel (`PATCH /v1/trade/auto-cancel`),
`updateMargin`, withdrawals (except the smoketest's rejected probe, see (a)).

Order statuses (SDK `PerpsOrderStatus`): accepted, open, partial, filled, cancelled, auto_cancelled,
post_only_rejected, fok_unfilled, ioc_no_fill, ioc_expired, stp_cancelled, zero_quantity, duplicate_order,
order_not_found, reduce_only_invalid, reduce_only_expired, order_expired, untriggered, armed, triggered,
parent_cancelled, position_closed, position_flipped, reduce_only_invalid_at_trigger, expired.

## The five questions

### (a) Can a proxy signer key withdraw funds? - `untested`
**No (per SDK and docs).** Withdrawal is `POST /v1/account/withdraw` with an EIP-712 `Withdraw{account, token,
amount, fee, to, salt, ts(seconds)}` signed by the **owner account** (domain includes the deposit contract);
the SDK's `withdraw_from_perps` signs it with the owner signer and sends funds to the owner wallet. The docs say
the main wallet signs the one-time create-proxy request; the proxy private key signs trade requests and the
(proxy, secret) pair authenticates private reads. Proxy creation/revocation also require the owner signature.
Live check: `smoketest` signs a 1-base-unit Withdraw with the **proxy** key for your own account, destination
= your own wallet, and expects a rejection.

### (b) Can a bracket SL and a position SL exist together? - `untested`
**Unclear from the docs.** Documented: at most one *position* TP and one *position* SL per instrument; position
TP/SL is sent with `grp:"position"`, `qty:"0"` (sized at trigger time) and is rejected if the mark already
crossed the trigger; a bracket's triggers use `grp:"order"`. For a filled bracket the docs recommend
"cancel the armed trigger and create position TP/SL as the replacement" (not atomic), which suggests they are
separate objects, but coexistence is not stated. The bot never needs both: it only places a position SL when
no active SL exists. Live check: `smoketest` places a position SL while the bracket SL is armed and reports
whether both are listed.

### (c) Are leftover orders cleared automatically after a close? - `untested`
**Partly documented.** "If the entry is canceled, rejected, or only partially filled, the triggers are canceled
too." Order statuses `position_closed`, `position_flipped` and `parent_cancelled` exist, which suggests the
engine cancels triggers when their position goes away, but the docs do not state it. **The bot does not rely on
it:** after every close it lists open orders and cancels any TP/SL or reduce-only order by id, then verifies
none remain before any new entry. Live check: `smoketest` reports the bracket orders' statuses right after its
close.

### (d) Can cumulative funding be read after a close? - `untested`
**Not from the position.** `cumulative_funding` is a field of an *open* position in `GET /v1/account/portfolio`;
a closed position is no longer listed. Funding remains readable per payment from `GET /v1/account/funding`
(`id, instrument_id, size, funding_rate, funding, timestamp`). The bot records `cumulative_funding` on every
run while the position is open and books trade funding from `/v1/account/funding` between entry and exit.
Live check: `smoketest` reads both after its close.

### (e) What is the order response in cancel-only mode? - `untested` (cannot be triggered on demand)
Docs (perps errors page): "Trading is currently cancel-only. New orders are not accepted, but cancels are
allowed." SDK transport: REST returns **HTTP 503** with JSON `{"error": "...cancel-only..."}` (no structured
code; `RequestRejectedError.restriction == "cancel_only"`); **425** means the matching engine is restarting;
503 with `code: "post_only_mode"` means post-only. Over the SDK's WebSocket session a rejected command comes
back as an acknowledgement `{"status": "err", "error": "..."}`. The bot treats any rejection as "not placed":
no entry that day after the retry; if an SL cannot be re-placed it tries to close reduce-only and alerts
"CLOSE FAILURE" if that is rejected too.

## Other assumptions the bot depends on (all `untested`; verified by smoketest)

- **(f) Equity:** mark-to-market equity = wallet + unrealized PnL. It is unknown whether `balances[].value`
  still includes margin locked in an isolated position. Default `risk.equity_source: auto` uses wallet + uPnL
  but switches to the exchange's `margin.total_account_value` (and alerts) when they differ by more than 0.5%.
  Smoketest reports `balance_excludes_isolated_margin`.
- **(g) Funding sign:** assumed `funding > 0` = credit to us (`risk.funding_payment_sign: 1`). Check the first
  funding payments against the balance.
- **(h) Fill PnL:** assumed `fills[].pnl` excludes fees (net = pnl - fees + funding). Smoketest reports whether
  the wallet change equals pnl - fees or pnl.
- **(i) Fill side:** `fills[].side` is `long|short`; its meaning (trade direction vs position side) is not
  documented. The bot does not use it: it classifies fills by `previous_size` (0 = opening fill). Smoketest
  prints both.
- **(j) Visibility:** bracket TP/SL triggers are assumed to appear in `GET /v1/account/open-orders` with
  `tpsl.kind`. If they did not, the bot would add a position SL each run; smoketest checks this.
- **(k) Order status:** FOK results are read from `GET /v1/account/orders?client_order_id=`; if that does not
  show the order, the bot confirms a fill from the position itself.
- **(l) `price_bounds`:** meaning not documented; the bot logs it and does not enforce it (the exchange will).
- **(m) Proxy key lifetime:** the SDK default is 7 days; whether the server accepts a 30-day expiry is not
  documented. The bot reads the real expiry from `/v1/account/credentials` and alerts 5 days before.
- **(n) Region:** the Perps FAQ (via search) says Perps have no geographic restriction on order placement;
  you require non-US only. The bot calls `polymarket.com/api/geoblock` at every `decide` and in `smoketest`,
  blocks new entries if `blocked: true`, and never uses a VPN or proxy.
- **(o) Timestamps:** signed requests need a fresh `ts` and `salt`; the computer's clock must be NTP-synced.
  Smoketest reads `/v1/info/time` and reports the skew.
- **(p) Rate limits:** 1,000 weighted tokens per IP per minute (docs); the bot makes a few dozen calls per run.

## Binance (indicator data)

Polymarket BTC-PERP has little history, so indicators use Binance public data (no keys):
`GET https://api.binance.com/api/v3/klines` (fallback `https://data-api.binance.vision`) for 1d/4h candles,
`GET https://fapi.binance.com/fapi/v1/fundingRate` for 365+ days of 8h funding (point-in-time percentile).
Polymarket funding is logged as a cross-check only.
