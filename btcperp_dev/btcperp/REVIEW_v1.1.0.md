# Response to the independent review of v1.0.0 (delivered in v1.1.0)

Status per item: DONE = changed and unit-tested (`tests/test_review_v110.py` unless noted);
NOT CHANGED = deliberately not changed, with the reason; PENDING = the owner still decides.

## A. Before the smoketest
| # | Item | Status |
|---|---|---|
| A1 | `entry_attempts` = 1 | NOT CHANGED. With B1 a retry happens only with proof the first FOK did not fill, so one retry (the spec) is safe. |
| A2 | withdrawal probe off by default, explicit switch | DONE. Off by default; `python3 run.py smoketest --probe-withdrawal` enables it (Grok Bot may not edit config). Test: `test_smoketest_new_steps_with_mock`. |

## B. HIGH
| # | Item | Status |
|---|---|---|
| B1 | double entry on retry | DONE. The SDK behaviour was confirmed in the SDK source (`_expect_ok_ack` over all acks). No retry in the same run after a rejected / unknown result; a retry needs a terminal not-filled status, no new opening fill, a flat position and no entry today. Before deferring, the position is read once more after a pause; a late-visible fill is recorded and protected at once. Tests: `test_b1_*` (entry fills but SL row rejected; stale reads; true `fok_unfilled` still retries once). |
| B2 | drawdown kill tries to close only once | DONE. While `kill_drawdown`, `manual_kill` or `equity_floor` is active and a position remains, every run retries the reduce-only close. Tests: `test_b2_*`. |
| B3 | backtest tool | NOT CHANGED. The original spec (section 12) schedules the backtest after 2 weeks of live trading; the owner decides whether to build it before going live. The development machine cannot reach Binance, so it would have to run on Grok Bot's computer or after network access is opened. |

## C. MEDIUM
| # | Item | Status |
|---|---|---|
| C4 | partial order-scoped SL | DONE (`test_c4_partial_order_sl_gets_position_sl`). |
| C5 | one read without the position | DONE. Book / cancel only with evidence: exit fills covering the trade, a fired or `position_closed` trigger, or two reads `flat_confirm_delay_seconds` apart. Also used inside `close_position`. Tests: `test_c5_*`. |
| C6 | kill check between flip close and entry | DONE (`test_c6_*`). |
| C7 | EMA200 history | DONE. `daily_candles_to_load: 1000`; test shows < 0.1% difference to a full-history EMA200. |
| C8 | equity sources | DONE with a variation: one fixed source (`total_account_value`) for peak and current; disagreement > 0.5% blocks new entries and alerts, but does not disable the kill switches (if balances exclude isolated margin they would disagree whenever a position is open, which would silently disable the kill switch); an unreadable equity skips the kill evaluation for that run and alerts. Tests: `test_c8_*`. |
| C9 | pending flows | DONE. Pending deposits/withdrawals skip drawdown and floor checks and block entries; confirmed flows adjust the peak and funded capital. Test: `test_c9_*`. |

## D. LOW
| # | Item | Status |
|---|---|---|
| D10 | missing liquidation price | DONE (isolated: missing / 0 / NaN fails; engine closes and alerts). MMR comment updated (0.5 / max leverage, cited from the review; developer could not reach the docs). |
| D11 | tie rule for the losing streak | NOT CHANGED yet: this changes the spec's kill-switch definition; waiting for the owner. |
| D12 | adopted positions | DONE (entry day from the opening fill; only active TP/SL ids; over-budget risk pauses; ATR computed from Binance when no decision exists yet). |
| D13 | unknown SL re-place | DONE (re-read open orders before closing). |
| D14 | redaction | DONE (database writes and error alerts pass through the secret redactor). |
| D15 | 30-day proxy expiry on creation | NOT CHANGED. The bot never creates proxy keys (the owner does). The 2100 date is only a placeholder the SDK object requires; the real expiry is read from `GET /v1/account/credentials` and an unreadable expiry already raises a daily alert. |
| D16 | shadow costs | DONE (funding over the holding time and FOK entry slippage). |
| D17 | smoketest short and flip | DONE (`short_and_flip`). |
| D18 | region note | DONE (API_NOTES (n)). |

## E. Parameters (PENDING USER DECISION, marked in config.yaml)
`notional_cap_pct_equity: 30`, `kill_losing_streak_pct: 8`, new `equity_floor_pct_of_net_funded: 75`
(hard stop that `resume` cannot clear; a new config version can). No deposits/withdrawals while a position
is open: alerted by the bot and written in START_HERE.md.

## F. Kept as designed
Event window handling, only exchange SL/TP during kill switches, Binance indicator data, `resume` resets
the peak (now subject to the equity floor).

## G. Smoketest records
1 `g1_bracket_partial_reject`, 2 `b_position_sl_with_bracket`, 3 `close.g3_account_read_after_close`,
4 raw geoblock in `region`, 5 `equity_formula`, 6 `open_bracket.liquidation_price_raw`,
7 `python3 run.py flowwatch` (owner makes a small deposit while flat).

## H. Delivery
Every change has unit tests; the full suite (135 tests) was run 10 times with shuffled order on a clean
install of the zip. No paper trading or live trading happens before the owner approves.
