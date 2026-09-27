# Backtest (review B3): pre-registered design, v1.4.0

中文摘要：
- 回測喺你部電腦跑。佢會下載 Binance BTCUSDT 由 2017 年開始嘅公開數據，用同實盤一模一樣嘅決策代碼逐日重播。
- 合格準則喺 `config/backtest_criteria.yaml`（draft-2，C0 至 C7）。**委員會睇過 draft-2 之後**，你先喺 `Backtest.bat` 打 `CONFIRM`。
- 確認會鎖死準則、config、日曆、程式、數據截止日同費率。之後改任何一樣，`run` 都會拒絕。每次運行都有編號，委員會以第 1 次為準。
- 實盤用 A_live。其他變體要符合 S5 四個條件先可以取代佢，而且要委員會覆核。
- 回測唔會落單，亦唔會接觸你個戶口。

This file and `config/backtest_criteria.yaml` were written **before any backtest run on real data**. The design
follows the committee's v1.2.0 and v1.3.0 reviews (REVIEW_v1.2.0.md, REVIEW_v1.3.0.md).

## Data
Everything is public market data, downloaded on the owner's PC by `backtest download` into `data/backtest/*.csv`.

| Series | Source | From |
|---|---|---|
| BTCUSDT spot 1d, 4h and 1h klines | `api.binance.com` (fallback `data-api.binance.vision`), same endpoints as live | 2017-08-17 |
| USD-M funding | `fapi.binance.com` | 2019-09-10 (earliest available) |
| Polymarket BTC-PERP 1h candles (I7 only) | the exchange's public candles | `backtest.polymarket_start` (2025-01-01) or as far back as served |

- The first 6-month window starts on 2020-10-01. That gives the funding percentile a full 365-day history and every decision 1,000 prior daily candles, exactly like live (review C7).
- The economic calendar is `config/calendar_history.yaml` (2020-09 to 2026-09, **verified by the committee** against federalreserve.gov and bls.gov) merged with `config/calendar.yaml`.
- **Data quality (R2, C0).** The report lists:
  - a SHA-256 for each series (the exact slice used);
  - the missing candles per series;
  - every funding gap longer than 8 h;
  - every gap in the 1h data while a position is open (time and length).

  C0 fails the run when too much is missing (see Pass / fail).

## Decisions: identical to live
For UTC day D, the backtest gives `strategy.day_features` + `strategy.plan_for` exactly what the live `decide` sees at 00:30 UTC (08:30 HKT):
- the last `binance.daily_candles_to_load` (1000) closed daily candles;
- the last `binance.h4_candles_to_load` (300) closed 4h candles;
- funding from `binance.funding_days_to_load` (400) days before D;
- the event windows active at 00:30 UTC.

The same functions are used by the live engine, so the rules exist once. The test `test_backtest_decisions_equal_live_decide_on_20_days` checks score, gates, caps and plan field by field on 20+ days.

The following are live-only and not simulated: the live operational blocks (clock skew, calendar-expiry fail-safe, region) and the live review line (S9, which is set from this backtest).

## Execution and costs (conservative)
- **Fills:** decisions fill at the close of the 00:00–01:00 UTC 1h candle, 30 minutes after the decision. Entries pay `exits.entry_slippage_bps` (10 bps). Every exit (SL, TP, rule close, kill) pays `backtest.exit_slippage_bps` (10 bps).
- **Stops:** bracket SL/TP at ±1.5 / 3 ATR from the fill, checked on 1h candles from the fill on.
  - A candle that touches both counts as the SL.
  - **Gap (BT1):** when a candle opens beyond the stop, the fill is that open, which is worse than the SL, plus exit slippage.
  - **Margin cap (BT6):** the loss of an isolated 3x position never exceeds its margin.
  - The breakeven variant moves the SL once, after a 1h candle's favourable excursion reaches +1 ATR.
  - **Missing 1h candles while holding:** the next available candle is used. Its open is checked against the stop first (BT1), so a stop passed during the gap fills at that open. Every such gap is listed and limited by C0b/C0c.
- **Costs:** as `shadow._r`:
  - fees on both sides at the backtest fee rate, which is the larger of `shadow.fee_rate_estimate` (0.05%) and the real taker fee from the latest smoketest (BT3);
  - Binance funding over the holding time (a positive rate means longs pay);
  - the exit slippage above.
- **Stress twins (BT3):** every candidate variant X also runs as `X_stress`:
  - fees ×2;
  - 30 bps exit slippage (`backtest.stress_exit_slippage_bps`);
  - funding **paid** ×2 (funding received is unchanged, so a stress trade always costs more than its normal twin).
- **Sizing:** `risk.compute_size` with this config: 1.5% risk × tier fraction, 30% notional cap, 3x leverage cap.
  - The half-risk ramp for the first 10 trades applies **only to the chained full-period run** (S6).
  - Every 6-month window uses full risk from trade 1.
  - The report gives the notional-cap binding rate.
- **Equity (BT2):** while a position is open, equity is marked to market on every 1h candle at the candle's adverse extreme. All drawdowns (C3, C6, the drawdown kill switch) use this hourly equity.
- **Kill switches:** checked once a day at the fill time, and after every closed trade.
  - Drawdown 15% from peak: close, pause 14 days (`backtest.kill_pause_days`), then resume with the peak reset.
  - Losing streak 8% (D11 tie rule ±0.1%): no new entries for 14 days; the position is kept.
  - Equity floor 75% of the run's starting equity: close and stop for the rest of the run.
  - **BT5:** live checks 7 times a day, the backtest once. A kill switch can therefore fire later in the backtest than live, so its losses are, if anything, larger than live. This is the conservative direction.
  - The committee specified the automatic resume for the backtest; live, only the owner resumes.

## Variants (pre-registered, config 1.4.0 values)
| Name | Rules | Role |
|---|---|---|
| `A_live` | Live rules: hybrid score, no breakeven move (V0), crowded funding closes the position | **primary: live uses it** |
| `B_breakeven` | A plus a one-time SL move to breakeven at +1 ATR (V2) | candidate |
| `C_control` | A, but \|score\| < 30 means flat (no entry; close an open position) | candidate; I1 baseline |
| `A_live_fhold`, `B_breakeven_fhold`, `C_control_fhold` | Same as A / B / C, but crowded funding only blocks new entries and never closes | candidates |
| `<each of the above>_stress` | the variant under stress costs (BT3) | stress twin, used by C1b and S5(d) |
| `A_live_noevents` | A without the event gate | sensitivity only (I2: how much calendar errors can matter) |
| `A_live_nokill` | A without kill switches | sensitivity only (BT4 / I3b: losing streaks not cut off by the 8% kill) |

The owner decided on 2026-09-26 not to test dynamic bankroll or position-sizing variants for now.

## Choosing the live variant (S5, fixed before any result)
- **Live uses `A_live`.**
- Another candidate replaces it only if **all four** hold:
  1. it passes C0–C7 itself;
  2. its full-period total R beats A_live's at **every** start offset;
  3. it beats A_live in at least **two of the three** segments (offset median);
  4. its stress twin beats `A_live_stress` at every offset.
- If more than one qualifies, the one with the highest worst-offset total R is proposed, and the committee reviews it before any switch.
- If A_live fails and another variant qualifies, **nothing is approved automatically**. That needs a new committee meeting and a shadow forward period.
- If A_live fails and nothing qualifies: no-go.

`summary.md` prints this decision (`select_variant`), with each candidate's four checks.

## Windows and segments
- Consecutive 6-month windows from 2020-10-01. Each window starts from USD 10,000 and is run 5 times, starting 0, 7, 14, 21 and 28 days later.
- There is also one chained full-period run per start offset (kill switches resume automatically, as above).
- The full period is split into three segments for C1c: 2020-10..2022-09, 2022-10..2024-09, 2024-10..2026-09.
- Nothing is fitted: every threshold (funding percentile, EMA, ATR) comes only from data before the decision.

## Pass / fail (`config/backtest_criteria.yaml`, draft-2)
- **C0a–c** data completeness. **C1a–d** edge after costs:
  - C1a: every offset ≥ +0.05 R;
  - C1b: the stress twin > 0 R at every offset;
  - C1c: ≥ 2 of the 3 segments positive;
  - C1d: t ≥ 2.0.
- **C2a–b** windows. **C3** worst hourly window drawdown ≤ 20%. **C4** floor never hit. **C5** ≥ 60 trades. **C6** full-period drawdown ≤ 25%. **C7** ≤ 3 drawdown kills.
- Informational rules:
  - I1: the hybrid score vs the control;
  - I2: calendar sensitivity;
  - I3a/b: losing-streak p99 with and without kills;
  - I4: notional-cap share ≤ 0.5;
  - I5: long vs short per offset and segment (short negative everywhere → committee discussion; no automatic change);
  - I6: rolling 30-trade expectancy, 5th percentile. It becomes the live review line `risk.live_review_expectancy_floor_r` (S9) in a new config version after the backtest;
  - I7: Polymarket-candle replay vs Binance, with exit agreement ≥ 90% where data exists.
- The verdict is PASS only if every non-informational rule passes, for `A_live`, and `A_live_stress` where a rule says so.

## Lock and run numbering (R1)
- `backtest confirm` (Backtest.bat, after the owner types CONFIRM) records a manifest. Its SHA-256 goes into the append-only database. It covers:
  - the criteria file;
  - config: strategy, gates, exits, risk, backtest, the Binance history lengths, and the shadow fee and breakeven settings;
  - both calendars;
  - `VERSION`;
  - the code: backtest, strategy, risk, indicators, shadow and calendar;
  - a fixed `data_end_utc`;
  - the SHA-256 of the data slice;
  - the fee rate.
- `backtest run` rebuilds the manifest and **refuses any difference**, naming what changed. A new confirmation is a new pre-registration, and the committee must see why it was needed.
- Each run under one confirmation is numbered. `summary.md` starts with "Run #N under this confirmation" and the total number of confirmations and runs. **The committee uses run #1.**

## Results
`data/backtest/results_<time>/` contains:
- `summary.md` and `summary.json`: criteria, verdict, the S5 decision, data quality, I5 split, I7 replay, manifest and a result hash (identical inputs give an identical hash);
- `runs.csv`: one row per run;
- `trades_<variant>.csv`: the full period at offset 0.

Send `summary.md` and `summary.json` for review, never `.env`.

## Known limits
- Binance spot prices stand in for Polymarket perp prices, because Polymarket Perps has no multi-year history. Basis and funding differ. Three things address this:
  - the stress twins;
  - I7, where Polymarket candles exist;
  - the live basis log (R3).
- 1h candles cannot show the order of moves within an hour. The SL-first rule is the conservative choice.
- A missing 1h candle can hide a stop touched and recovered within the gap. C0b/C0c limit the gaps, and the report lists each one.
- Order-book depth is not modelled: slippage is a flat 10 bps each way (30 bps under stress), and the smoketest measures real fills.
