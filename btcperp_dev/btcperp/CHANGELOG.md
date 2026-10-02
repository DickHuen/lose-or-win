# Changelog

Each version ships as `btcperp_vX.Y.Z.zip`. Code version = `VERSION`; config version = `config_version`
in `config/config.yaml`. Every log row records both, and reports never mix versions.

## 1.5.8 - 2026-10-02 (config 1.5.8) - owner: 5% risk per trade

The owner chose 5% at the 100% tier (the maximum the config allows) after the trade-offs were laid out: about $5 at
the stop per full trade on 100 USDC, and backtest run #1's max drawdown of 10.9% at 1.5% risk scales to about 36%.

- **Config 1.5.8:** `risk_per_trade_pct` 5 (was 2 in 1.5.7, 1.5 before); `notional_cap_pct_equity` 150 (was 60) so a
  full-tier trade is not cut down (3x isolated allows up to 300%); `kill_drawdown_pct` 25 (was 15) and
  `kill_losing_streak_pct` 20 (was 8): at 5% the old lines would trip after three or two full losses.
- **Unchanged:** the 75% equity floor (hard stop: about $25 lost on 100 USDC) and the 50% permanent floor; 3x
  isolated; SL 1.5 ATR / TP 3 ATR; no ramp; raise-to-minimum; tie rule +/-0.1%; live review line -0.196 R.
- With 100 USDC at BTC 83,066 and ATR 2,198: 25% tier 0.00037 BTC (~$31, risk ~$1.22), 50% tier 0.00075 BTC
  (~$62, ~$2.47), 100% tier 0.00151 BTC (~$125, ~$4.98; isolated margin ~$42).
- Tests keep the v1.5.6 sizing and kills (pinned in conftest); `test_sizing_v157.py` covers the shipped values.
  **Tests:** 308.

## 1.5.7 - 2026-10-02 (config 1.5.7) - owner: more aggressive sizing so a small account trades

First live decision (2026-09-30 16:30 HKT): score +37.17, LONG, 50% tier, all gates clear - but no order: 100 USDC x
1.5% x 0.5 (ramp) x 0.5 (tier) = $0.375 at the stop = 0.00011 BTC = $9.1, below BTC-USD's $10 minimum ("entry
rejected"). With 100 USDC only 100%-tier signals could trade during the ramp. The owner asked for more aggressive
sizing so that a small account always trades.

- **Sizing (config 1.5.7, owner 2026-10-02):** risk 2% at the 100% tier (was 1.5%); no half-size ramp
  (`ramp_trades` 0, was 10); notional cap 60% of equity (was 30%).
- **`risk.raise_to_min_notional: true`:** a size below the exchange minimum is raised to the minimum if that trade's
  risk at the stop stays within the 100%-tier budget (equity x 2%) and the notional and leverage caps; otherwise the
  entry is refused as before. With 100 USDC at BTC 83,066 and ATR 2,198: 25% tier 0.00015 BTC (risk ~$0.49),
  50% tier 0.0003 BTC (~$0.99), 100% tier 0.0006 BTC (~$1.98, notional ~$50 under the $60 cap). The refused
  16:30 trade would have been 0.0003 BTC.
- Unchanged: 3x isolated, SL 1.5 ATR / TP 3 ATR, kill switches (15% drawdown, 8% losing streak, 75% equity floor,
  50% permanent floor), tie rule +/-0.1%. With 2% risk the losing-streak switch can fire after four full-size losses.
- **Backtest:** the R numbers (expectancy, t, I1, I6) do not depend on sizing; % returns and drawdowns scale by about
  2/1.5 (run #1: max drawdown 10.9% -> about 14.5%, close to the 15% drawdown kill). The config change ends the
  2026-09-30 confirmation: a new `Backtest.bat` run asks for CONFIRM again.
- **Live review line (S9):** -0.196 R (I6 of R4h_live, run #1). After 30 live trades, a rolling 30-trade expectancy
  below it pauses new entries until you review with Claude.
- **Owner decisions recorded:** option B (4h) runs live although backtest run #1 failed (C0c data gaps, C1d t 1.79);
  8% / 75% / 50% confirmed 2026-09-30; the committee is dissolved - texts now say "owner" / "you".
- Tests run on the v1.5.6 sizing (pinned in conftest) plus `test_sizing_v157.py` for the shipped values. **Tests:**
  308 (+6).

## 1.5.6 - 2026-09-30 (config 1.5.0) - rate limits: reads wait and retry

Smoketest W on v1.5.5 got much further: `fok_unfilled_status`, `open_bracket` (a real minimum long with bracket SL/TP)
and `b_position_sl_with_bracket` ("YES, both can exist") passed. The close filled one second after the entry
(exchange history: open long 13:44:27 at 83,421, close long 13:44:28 at 83,420, 0.00015 BTC, fee 0.005005 each =
0.04% taker), but the step failed: `GET /v1/account/portfolio` answered "rate limited (retry_after=1.0)" while the
smoketest polled it every 0.5 s. The new `cleanup` step found no position (PASS). Cost of the test: about 1 cent.

- **Exchange reads (GET) wait and retry** on a rate limit: the exchange's `retry_after` (at most 5 s), up to 4 times.
  Commands (orders, cancels, leverage) never retry.
- Smoketest: the flat check reads once a second, and a failed read counts as "not yet flat", never as a failed close.
- **Tests:** 302 (+3).

## 1.5.5 - 2026-09-30 (config 1.5.0) - order status without the client-order-id lookup; smoketest cleanup

Smoketest W on v1.5.4: every read step, the leverage step and `place_cancel` passed (the price fixes work). It
stopped at `fok_unfilled_status`: the FOK test order was accepted and did not fill (portfolio unchanged, no position),
but `GET /v1/account/orders?client_order_id=` returned nothing, so its status was unknown. The order-id lookup does
work (place_cancel found its order "cancelled").

- **Order status:** `confirm_order` uses, in order: the exchange's own order update that comes with the placement,
  the order id, then the client order id. Restart recovery, the entry-retry proof (B1) and position adoption use a
  new `orders_by_coid`: the client-order-id lookup, else the order ids known locally (the placement response logged
  in `orders`, and any fill carrying that client order id). The whole test suite also passes with the
  client-order-id lookup disabled; the entry safety tests run that way in `test_order_status_v155.py`.
- **Smoketest:** `fok_unfilled_status` records the status from each source. A new last step `cleanup` (YES / W)
  closes any position left by a failed step, reduce-only, and cancels its leftover orders by id. Before, a critical
  failure after the entry filled skipped the close.
- **Tests:** 299 (+12).

## 1.5.4 - 2026-09-30 (config 1.5.0) - Windows: install test reset and a restore that gave up

The v1.5.3 upgrade on the owner's PC failed one test and then could not restore v1.5.2. No position was open and no
scheduled task was installed. v1.5.4 contains all of v1.5.3 plus:

- **Local servers read the request body before answering** (signing page and dashboard).
  `test_local_signing_page_round_trip_and_single_submit` failed with WinError 10053. http.client sends the headers
  and the body separately; the signing server refused (403 / 409) before the body arrived, and on Windows the late
  body resets the connection, so the client lost the answer. Handlers also time out after 10 s, so a client that
  never sends its body cannot hold a thread.
- **The automatic restore overwrites in place and retries.** It used to delete each code folder and copy the backup
  back; Windows refused to delete `perpbot\datasources` for a moment (WinError 5, typically antivirus or the search
  indexer), and the restore stopped half way. Now it copies the backup over the folder and then removes only the
  files the new version added, each step retried for about 20 seconds.
- **Recovery from that state:** `Upgrade.bat` with this zip. The installed VERSION still reads 1.5.3, so 1.5.4 is
  accepted; every file is written again and the tests run (tested from exactly that half-restored state).
- **Tests:** 287 (+4).

## 1.5.3 - 2026-09-30 (config 1.5.0) - live smoketest W: wrong ticker, price format, price band

Smoketest W on the owner's PC placed no order: the first test order was refused. Its output also showed that the
ticker belonged to another market. Nothing was traded; the withdrawal probe was refused as expected.

- **Wrong ticker (serious, fixed):** the SDK's `fetch_perps_ticker` returns the first ticker the API sends, and the
  API ignored the instrument filter: the bot read mark 7690.6 while BTC-USD's book was 83264 / 83265. The mark feeds
  sizing and the SL/TP prices, so a live entry would have been wrong.
  - The adapter now picks the ticker whose `instrument_id` matches (searching the full list if the filtered one
    lacks it) and refuses otherwise.
  - Guard: every decision checks the mark against the same instrument's order book (tolerance: the instrument's
    price band, at least 2%). If they disagree the decision stops with an error and alert; nothing is sized or
    placed. Market-data logging skips such a snapshot.
  - Smoketest `prices` fails (and stops the trading steps) if the mark, the last Polymarket 1h close or Binance
    disagree with the book; `basis` fails beyond max(price band, 200 bps).
  - Backtest download keeps Polymarket 1h candles only if the latest one matches the book.
- **Price format (fixed):** "price exceeds allowed significant figures". BTC-USD allows 1 decimal but at most
  5 significant figures (its book quotes whole dollars). Every order and SL/TP price is now rounded to at most
  5 significant figures in the same direction as before (BTC at 83,264: whole dollars; at 100,000 or more: steps
  of 10).
- **Price band:** BTC-USD refuses orders more than 2% from the mark (`price_bounds` 0.02). The smoketest's resting
  test order now sits at most half the band below the bid (1%).
- The mock exchange enforces both rules, so every test checks them.
- **Tests:** 283 (+7).

## 1.5.2 - 2026-09-30 (config 1.5.0) - smoketest fee check fixed after the first live read-only run

First live `smoketest --no-trade` on the owner's PC: key, account (100 USDC) and prices read fine, but two steps failed.

- **fees** (bot bug, fixed): `GET /v1/info/fees` listed only the `equity` category (taker 0.0004), so the lookup for
  `crypto` found nothing and the step failed. Now:
  - the lookup uses the resolved instrument's own category (config `market.category` only if unknown);
  - if the exchange does not list that category, the fee is the **higher** of the listed taker rates and the config
    estimate (0.0005), and the step passes with a note;
  - the full smoketest (YES / W) also records the fee actually charged on its fills (`measured_taker_fee_rate`);
    the backtest fee is the highest of the config estimate, the schedule and the measured fee (BT3 unchanged in
    spirit: never lower than the evidence).
  - The shadow fee uses the same rule.
- **region** (not a bug): the exchange's geoblock answered `blocked: true` for the PC's network. The bot does not
  open positions while blocked, and it must never be worked around with a VPN or proxy. START_HERE section 4 says
  what to check.
- Smoketest output and result files show Chinese text instead of `\uXXXX` escapes.
- Docs: START_HERE go-live list names option P; the owner approves the backtest criteria (no committee wait);
  Upgrade.bat and START_HERE give the 4-hourly decision times to avoid when upgrading.
- **Tests:** 276 (+3).

## 1.5.1 - 2026-09-30 (config 1.5.0) - sign the proxy key on your phone

The owner has no hardware wallet and no second computer. v1.5.1 lets the main wallet sign on the phone, so the main
wallet key never touches the bot PC.

- **Proxy_Key.bat option P** (`proxykey new --phone [--host IP]`):
  - the one-off signing page is served on this PC's home Wi-Fi address (private addresses only: 192.168.x.x,
    10.x.x.x, 172.16-31.x.x; never public or loopback);
  - a short one-time link (10 characters, easy to type on a phone);
  - only that address is accepted as Host; one submission; closes after 15 minutes;
  - the phone's MetaMask app (in-app browser) opens the link and signs;
  - the page builds the CreateProxy message itself, as before, and checks the wallet account is the main wallet
    given at `new`. The bot PC registers the key and writes .env.
- **Signing pages** (local, phone and `offline_sign.html`): if the wallet does not know Polygon, the pages add it
  (`wallet_addEthereumChain`). They warn to reject Permit, Approve or transfer requests.
- START_HERE section 3 gives the phone steps in Chinese.
- **Tests:** 273 (+4). The LAN page, a public address refused, the address detection and the CLI option.

## 1.5.0 - 2026-09-29 (config 1.5.0) - option B: decide every 4 hours; readable analysis; Preview

The owner chose option B: the same strategy, decided every 4 hours instead of once a day.

- **Rolling 4h cadence** (`strategy.cadence: rolling_4h`, the new default):
  - a decision 30 minutes after every UTC 4h candle closes: 00:30, 04:30, 08:30, 12:30, 16:30 and 20:30 HKT,
    each with a retry at :50;
  - the score uses daily candles that END at the decision time, built from Binance 4h candles. Same score,
    gates, exits and parameters; only the day's end moves;
  - one entry per 4-hour period; the entry window is HH:30 to HH+1:30 HKT; after it, the period's close rules
    still run late (never a late entry);
  - the 3-day rule is 18 consecutive opposite periods (72 h);
  - `strategy.flip_confirm_periods` (default 1): 2 makes a flip wait for a second opposite period
    (backtest variant `R4h_confirm`);
  - manage runs in between (02:30, 06:30, 10:30, 14:30, 18:30, 22:30 HKT);
  - `cadence: daily` restores the v1.4 behaviour exactly (the existing tests run it).
- **Backtest:** new primary variant `R4h_live` with its stress twin, `R4h_confirm`, and the sensitivity variants
  `R4h_live_noevents` / `R4h_live_nokill`. The daily variants stay for comparison; I1 is now "4h minus daily".
  Criteria draft-3: the same rules on the new primary. A test checks the rolling backtest equals the live decide
  on 30 periods.
- **Scheduler:** `schedule install` registers the 4h schedule and deletes btcperp decide/manage tasks of an older
  schedule, so the old and the new never both run.
- **Readable analysis:** every decision stores a Traditional Chinese analysis. It covers:
  - the score and its three parts, with prices;
  - each gate;
  - the position and the action;
  - entry, stop-loss and take-profit estimates;
  - the risk budget;
  - the flip condition and the next decision time.

  It is shown on the dashboard, and a one-line Windows notification is sent (`notifications.analysis_toast`).
- **Preview:** new `preview` command and `windows\Preview.bat`: what the strategy would decide right now. Public
  Binance data only; no keys, no orders, nothing written.
- **Reports:** a month is INCOMPLETE above 12 four-hour periods without an on-time decision (2 days, as before).
  Shadow variants replay daily decisions only, so they are skipped on the 4h cadence.
- **Heartbeat:** new cron schedules for the two healthchecks.io checks (START_HERE section 8).
- **Tests:** 269 (+17).
- **From v1.4.1:** `windows\Upgrade.bat`. If the backtest was confirmed under v1.4, it needs a new confirmation.
  The code and criteria changed, so `backtest run` says so.

## 1.4.1 - 2026-09-28 (config 1.4.0) - Windows install fix

- **Fix:** on the owner's Windows PC, `1_Install.bat` stopped with `INSTALL FAIL`. One test,
  `test_no_cancel_all_or_auto_cancel_used`, read the bot's source files without naming UTF-8. Windows then reads
  them as cp1252, which cannot decode the Chinese text in the dashboard. The bot's own code already names UTF-8
  everywhere. All 61 such reads and writes in the tests, and the lock file, now say `encoding="utf-8"`.
- **New test** `test_every_text_file_access_names_utf8`: fails on any text-file access without an explicit
  encoding, so this is caught on Linux too.
- No change to trading logic or config.
- **Tests:** 252.
- **From a failed v1.4.0 install:** put this zip in Downloads, run `windows\Upgrade.bat` and type `UPGRADE`.

## 1.4.0 - 2026-09-27 (config 1.4.0) - committee review of v1.3.0

All items the developer agreed with, as the owner decided ("change what you agree with; decide the rest").
REVIEW_v1.3.0.md has the item-by-item record, including the three items done differently (C0 gap tolerance,
BT3 funding, P7 revoke). Strategy scoring, gates and tiers are unchanged. Bankroll stays at a fixed 1.5% risk.
No Telegram. **Do not type CONFIRM until the committee has reviewed criteria draft-2 and this release.**

- **Calendar:** the committee verified all 193 historical dates; `calendar_history.yaml` is marked verified.
- **Criteria draft-2** (`config/backtest_criteria.yaml`): C0 data completeness (C0a missing ≤ 0.1%; C0b/C0c
  gaps while holding ≤ 3 h each, ≤ 6 h per run), C1a–d (every offset ≥ +0.05 R, stress twin > 0, ≥ 2 of 3
  segments, t ≥ 2.0), C2a/b, C3 hourly drawdown ≤ 20%, C4, C5 ≥ 60 trades, C6 full-period drawdown ≤ 25%,
  C7 drawdown kills ≤ 3; I1–I7 (I3b without kill switches, I5 long/short split, I6 rolling p5, I7 Polymarket replay).
- **Backtest:**
  - BT1: a gap through the stop fills at the open; BT6: the loss is capped at the margin;
  - BT2: hourly mark-to-market equity and drawdown;
  - BT3: `_stress` twins (fees ×2, 30 bps exit slippage, paid funding ×2); the fee is never below the smoketest's real taker fee;
  - S5: pre-registered rule for replacing A_live (in `select_variant` and BACKTEST.md);
  - S6: windows at full risk from trade 1; only the full-period run ramps;
  - R1: `confirm` locks criteria, config, calendars, code, `VERSION`, data end date, data hash and fee; `run`
    refuses any change and numbers the runs (the committee uses run #1);
  - R2: data SHA-256, missing candles, funding gaps and gaps while holding in the report;
  - BT4: `A_live_nokill`; I7: optional Polymarket 1h candles (`polymarket_1h.csv`) for the replay.
- **Live risk:**
  - F1: permanent floor at 50% of all capital ever funded (**owner to confirm**). Never re-based; below it the
    bot closes and stops for good. Only a dated config restarts it; a config that lowers the % is refused.
  - F2: the equity-floor baseline is tied to the trigger date (`risk.equity_floor_reset_for`), used once, and
    must be ≤ the equity at that time.
  - S9: live review line. After 30 live trades, a rolling 30-trade expectancy below
    `risk.live_review_expectancy_floor_r` pauses new entries. It is set from I6 after the backtest; `null` for now.
- **Heartbeat (V4):** separate decide and manage checks (`HEALTHCHECK_DECIDE_URL`, `HEALTHCHECK_MANAGE_URL`;
  `HEALTHCHECK_PING_URL` is the fallback). Each run pings `/start` first. At the end it pings `/fail` on an error,
  while a critical alert is unread, or while a hard stop is active.
- **Upgrade:**
  - U1: an unreadable Task Scheduler stops the upgrade before any change;
  - U2: backup to `data/upgrade_backup_<version>_<time>/` and automatic restore on any failure;
  - U3: an older or equal zip is refused, and the zip's SHA-256 is shown;
  - new Upgrade.bat choice `TEST-RESTORE` (`install.py --test-restore`) for the go-live check B3: it installs
    the zip, simulates a failure and must restore the installed version. Without internet, the restore accepts
    packages that are already installed.
- **Proxy key (P1–P8):**
  - option N needs typing HARDWARE;
  - the signer builds the one CreateProxy message itself (fixed domain, chain 137, 4 fields, ≤ 30 days);
  - option O carries only `sign_fields.txt` to the other computer, which uses its own verified release:
    `perpbot/offline_sign.py` or the new `offline_sign/offline_sign.html`;
  - the main wallet is fixed at `new`, and `finish` accepts only its signature;
  - `--days` is at most 30, and an unfinished request is deleted after 1 hour;
  - registration and the .env write run under the bot lock;
  - `status` lists all registered proxy keys;
  - the signing page accepts one submission only and escapes its data.
- **Other:**
  - L1: late-decision comparison test;
  - V16: the wrong-folder message shows the right Kill path;
  - V17: the monthly report gives all months and complete months only;
  - R3: the Polymarket mark vs Binance basis is logged every manage run and reported (p50/p99);
  - the smoketest records the taker fee and the basis.
- **Tests:** 251 (+36).
- **From v1.3.0:** use `windows\Upgrade.bat` with this zip. If nothing is installed yet, install v1.4.0 fresh
  (START_HERE.md). A proxy-key request started with v1.3.0 is discarded: run Proxy_Key.bat again.

## 1.3.0 - 2026-09-26 (config 1.3.0) - committee review of v1.2.0

All items the developer agreed with, as decided by the owner. REVIEW_v1.2.0.md has the item-by-item
record. Strategy scoring, gates and tiers are unchanged. Bankroll stays at a fixed 1.5% risk. No Telegram.

- **Backtest (B3):**
  - new `backtest download|criteria|confirm|run` command and `Backtest.bat`, running on the owner's PC from Binance data;
  - pre-registered variants, 6-month windows × 5 start offsets, conservative execution; BACKTEST.md describes it;
  - pass/fail rules in `config/backtest_criteria.yaml`, which must be confirmed (SHA-256) before a run;
  - historical calendar `config/calendar_history.yaml`, still to be verified by the committee;
  - live and backtest share `strategy.day_features` + `plan_for`, and a test checks the backtest equals live `decide` on 20+ days.
- **Missed decide:** a late `decide`, or the first `manage` with no decision and a position open, runs the day's
  close rules on the same data. It never enters.
- **D11:** ±0.1% of equity at entry is a tie in the losing streak (`risk.losing_streak_tie_pct`).
- **Notifications:** sent after the run, once the lock is released. Toasts do not block; at most 5 per run.
- **Heartbeat:** optional `HEALTHCHECK_PING_URL` pinged after every `decide` / `manage` (`/fail` on error).
- **Pauses and resume:**
  - `unpause` / `Unpause.bat` removes only the manual pause, and the upgrade flow uses it;
  - `reasons` lists the active pauses;
  - `resume` needs `--reset-peak` (`RESET-PEAK` in Resume.bat) to clear a drawdown / losing-streak kill;
  - the equity floor clears only with a new config version stating `risk.equity_floor_reset_baseline_usd`.
- **Upgrade:** `Upgrade.bat` / `install.py --from-zip` disables the tasks, waits for the bot lock, replaces the
  files, installs and tests, then re-registers the tasks. If the tests fail, the tasks stay disabled.
- **Calendar fail-safe:** once an event type's coverage has ended, new positions are blocked; closes still run.
- **FOK:** `exits.entry_attempts: 1`. The smoketest records the exchange's status for an unfillable FOK.
- **Clock check:** a difference of more than 30 s from the exchange server time (or an unreadable server time)
  blocks new positions.
- **Proxy key:** `proxykey` command and `Proxy_Key.bat`. The proxy key is generated on the bot PC and the main
  wallet only signs, in a browser or hardware wallet or on another computer (`offline_sign.py`).
  `tools/create_proxy_key.py`, which needed the main key typed in, is removed.
- **Windows:**
  - daily tasks pinned to HKT (`+08:00`);
  - `UserId` on the tasks;
  - power and wake-timer check in `schedule show/list`;
  - wrong-copy guard for manual commands;
  - `schedule install` needs a passing full smoketest of this version and proxy key (`--upgrade` for upgrades);
  - `2_Smoketest.bat` option W adds the withdrawal probe.
- **Reports:** the monthly report lists days without an on-time decision; more than 2 marks it INCOMPLETE and
  raises an alert.
- **Dashboard:** Content-Security-Policy header; header shows snapshot failures (not listed as errors).
- **Docs:** START_HERE.md rewritten with a go-live checklist (committee go/no-go), proxy key, backtest,
  heartbeat, power and sign-in settings, offline guidance and the new upgrade flow.
- **Tests:** 215 (+51). Redaction tests also read the SQLite WAL. The test suite never calls the real Task Scheduler
  (`BTCPERP_NO_SCHTASKS`), because it also runs on the bot PC during install and upgrade.
- **From v1.2.0:** nothing is installed yet, so install v1.3.0 fresh (START_HERE.md). Later versions use Upgrade.bat.

## 1.2.0 - 2026-09-26 (config 1.2.0) - Windows edition

The bot now runs on the owner's Windows PC instead of Grok Bot. Strategy, risk rules and all
trading logic are unchanged.

- Scheduling: new `schedule install|remove|list|show` command registers the routines in Windows Task
  Scheduler (`\btcperp\` folder, runs `pythonw.exe` without a console, as the logged-on user, wakes the PC,
  starts late after a missed start, never overlaps; HKT converted to the PC's time zone; first run is the
  next future time). `schedule install` starts the dashboard task; `schedule remove` stops it.
- Dashboard: new `dashboard` command, `http://127.0.0.1:8765` (loopback only, Host check, per-server token
  for POSTs, exclusive port on Windows). Read-only; shows state, equity, kill switches, position with SL/TP,
  latest decision, equity curve, statistics, trades, alerts, runs, calendar and shadow results.
- New read-only `snapshot` command (new append-only table `dash_snapshots`) for the dashboard's
  "refresh from exchange": no orders, no state change; 5 s lock wait; failures logged, never alerted.
- Notifications: every alert (every trade, SL/TP change, kill switch, warning, error, lock timeout) pops up a
  Windows toast (`notifications.windows_toast: true`); the dashboard lists all alerts with read/unread.
  `alerts` still prints unread alerts. Telegram stays off.
- `windows\*.bat` shortcuts (CRLF, ASCII): install, secrets, smoketest, go-live, dashboard, status, pause,
  kill, resume, alerts, daily report, schedule check/remove. Kill, resume, go-live and remove ask for a typed
  confirmation.
- `install.py`: Windows next steps; stops/restarts the background dashboard during an upgrade; removes stale
  files in `windows\` too. `run.py` forces UTF-8 output. Logging skips the console under `pythonw.exe`.
- Alert texts and docs no longer refer to Grok Bot. START_HERE.md rewritten in Traditional Chinese for Windows.
- Tests: +29, 164 in total (Task Scheduler XML, time conversion, toasts, snapshot read-only, dashboard summary / Host / token
  / refresh throttle, quiet snapshot failures). Two redaction tests now also read the SQLite WAL file.
  The test suite never pops real toasts (`BTCPERP_NO_TOAST`).
- Moving from Grok Bot: stop the Grok Bot routines first (only one computer may run the bot), then install
  this version on Windows (START_HERE.md). Copy `data\` from the old computer only if you want to keep the
  history; otherwise start fresh.

## 1.1.0 - 2026-09-26 (config 1.1.0)

Changes from the independent review (full item-by-item list in `REVIEW_v1.1.0.md`). Strategy rules unchanged.

- B1: no FOK retry in the same run after a rejected or unknown result; a retry needs proof (terminal
  not-filled status, no opening fill, flat position). A fill seen late is recorded and protected immediately.
- B2: an active drawdown / kill / equity-floor close is retried on every run while a position remains.
- C4: an order-scoped SL smaller than the position gets a position SL. C5: closes are booked and leftover
  orders cancelled only with evidence (exit fills, fired trigger, or two reads apart). C6: kill switches are
  re-checked between a flip's close and its entry. C7: 1000 daily candles (EMA200 within 0.1%).
- C8: one fixed equity source (`total_account_value`) for peak and current; disagreement blocks entries;
  unreadable equity skips the kill switches for that run. C9: pending deposits/withdrawals skip the
  drawdown/floor checks and block entries; flows while holding are alerted.
- D10 missing liquidation price fails the check; D12 adopted positions: entry day from the opening fill,
  only active TP/SL ids, over-budget risk pauses; D13 unknown SL re-place is re-read before closing;
  D14 secrets redacted in the database and error alerts; D16 shadow R includes funding and entry slippage;
  D17 smoketest adds a short and a two-step flip; D18 region note corrected.
- E (PENDING USER DECISION, in config): notional cap 30%, losing-streak kill 8%, new equity floor at 75% of
  net funded capital (hard stop; `resume` cannot clear it, only a new config version).
- A2: withdrawal probe off by default (`smoketest --probe-withdrawal`). G: smoketest records bracket partial
  rejection, read delay after close, raw geoblock, raw liquidation price; new `flowwatch` command.
- START_HERE: every trade must be pinged; no deposits/withdrawals while holding.
- Upgrade: pause -> unzip -> install.py -> status -> resume after confirmation. Run the smoketest again.

## 1.0.1 - 2026-09-26 (config 1.0.1)

- Telegram notifications turned off (`telegram.enabled: false`). Grok Bot is the alert channel.
- New command `alerts`: prints every new alert once (`PING OWNER ...`) for Grok Bot to forward, and
  marks it delivered (new append-only table `alert_deliveries`). Errors are stored as alerts too.
  Every other command ends with `NEW ALERTS FOR OWNER: n` when alerts are waiting.
- Reports are printed and saved; Grok Bot forwards them (START_HERE.md section 4).
- The daily decision is no longer sent as an alert (it is in the decision log and the daily report).
- Telegram /pause /kill /status no longer used: tell Grok Bot "pause", "stop" or "status".
- `.env` no longer needs TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. Strategy and risk unchanged.
- Upgrade: pause -> unzip -> install.py -> status -> resume after confirmation; add `alerts` after
  every routine (START_HERE.md section 4).

## 1.0.0 - 2026-09-26 (config 1.0.0, calendar 2026-09-26)

Initial release.

- Commands: decide, manage, report daily/weekly/monthly, backup, status, pause, kill, resume, selftest,
  smoketest; file lock, logging, run log, Telegram alerts and non-zero exit codes on error.
- Hybrid score (trend + structure/CLV) on closed UTC daily candles; size tiers 25/50/100%; gates: EMA200
  regime, 4h EMA20/50, Binance funding percentile (365d point-in-time), FOMC/CPI/NFP event window (DST-aware),
  region check.
- Execution: FOK limit bracket entry (SL 1.5 ATR, TP 3 ATR, mark-triggered) with one retry; two-step flips;
  deterministic client order ids; order status confirmation; interrupted-run recovery; SL re-placement on
  every run; leftover orders cancelled by id; never cancel-all or auto-cancel.
- Risk: 1.5% fixed risk at the 100% tier with a 10-trade half-risk ramp, 3x isolated, 50% notional cap,
  liquidation distance check, drawdown and losing-streak kill switches, expectancy warning, key expiry alert.
- Append-only SQLite logging of decisions, orders, fills, trades, market data, equity, runs; daily CSV,
  weekly zipped CSV, monthly statistics; shadow tracking (gate trades, V2 breakeven, FLAT-allowed, ungated).
- Calendar: FOMC through 2027; CPI/NFP through 2026-12 (2027 BLS schedule not yet published).
