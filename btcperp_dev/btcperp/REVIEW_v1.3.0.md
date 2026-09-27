# Committee review of v1.3.0: what was done in v1.4.0

中文摘要：
- 委員會 v1.3.0 審核嘅「CONFIRM 前」同「上實盤前」項目，v1.4.0 全部做咗，每項都有測試。
- 有三處同委員會原文唔同，下面有講原因：C0 持倉期間缺數據（容許好短嘅缺口，並全部列出）；BT3 funding 加倍（只加倍要付嘅 funding）；P7 撤銷舊 proxy（bot 電腦做唔到）。
- 準則檔已改成 draft-2（C0 至 C7、I1 至 I7）。**委員會睇過 draft-2 同今次改動之前，唔好打 CONFIRM。**
- 策略數字冇改。Bankroll 維持固定 1.5% 風險。冇 Telegram。

The committee reviewed v1.3.0 and gave a list of changes: some before the owner confirms the backtest criteria
(CONFIRM), some before going live. It also gave a go/no-go list.

Process:
1. The owner said: "change what you agree with; decide the rest yourself".
2. The developer (Claude Code) agreed with nearly every item. Three are done differently, with the reasons below.
3. v1.4.0 implements all of it.

Strategy numbers are unchanged (`strategy.py` scoring, gates and tiers). The committee ran nothing, so there are
still no backtest results.

## Calendar

| Item | Decision | What v1.4.0 does | Tests |
|---|---|---|---|
| 193 dates verified (FOMC 49, CPI 72, NFP 72; 2020-09 to 2026-09) | Accepted with thanks. | `calendar_version: "history-2026-09-27-verified"`; every `verify` flag removed; the header records the committee's check. | `test_calendar_history_is_marked_verified` |

## Criteria (`config/backtest_criteria.yaml`, now draft-2)

| Item | Decision | What draft-2 says |
|---|---|---|
| C0 data completeness | **Agreed, adjusted (see below).** | C0a: 1h candles missing ≤ 0.1% over the test period. C0b: no single gap longer than **3 h** while a position is open. C0c: at most **6 h** missing while holding in any full-period run. Every gap is listed in the report; funding gaps over 8 h are listed. |
| C1 (a)–(d) | Agreed, all four. | C1a: every offset ≥ +0.05 R. C1b: the stress twin > 0 R at every offset. C1c: ≥ 2 of the 3 segments with a positive median. C1d: t-statistic ≥ 2.0 on the median-offset run. |
| C2 | Agreed. | C2a: median share of positive windows > 50%. C2b: median 6-month window return > 0%. |
| C3 | Agreed. | Worst window drawdown ≤ 20%, measured hourly (BT2). |
| C4 | Agreed. | Equity floor never hit. |
| C5 | Agreed. | Median full-period trades ≥ 60. |
| C6 | Agreed. | Full period, including automatic resumes: worst drawdown over offsets ≤ 25%. |
| C7 | Agreed. | Median drawdown kill-switch triggers per full-period run ≤ 3. |
| I2, I4 | Agreed. | I2 reports the value only. I4: share of trades capped by the 30% notional cap ≤ 0.5, otherwise review the cap. |
| I5 | Agreed. | Long and short expectancy and counts, per offset and segment. "Short negative at every offset" is flagged for the committee; no automatic change. |
| I6 | Agreed. | Distribution of the rolling 30-trade expectancy; its 5th percentile becomes the live review line (S9). |
| I7 | Agreed. | A_live replayed on Polymarket's own 1h candles where they exist, trade by trade against Binance. Exit agreement (same reason, same day) ≥ 90%. |

**C0, why "≤ 3 h each and ≤ 6 h in total" instead of "none at all while holding":**
- Binance's public 1h history has a few known holes (exchange maintenance and outages).
- With "none at all", one maintenance hour during any trade in 2020–2026 would fail C0. The only fix would then be to edit the data.
- The risk behind the committee's rule is real. During a missing hour the price can touch the stop-loss and recover, and the backtest then misses a loss.
- v1.4.0 limits that risk in three ways:
  - every gap fill is conservative: after a gap, a stop the next open has passed fills at that open (BT1);
  - one gap can last at most 3 h, and all gaps in a run at most 6 h;
  - the report lists every gap with its time and length.
- If the committee wants zero, change C0b and C0c to `value: 0` **before** CONFIRM. It is a one-line change and needs no code.

## Changes before CONFIRM

| # | Decision | What v1.4.0 does | Tests |
|---|---|---|---|
| BT1 SL gap fill | Agreed. | When a 1h candle opens beyond the stop, the fill is that open (worse than the SL), then exit slippage. | `test_bt1_gap_through_stop_fills_at_the_open` |
| BT6 margin cap | Agreed (with BT1). | The loss of an isolated position is capped at its margin. | `test_bt6_isolated_loss_capped_at_margin` |
| BT2 intraday drawdown | Agreed. | Equity is marked to market on every 1h candle while holding, at the candle's adverse extreme. Drawdowns, C3 and C6 use it. | `test_bt2_intraday_drawdown_is_counted` (12% intraday dip counted) |
| BT3 stress costs | **Agreed, one detail differs.** Stress twins with fees ×2 and 30 bps exit slippage. Funding: "absolute value ×2" is done as **funding paid ×2, funding received unchanged**. Doubling received funding would make the stress run cheaper on some trades, and the committee's own test says stress must cost strictly more on every trade. The fee rate is the larger of the real taker fee from the latest smoketest and 0.05%. | Every candidate variant has a `_stress` twin; `backtest run` reads the smoketest fee. | `test_bt3_stress_twin_costs_more_on_every_trade`, `test_cli_backtest_fee_never_below_smoketest` |
| S5 pre-registered choice | Agreed, as written. | `select_variant`: live uses A_live. Another variant replaces it only if (a) it passes C0–C7, (b) it beats A_live's total R at every offset, (c) it beats A_live in ≥ 2 of 3 segments, (d) its stress twin beats A_live_stress at every offset. If several qualify, the one with the best worst offset is proposed, and the committee reviews it. If A_live fails, nothing is approved automatically: a new meeting and a shadow forward period follow. BACKTEST.md states the rule. | `test_s5_variant_selection_rules` |
| S6 windows at full risk | Agreed. | Window runs use full risk from trade 1. Only the chained full-period run keeps the half-risk ramp for the first 10 trades. | `test_s6_windows_full_risk_full_period_ramp` |
| R1 lock everything | Agreed. | `backtest confirm` records a manifest of: the criteria file, config (strategy, gates, exits, risk, backtest, the Binance history lengths, shadow fee and breakeven settings), both calendars, `VERSION`, the code files (backtest, strategy, risk, indicators, shadow, calendar), a fixed `data_end_utc`, the data slice's SHA-256 and the fee rate. `backtest run` refuses any difference and names it. Each run under one confirmation is numbered, and the report prints "run #N under this confirmation"; the committee uses run #1. | `test_cli_backtest_confirm_locks_everything_and_numbers_runs` (changing `start_offsets_days` is refused; the second run shows #2) |
| R2 data hashes and gaps | Agreed. | The report lists a SHA-256 of each data series (the exact slice used), the missing candles per series, funding gaps over 8 h and the gaps while holding. C0 is implemented. | `test_c0_gaps_while_holding_are_recorded` (one deleted 1h row is listed and C0 fails) |
| S9 live review line | Agreed. | `risk.live_review_min_trades: 30`, `live_review_window_trades: 30`, `live_review_expectancy_floor_r: null`. After the backtest, a new config version sets the floor to I6's 5th percentile. From then on, once there are 30 live trades, a rolling 30-trade expectancy below the line pauses new entries (`live_review`, a hard stop) and alerts. It is checked once per new trade. The open position keeps its stop-loss. | `test_s9_live_review_pauses_below_backtest_line_once_per_new_trade`, `test_s9_line_not_set_means_no_check` |
| BT4 untruncated streaks | Agreed. | New `A_live_nokill` sensitivity variant; I3b reports its losing-streak p99. | in `test_run_backtest_is_deterministic_and_writes_results` |
| BT5 daily kill check | Agreed. | BACKTEST.md states that the backtest checks kill switches once a day, while live checks 7 times a day, so the backtest is on the conservative side. | - |
| S7, S8 | Merged into I5 and C1(d), as the committee said. | - | - |

## Changes before going live

| # | Decision | What v1.4.0 does | Tests |
|---|---|---|---|
| V4 heartbeat | Agreed; the current state was not acceptable. | Separate checks: `HEALTHCHECK_DECIDE_URL` and `HEALTHCHECK_MANAGE_URL` (the old `HEALTHCHECK_PING_URL` is the fallback). Each run pings `/start` first. At the end it pings `/fail` in three cases: the run failed; a critical alert is unread (SL re-place failed, close failure, kill switches, floors, calendar expired, clock skew, late decision failed, equity unreadable, liquidation check, wallet mismatch, live review, proxy key expiry and others); or a hard stop is active. Otherwise it pings success. "Read" (marked in the dashboard or Alerts.bat) is recorded separately from "delivered". A stuck run shows up because `/start` arrives and nothing follows. START_HERE sets up both checks with HKT cron schedules and 20 minutes grace. | `test_v4_critical_alert_keeps_failing_until_read_and_hard_stop_while_active`, `test_v4_decide_uses_its_own_check` |
| U1 schtasks query | Agreed. | If `schtasks /Query` fails, the upgrade stops before changing any file. | `test_u1_unreadable_task_scheduler_changes_nothing` |
| U2 automatic restore | Agreed (replaces V8). | Before writing, the installed version is copied to `data/upgrade_backup_<old version>_<time>/`. If any step fails (files, pip, tests), it restores the old version, reinstalls its packages (offline, it accepts packages already installed), re-enables the tasks and says so. A failed pip or test step is reported in one line, never as a raw traceback. If even the restore fails, the tasks stay disabled, and a banner of `!!!` lines shows the open position, its stop-loss and the backup folder. | `test_u2_failed_upgrade_restores_old_version_and_tasks` (pip failure and test failure), `test_u2_failed_restore_keeps_tasks_disabled_and_shows_position`, `test_u2_restore_test_mode_installs_then_puts_the_old_version_back` |
| U2 real test (go-live B3) | Added by the developer. | Upgrade.bat `TEST-RESTORE` / `install.py --from-zip X --test-restore`: installs the zip (an equal version is allowed), simulates a failure after its tests pass, restores the installed version and re-enables the tasks. It prints `RESTORE TEST PASSED` only if the old version is back. | `test_u2_restore_test_mode_installs_then_puts_the_old_version_back` |
| U3 version and hash | Agreed. | A zip that is not newer than the installed version is refused. Upgrade.bat shows the zip's SHA-256 to compare with the published value. | `test_u3_older_or_equal_zip_is_refused` |
| P1 option N only with a hardware wallet | Agreed. | Proxy_Key.bat option N explains why and asks the owner to type `HARDWARE`; anything else points to option O. START_HERE's go-live list records which option was used. | - (owner's written confirmation) |
| P2 whitelist | Agreed. | The signer builds the message itself. The primary type is always CreateProxy. The domain is fixed: Polymarket, version 1, chain 137, no verifyingContract. The message has exactly addr, exp, salt and ts, and at most 30 days. The expiry is shown in HKT. The local signing page gets only the four fields; the wallet account must equal the owner given at `new`. | `test_offline_signer_refuses_anything_but_create_proxy` (ERC-20 Permit, chain 1, extra verifyingContract, another name, extra field, changed types, 31 days: all refused); `test_signing_page_builds_the_message_itself_and_escapes` |
| P5 option O | Agreed. | The bot PC writes only `data/proxykey/sign_fields.txt`: addr, exp, salt, ts, owner (plain text, nothing secret). The other computer uses its own copy of the release zip, checked by SHA-256: `perpbot/offline_sign.py` (asks YES, then the key hidden) or `offline_sign/offline_sign.html` (browser wallet, served on 127.0.0.1; it sends nothing). Both rebuild the message from the fields. The v1.3.0 files `sign_request.json` and `sign.html` are no longer made and are deleted. | `test_offline_builder_matches_the_sdk`, `test_offline_signer_script`, `test_static_offline_page_is_self_contained` |
| P6 owner fixed at `new` | Agreed. | `proxykey new` needs the main wallet address (`--owner`, or asked; it must equal `PM_WALLET_ADDRESS` if .env has one) and stores it in the pending request. `finish` accepts only a signature by that address. The old hint to clear `PM_WALLET_ADDRESS` is gone. | `test_signature_from_another_wallet_or_message_is_refused`, `test_request_limits_and_owner` |
| P3 30 days | Agreed. | `--days` is 1 to 30. | `test_request_limits_and_owner`, CLI `--days 31` refused |
| P4 pending expiry | Agreed. | A request not finished within 1 hour is deleted. Every bot command and `proxykey status` checks this, and status shows the request's age. | `test_pending_request_expires_after_an_hour`, CLI test |
| P7 lock; old proxy | **Lock agreed. Revoking is not possible from the bot PC.** | The exchange registration and the .env write happen together under the bot's lock, so they wait for a running bot command (or give up after `lock.wait_seconds` with nothing changed). Revoking a proxy needs a **main-wallet** signature (SDK `revoke_credentials`), and the main wallet never signs on the bot PC. So `proxykey status` lists every registered proxy key with its expiry, marking the one in .env. Old keys expire within 30 days (P3). | `test_registration_waits_for_the_bot_lock`, `test_cli_finish_refuses_while_the_bot_runs`, `test_status_lists_registered_proxies` |
| P8 page race and escaping | Agreed. | A `threading.Lock` lets only one POST reach `finish`; a second POST gets HTTP 409. Page data is escaped (`</` → `<\/`). | `test_local_signing_page_round_trip_and_single_submit` (two simultaneous POSTs: one registration), escaping test |
| F1 permanent floor | Agreed. | `risk.permanent_floor_pct_of_cumulative_funded: 50` (**the owner decides; the committee recommends 50**). The base is the first equity plus every later deposit minus withdrawals; it is never re-based. Below the floor, the bot closes the position and stops for good (`permanent_floor`, hard stop). Only a new config version with `permanent_floor_reset_for` equal to the trigger date restarts it (single use). A config that lowers the % below any value used before is refused, and the bot does not run with it. Resume messages show the cumulative result since the first funding. | `test_f1_permanent_floor_stops_for_good_and_needs_a_dated_config`, `test_f1_new_config_may_not_lower_the_permanent_floor`, `test_f1_cumulative_funded_follows_deposits_and_is_never_rebased` |
| F2 floor baseline tied to a date | Agreed. | `risk.equity_floor_reset_for` must equal the trigger date (HKT). It is used once, and the database records it. The baseline must be ≤ the equity at the time. The same baseline with a new version number is refused on the next trigger. | tests in test_review_v130.py and the updated v1.2.0 item-7 tests |
| L1 late decision | Agreed (the logic was right). | Comparison test added. | `test_l1_late_decision_matches_on_time_even_with_a_later_funding_spike` |
| V8 | Replaced by U2. | - | - |
| V13 report times | Accepted as is. | - | - |
| V16 wrong folder | Agreed. | The refusal shows the full path of the right `Kill_Close_Position.bat`. | `test_v16_wrong_folder_message_shows_the_right_kill_path` |
| V17 incomplete months | Agreed. | The monthly report shows two sets of numbers: all months, and complete months only. | `test_v17_monthly_report_all_vs_complete_months_and_r3_basis` |
| R3 basis | Agreed. | Every manage run (7 times a day, including during the smoketest period) logs the Polymarket mark vs Binance spot basis in bps. Reports show the sample count and the p50 and p99 of the absolute basis. The smoketest records a basis step and the real taker fee. | `test_v17_monthly_report_all_vs_complete_months_and_r3_basis`, `test_smoketest_records_real_fee_and_basis` |

## Owner decisions (committee part 4), still open
1. Proxy key: option N (only with a hardware wallet) or option O (another computer). The committee recommends N with a hardware wallet if you have one, otherwise O with P5 done in full.
2. F1 permanent floor: set to 50% of all capital ever funded as the committee recommends. Confirm or change it before going live.
3. Confirm E (30% notional cap, 8% losing-streak kill, 75% equity floor) and D11 (±0.1% tie) in writing.

## Go/no-go at v1.4.0
A. Before CONFIRM:
1. Calendar verified: **done**.
2. BT1, BT2, BT3, S5, S6, R1, R2, S9 with tests: **done**.
3. Criteria file C0–C7, I2, I4–I7: **done (draft-2)**.
4. The committee reviews draft-2 and this record (including the C0 and BT3 details) before the owner types CONFIRM: **open**.

B. Before going live:
1. Backtest run #1: A_live and A_live_stress pass C0–C7; the committee has seen the report and I5. **Open.**
2. V4 built; phone alerts tested for PC off, a stuck run and a critical alert: **built; the owner's test is open**.
3. U1 and U2 built; one failed upgrade tested with automatic restore: **built and tested in code**. For the real test on the PC, Upgrade.bat has a new `TEST-RESTORE` choice. It installs the zip (the same version is allowed), acts as if it failed, and must end with `RESTORE TEST PASSED`. **Open.**
4. Proxy key per P1, P2, P5, P6, and the withdrawal probe rejected: **built; the owner's run is open**.
5. F1 and F2: **done**.
6. Full minimum-size smoketest: **open**. It covers long, short, flip, partial bracket rejection and two stop-losses at once, and it records the real taker fee and the basis. A missed decide cannot be produced on purpose with real money: unit tests cover the late close (item 2 of the v1.2.0 review, L1).
7. Owner decisions in part 4 in writing: **open**.
8. PC settings: sleep never, `+08:00` tasks, Windows Update active hours, sign-in after updates, one restart test. **Open.**

Only the owner can approve real money.
