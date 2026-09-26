# btcperp - BTC-PERP bot for Polymarket Perps

Daily hybrid trend/structure strategy on BTC-PERP with exchange-side bracket stop-loss/take-profit,
fixed-risk sizing, gates, kill switches, full append-only logging, a local dashboard and Windows
desktop notifications. It runs on the owner's Windows PC from Windows Task Scheduler; see `START_HERE.md`
(Traditional Chinese). The code is cross-platform (the unit tests also run on Linux).

## Windows shortcuts (`windows\*.bat`)

| Shortcut | What it does |
|---|---|
| `1_Install.bat` | First install |
| `Upgrade.bat` | Upgrade from the newest zip in Downloads (asks for `UPGRADE`) |
| `Proxy_Key.bat` | Proxy key; the main wallet only signs |
| `Edit_Secrets.bat` | Open .env in Notepad |
| `2_Smoketest.bat` | Smoketest: YES / W (+ withdrawal probe) / R |
| `Backtest.bat` | Backtest (asks for `CONFIRM` on the criteria the first time) |
| `3_Schedule_Install.bat` | Go live (asks for `GO`; needs a passing full smoketest) |
| `Dashboard.bat` | Open the dashboard |
| `Status.bat` | Status |
| `Pause_New_Entries.bat` | Stop new entries |
| `Unpause.bat` | Remove your manual pause only |
| `Kill_Close_Position.bat` | Close the position now (asks for `KILL`) |
| `Resume.bat` | Clear pauses (asks for `RESUME`, plus `RESET-PEAK` for a kill switch) |
| `Alerts.bat` | Print unread alerts |
| `Report_Daily.bat` | Daily report now |
| `Schedule_Check.bat` | Show the scheduled tasks and power settings |
| `Schedule_Remove.bat` | Remove the scheduled tasks (asks for `REMOVE`) |

Each one runs `venv\Scripts\python.exe run.py <command>` from the install folder, and refuses to run from any
other copy of the folder.

## Commands (`python run.py <command>`)

| Command | What it does |
|---|---|
| `decide` | 08:30 / 08:50 HKT. Reconcile, then (only inside 08:30-09:30 HKT) compute the score from closed UTC daily candles, apply gates, log the decision and intent, and enter/flip/close. Idempotent: the second run completes or skips. New positions are blocked when the clock differs from the exchange by more than 30 s, or when the economic calendar's coverage has ended (closes still run). After the window with a position open and no decision yet: a late decision on the same data runs the close rules only. |
| `manage` | 12:30, 16:30, 20:30, 00:30, 04:30 HKT. Reconcile, complete a planned close, log position/market data. If no decision was made in today's window and a position is open, runs the late close-only decision. Never opens. |
| `report daily` / `weekly` / `monthly [--month YYYY-MM] [--only-first-sunday]` | Reports printed and saved in `data/reports/`; daily CSV export; weekly zipped CSV of all logs; monthly statistics file. |
| `alerts` | Print every unread alert once (`PING OWNER ...`) and mark it read (same list as the dashboard). |
| `snapshot` | Read-only exchange read for the dashboard (position, SL/TP, mark, equity). No orders, no state changes, no kill checks. Waits at most 5 s for the lock (skips if a run is busy); failures are logged, never alerted. |
| `dashboard [--port N] [--no-browser]` | Local web dashboard on `http://127.0.0.1:8765` (loopback only). No lock, no exchange access; reads the database. Its "refresh" button runs `snapshot`. |
| `schedule install\|remove\|list\|show [--dry-run] [--no-dashboard] [--upgrade]` | Windows Task Scheduler tasks under `\btcperp\`. Daily tasks are pinned to HKT (`+08:00`). `install` needs a passing full smoketest of this version with this proxy key; `--upgrade` re-registers tasks that already run from this folder. `show`/`list` also print the sleep and wake-timer settings. No lock. |
| `proxykey new [--days N] [--offline] \| finish [--signature] \| status` | Create a proxy key here; the main wallet only signs the EIP-712 CreateProxy message, in a browser wallet (one-off page on 127.0.0.1:8766) or on another computer. Writes .env. |
| `backtest download \| criteria \| confirm \| run` | See BACKTEST.md. `run` refuses until the owner has confirmed the current criteria file (SHA-256 recorded). Never trades; no lock. |
| `backup` | SQLite backup (+config) into `data/backups/` (last 14 + first of each month kept). |
| `status` | State, position, SL/TP, equity, drawdown, kill switches, last decision. |
| `pause` / `unpause` | Stop new entries (position and SL/TP kept) / remove only that manual pause. |
| `reasons` | Print the active pause reasons and what clears each one. |
| `kill` | Close the position with a reduce-only order, cancel its TP/SL by id, and pause. |
| `resume [--reset-peak]` | Clear pauses (only when the owner asks). A drawdown / losing-streak kill needs `--reset-peak` (exit code 6 without it): the peak is reset and the streak restarts. The equity floor needs a new config version stating `risk.equity_floor_reset_baseline_usd`. |
| `selftest` | Run the unit tests (mocked exchange, no network). |
| `smoketest [--no-trade] [--probe-withdrawal]` | Live minimum-size test (START_HERE.md section 4). Records the exchange's status for an unfillable FOK. The withdrawal probe runs only with the flag. |
| `flowwatch [--minutes N]` | Record balances / deposit-withdrawal statuses while the owner makes a small deposit (only when flat). |

Every bot command takes an exclusive file lock (`data/btcperp.lock`), logs to `logs/btcperp_YYYY-MM-DD.log`
(secrets redacted), writes a start/end row to the run log, and on error exits non-zero and stores an
alert with the last log lines. Every alert is shown on the dashboard.

After the run, once the lock is released, Windows toasts are sent (at most 5 per run), so a slow
notification can never delay a trading action. Toasts use `notifications.windows_toast` (PowerShell WinRT,
text passed by environment variables). `decide` / `manage` also ping the optional heartbeat URL
(`HEALTHCHECK_PING_URL`), or `<url>/fail` on an error. Telegram is optional and off (`telegram.enabled`).
Exit codes: 0 ok, 1 error, 3 config / secrets / wrong folder, 4 lock busy, 5 selftest, 6 needs a typed
confirmation.

## Scheduling (Windows Task Scheduler)

`schedule install` writes one task XML per routine to `data/tasks/` and registers it with
`schtasks /Create /XML` (folder `\btcperp\`). Tasks run `venv\Scripts\pythonw.exe -m perpbot <command>`
(no console window) in the install folder, as the logged-on user (no stored Windows password: the user
must be logged on; a locked screen is fine), wake the computer, start as soon as possible after a missed
start (a late `decide` is logged as missed and never enters late), and queue instead of overlapping.
The first run of each task is its next future time, so registering never triggers a "missed" run.
If the PC is not on HKT, times are converted; the monthly report then runs weekly and
`--only-first-sunday` checks the HKT date. The dashboard task starts at logon.

## Dashboard

`http://127.0.0.1:8765`, bound to 127.0.0.1 only (exclusive port bind on Windows). The Host header must be
`127.0.0.1:<port>` or `localhost:<port>` (blocks DNS rebinding), and the two POST actions ("refresh from
exchange", "mark alerts read") need a per-server random token embedded in the page. Cards: state and pause
reasons, equity, drawdown / losing streak / equity floor against their limits, position with SL/TP and
liquidation price (latest `snapshot` or `manage`), latest decision with score components and gates, equity
curve with peak, statistics, trades, alerts, runs (last result per command, missed/late in 48 h, errors in
7 days), economic calendar and shadow variants.

## Every trading run starts with reconcile

1. Exchange position vs local trade record: a close that happened intraday (TP/SL/liquidation) is booked
   from the fills (net PnL after fees and funding) into the kill-switch statistics; an unknown position is
   adopted (an interrupted entry is recognised by its client order id).
2. If a position is open, an active SL must exist on the exchange; if not, a position SL is re-placed at the
   recorded price; if that fails (or the mark is already beyond it) the position is closed with a reduce-only
   order; if that fails too: "CLOSE FAILURE" alert.
3. When flat, leftover TP/SL/reduce-only orders are cancelled by id (never cancel-all, never auto-cancel).
4. `cumulative_funding` is recorded; fills and funding payments are synced.
5. (Only if Telegram is enabled in config: /pause, /kill, /status from the configured chat id.)
6. Equity log and kill switches. Equity = the exchange's `total_account_value` for both peak and current
   (wallet + uPnL is a cross-check; disagreement blocks new entries). Drawdown 15% from peak (confirmed
   deposits/withdrawals adjust the peak) -> close and pause; losing streak with cumulative loss of 8% of equity
   (a trade within +/-0.1% of equity at entry is a tie: it neither ends nor extends the streak, review D11)
   -> pause, keep SL/TP; equity below 75% of net funded capital -> close and hard stop (`resume` cannot clear
   it; only a new config version that states the new baseline); a failed kill close is retried every run; pending deposits/withdrawals skip
   the drawdown/floor checks and block entries; 25-trade size-weighted expectancy < 0 -> warning.
   Proxy key expiry alert 5 days ahead. (8%, 75% and the 30% notional cap are pending the owner's decision.)
7. A missing position on one read is not trusted: closes are booked and leftover orders cancelled only with
   evidence (exit fills, fired trigger, or two reads apart).

## Strategy (all numbers in `config/config.yaml`)

- Score = Trend (50 x clip((C - EMA50) / (2 x ATR14), -1, 1)) + Structure (+/-25 breakout of previous
  day high/low, + 25 x CLV; CLV = 0 if H = L), from closed UTC 00:00 daily candles (Binance BTCUSDT).
  Score exactly 0 (after rounding to 4 decimals) = no signal.
- Size tier by |score|: < 30 -> 25%, 30-50 -> 50%, > 50 -> 100% of the risk budget.
- Gates (caps: minimum, never multiplied): EMA200 regime against signal -> cap 50%; last closed 4h candle
  (20:00-00:00 UTC) EMA20/EMA50 disagrees -> cap 50%; Binance funding 365-day point-in-time percentile
  > p95 no new long, < p5 no new short; event window (08:30 HKT before an FOMC/CPI/NFP release until
  release + 4 h, DST-aware) -> no new positions, no flips; region blocked -> no entry.
- Holding: opposite |score| >= 30 -> flip (reduce-only IOC close, confirm flat with no leftover orders,
  then FOK bracket entry); weaker opposite -> hold; 3 consecutive UTC days of opposite signal -> close and
  re-enter per today's signal and gates; crowded side of extreme funding -> close and re-enter per gates.
- One entry per UTC day. Entry window 08:30-09:30 HKT; after that "missed", no late entry (the day's close
  rules are still evaluated late on the same data, close part only).
- Entry: FOK limit at best bid/ask +/- 10 bps with bracket SL 1.5 x ATR / TP 3 x ATR (mark-triggered,
  full size); `exits.entry_attempts` = 1 until the smoketest has recorded the real "FOK not filled" status; then no
  entry that day.
- Risk: 1.5% of equity at SL for the 100% tier (first 10 live trades: half), leverage 3x isolated
  (checked/set before every entry; failure = no trade), notional <= 30% of equity (pending decision),
  liquidation price must be >= 2 x SL distance away (pre-trade estimate and post-fill check on the
  exchange's value; a missing liquidation price fails the check).
- A rejected or unknown FOK is never retried in the same run (the SDK reports a whole bracket as rejected
  even if the entry row filled); the retry needs proof the first order did not fill.

## Data (`data/btcperp.sqlite3`, append-only; UPDATE/DELETE blocked by triggers)

Every row has UTC and HKT timestamps, config version and code version. Tables: `runs`, `decisions`
(all raw inputs, score breakdown, gates, plan and reasons), `intents`/`intent_events` (the daily plan,
written before any order, and its steps), `orders` (requests, responses, status checks, fills, slippage,
latency), `fills`, `funding_payments`, `trades` (open/update/close with exit reason, MAE/MFE, holding
hours, fees, funding, net PnL, R), `manage_log`, `equity_log`, `state_log`, `position_snapshots`,
`market_snapshots` (mark, index, funding, spread, depth), `pm_klines_1h`/`pm_klines_1d`/`pm_funding`
(our own Polymarket dataset, backfilled every run), `bn_klines_1d`/`bn_klines_4h`/`bn_funding`,
`shadow_log`, `alerts`, `alert_deliveries`, `dash_snapshots`, `backtest_log`, `telegram_updates`, `flows`.

Shadow tracking (simulation only): each day a gate blocked or reduced a trade gets a hypothetical trade
(bracket outcome on Polymarket 1h candles); parallel variants `live_rules`, `v2_breakeven` (one-time SL
to breakeven at +1 ATR), `flat_allowed` (|score| < 30 = flat) and `ungated` are replayed from the logged
decisions.

## Layout

```
run.py            launcher (uses ./venv)          install.py     installer / upgrader
config/           config.yaml, calendar.yaml       perpbot/       code
tests/            unit tests (selftest)            API_NOTES.md   exchange API notes (untested items)
windows/          .bat shortcuts                   START_HERE.md  setup guide (Traditional Chinese)
BACKTEST.md       backtest design + criteria       REVIEW_v*.md   committee reviews and what was done
data/  logs/      created at install, never in the zip, never touched by upgrades
.env              secrets (created from .env.example), never in the zip
```

Exchange access uses Polymarket's official Python SDK (`polymarket-client`, pinned) with the proxy signer
only; the main wallet key is never used. See `API_NOTES.md`.
