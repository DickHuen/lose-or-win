# btcperp - BTC-PERP bot for Polymarket Perps

Daily hybrid trend/structure strategy on BTC-PERP with exchange-side bracket stop-loss/take-profit,
fixed-risk sizing, gates, kill switches, full append-only logging and alerts delivered through Grok Bot.
It runs headless on Linux (Grok Bot's computer) from scheduled routines; see `START_HERE.md`.

## Commands (`python3 run.py <command>`)

| Command | What it does |
|---|---|
| `decide` | 08:30 / 08:50 HKT. Reconcile, then (only inside 08:30-09:30 HKT) compute the score from closed UTC daily candles, apply gates, log the decision and intent, and enter/flip/close. Idempotent: the second run completes or skips. |
| `manage` | 12:30, 16:30, 20:30, 00:30, 04:30 HKT. Reconcile, complete a planned close, log position/market data. Never opens. |
| `report daily` / `weekly` / `monthly [--month YYYY-MM] [--only-first-sunday]` | Printed reports (Grok Bot forwards them); daily CSV export; weekly zipped CSV of all logs; monthly statistics file. |
| `alerts` | Print every new alert once (`PING OWNER ...`) and mark it delivered. Grok Bot runs it after every routine and pings the owner. |
| `backup` | SQLite backup (+config) into `data/backups/` (last 14 + first of each month kept). |
| `status` | State, position, SL/TP, equity, drawdown, kill switches, last decision. |
| `pause` | Stop new entries; keep position and SL/TP. |
| `kill` | Close the position with a reduce-only order, cancel its TP/SL by id, and pause. |
| `resume` | Clear pause and kill switches (only when the owner asks). Resets the drawdown peak and the losing-streak count. |
| `selftest` | Run the unit tests (mocked exchange, no network). |
| `smoketest [--probe-withdrawal]` | Live minimum-size test (see START_HERE.md section 3). The withdrawal probe is off unless the flag is given. |
| `flowwatch [--minutes N]` | Record balances / deposit-withdrawal statuses while the owner makes a small deposit (only when flat). |

Every command takes an exclusive file lock (`data/btcperp.lock`), logs to `logs/btcperp_YYYY-MM-DD.log`
(secrets redacted), writes a start/end row to the run log, and on error exits non-zero and sends a
stored alert with the last log lines (shown by `alerts`). Telegram is optional and off (`telegram.enabled`).

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
   -> pause, keep SL/TP; equity below 75% of net funded capital -> close and hard stop (`resume` cannot clear
   it; only a new config version); a failed kill close is retried every run; pending deposits/withdrawals skip
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
- One entry per UTC day. Entry window 08:30-09:30 HKT; after that "missed", no late entry.
- Entry: FOK limit at best bid/ask +/- 10 bps with bracket SL 1.5 x ATR / TP 3 x ATR (mark-triggered,
  full size); one retry; then no entry that day.
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
`shadow_log`, `alerts`, `alert_deliveries`, `telegram_updates`, `flows`.

Shadow tracking (simulation only): each day a gate blocked or reduced a trade gets a hypothetical trade
(bracket outcome on Polymarket 1h candles); parallel variants `live_rules`, `v2_breakeven` (one-time SL
to breakeven at +1 ATR), `flat_allowed` (|score| < 30 = flat) and `ungated` are replayed from the logged
decisions.

## Layout

```
run.py            launcher (uses ./venv)          install.py     installer / upgrader
config/           config.yaml, calendar.yaml       perpbot/       code
tests/            unit tests (selftest)            API_NOTES.md   exchange API notes (untested items)
data/  logs/      created at install, never in the zip, never touched by upgrades
.env              secrets (created from .env.example), never in the zip
```

Exchange access uses Polymarket's official Python SDK (`polymarket-client`, pinned) with the proxy signer
only; the main wallet key is never used. See `API_NOTES.md`.
