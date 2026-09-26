# Changelog

Each version ships as `btcperp_vX.Y.Z.zip`. Code version = `VERSION`; config version = `config_version`
in `config/config.yaml`. Every log row records both, and reports never mix versions.

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
