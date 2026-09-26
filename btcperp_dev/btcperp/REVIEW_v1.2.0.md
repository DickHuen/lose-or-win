# Committee review of v1.2.0: what was done in v1.3.0

The committee reviewed v1.2.0 and proposed items 1-21 plus a go/no-go list.

Process:
1. The developer (Claude Code) gave a view on each item.
2. The owner decided: "do what the developer agreed with; bankroll unchanged (fixed 1.5% risk, no dynamic sizing in the backtest); no Telegram".
3. v1.3.0 implements that.

Strategy numbers are unchanged (`strategy.py` scoring, gates and tiers).

| # | Item | Decision | What v1.3.0 does | Tests |
|---|---|---|---|---|
| 1 | B3 backtest before going live | Agreed. Deferring it in v1.1.0 was a mistake. | `backtest` command and `Backtest.bat` (runs on the owner's PC from downloaded Binance data), described in BACKTEST.md. Decisions go through the live code path (`day_features` + `plan_for`) with exactly the live data slices. Pre-registered variants: A (live), B (breakeven), C (control); each also with crowded funding held instead of closed; plus a no-event-gate sensitivity run. Six-month windows, start offsets 0/7/14/21/28 days, 14-day pause after a kill switch, config values unchanged. Pass/fail rules are in `config/backtest_criteria.yaml`; the owner confirms their SHA-256 before the run (`backtest run` refuses otherwise). Historical event calendar: `config/calendar_history.yaml`, compiled without internet access, to be verified by the committee. | test_backtest.py: decisions equal live `decide` on 20+ days, field by field; deterministic (same hash twice); stop-loss checked first; costs = `shadow._r`; kill pause and resume; control and breakeven variants; criteria gate in the CLI. |
| 2 | Missed decide: close rules must still run | Agreed; confirmed in the code. | A late `decide`, or the first `manage` after the entry window with no decision that day and a position open, computes the day's decision. It uses data closed at 00:00 UTC and the events at 08:30 HKT, runs the **close part only** and never enters. The decision is marked `late`. If it fails, `manage` alerts and still protects the position. | test_review_v120 item2 ×4 |
| 3 | D11 tie rule | Agreed. Needs the owner's written confirmation (go-live item 7). | `risk.losing_streak_tie_pct: 0.1`. A trade within ±0.1% of equity at entry neither ends nor extends a losing streak, and the backtest uses the same rule. | item3 ×2 |
| 4 | Phone push (Telegram) | Gap acknowledged; **the owner declined Telegram**. | Not enabled. Item 5 (heartbeat) covers "the bot stopped", and the dashboard and Windows notifications cover alerts at the PC. The go/no-go list records the owner's decision. | - |
| 5 | Heartbeat / dead-man's switch | Agreed. | Optional `HEALTHCHECK_PING_URL` in .env (for example a free healthchecks.io check with a 6-hour period). After every `decide` / `manage` the bot calls the URL, or `<url>/fail` on an error. The call sends no data, has a 5 s timeout, happens after the lock is released, never raises, and the URL is redacted from logs. | item5 ×3 |
| 6 | A slow notification delays the stop-loss | Agreed; confirmed in the code. | The engine only stores alerts during a run. Notifications are sent after the run, once the lock is released. Toasts are started without waiting (`Popen`); at most 5 per run, then a summary toast. | item6 ×2; toast test updated |
| 7 | Upgrade flow cleared kill switches; floor re-based on the current equity | Agreed, both points. | New `unpause` / `Unpause.bat` removes only the manual pause; the upgrade flow uses it. `resume` lists the active reasons, and a drawdown or losing-streak kill needs `RESET-PEAK` (exit code 6 otherwise). The equity floor clears only with a new config version that states `risk.equity_floor_reset_baseline_usd`; it is never re-based automatically. | item7 ×5; floor test updated |
| 8 | Upgrade while the scheduled bot runs | Agreed with the goal; the fix is changed. Files are replaced when the zip is extracted, before install.py runs. | New `Upgrade.bat`: `install.py --from-zip` disables the tasks, waits for the bot lock, then extracts, installs and tests. It re-registers the tasks only after the tests pass; if they fail, the tasks stay disabled and it says so. | item8 ×3 |
| 9 | Calendar coverage expiry | Agreed. | When any event type's coverage has ended, new positions are blocked (closes still run) and an alert is raised. | item9 ×2 |
| 10 | FOK retry before the real status is known | Agreed (cheap; the retry already failed safe). | `exits.entry_attempts: 1`. New smoketest step `fok_unfilled_status` places a FOK that cannot fill and records the exchange's raw status. | item10 ×2 |
| 11 | Main wallet key never on the bot PC | Agreed; possible per the official SDK. | `proxykey` command and `Proxy_Key.bat`. The proxy key is generated on the bot PC. The main wallet only signs the EIP-712 CreateProxy message, either in a browser wallet (hardware wallet recommended) via a one-off local page, or on another computer (`sign.html` or `perpbot/offline_sign.py`). The bot PC registers the signature and writes .env. | test_proxykey.py ×7, including the offline signature equal to the SDK signer |
| 12 | Withdrawal probe before going live | Agreed. | `2_Smoketest.bat` option **W** (`smoketest --probe-withdrawal`) is on the go-live checklist. | existing |
| 13 | Task times fixed to HKT | Agreed for the daily tasks. | Daily tasks use `StartBoundary …+08:00`. Weekly and monthly reports keep the converted local time (only reports). | winsched tests updated |
| 14 | Sleep and wake timers | Agreed. | START_HERE requires "sleep: never". `schedule show/list` prints the power plan's sleep and wake-timer settings with warnings. | - |
| 15 | Clock skew | Agreed. | Each decision compares the clock with the exchange server time. If it is more than 30 s off, or unreadable, new positions are blocked and an alert is raised. | item15 ×3 |
| 16 | Wrong copy of the folder | Partly agreed (simple version). | `schedule install` records the install folder. Manual commands (pause, kill, resume, smoketest, dashboard, proxykey, backtest and others) refuse to run from any other copy. | item16 |
| 17 | Missed-decision days per month | Agreed, but the month is marked rather than excluded. | The monthly report lists days without an on-time decision. More than 2 marks the month "INCOMPLETE" and raises an alert. | item17 |
| 18 | Guidance for long periods offline | Agreed. | START_HERE: more than 24 h away, pause; more than 72 h (and no heartbeat), kill. | - |
| 19 | LogonTrigger user | Agreed. | `UserId` (DOMAIN\user) on the principal and the logon trigger. | - |
| 20 | Dashboard CSP; no trading controls | Agreed. | `Content-Security-Policy` and `Referrer-Policy` headers; the docstring states the dashboard never gets trading controls. | item20 |
| 21 | Go-live only after a smoketest | Agreed. | `schedule install` requires the latest **full** smoketest to have passed, with this code version and this proxy address (`--upgrade` re-registers existing tasks). | item21, item8 |

Also changed:
- Alert texts no longer mention Grok.
- The weak redaction tests now read the SQLite WAL file.
- `tools/create_proxy_key.py`, which needed the main key typed in, is removed.

Go/no-go (committee list), status at v1.3.0:
1. B3 backtest: code ready. The committee reviews the criteria and verifies the historical calendar, the owner confirms, then the owner runs it and sends the result. **Open.**
2. Items 2, 3, 6, 7, 8 and 9: done, with tests.
3. Item 4: declined by the owner (no Telegram). Item 5: needs the owner's healthchecks.io check, with a real test. **Open.**
4. Minimum-size smoketest (full, W option): **open**. It records the FOK-unfilled status, clock skew, raw geoblock, balances and the raw liquidation price.
5. Proxy key via Proxy_Key.bat without the main key on the PC, and the withdrawal probe rejected: **open.**
6. PC settings: sleep never; Windows Update active hours; "use my sign-in info to finish setting up after an update"; one restart test. **Open.**
7. The owner confirms E (30% / 8% / 75%) and D11 (±0.1%) in writing. **Open.**
