# Backtest (review B3): pre-registered design

中文摘要：
- 回測喺你部電腦跑。佢會下載 Binance BTCUSDT 由 2017 年開始嘅公開數據，用同實盤一模一樣嘅決策代碼逐日重播。
- 合格準則喺 `config/backtest_criteria.yaml`。委員會審閱之後，由你喺 `Backtest.bat` 打 `CONFIRM` 確認，確認咗先可以跑。
- 睇到結果之後，準則唔可以再改。
- 回測唔會落單，亦唔會接觸你個戶口。

This file and `config/backtest_criteria.yaml` were written **before any backtest run on real data**.
Changing either one creates a new file hash, which needs a new confirmation.

## Data
Everything is public Binance data, downloaded on the owner's PC by `backtest download` into `data/backtest/*.csv`.

| Series | Source | From |
|---|---|---|
| BTCUSDT spot 1d, 4h and 1h klines | `api.binance.com` (fallback `data-api.binance.vision`), same endpoints as live | 2017-08-17 |
| USD-M funding | `fapi.binance.com` | 2019-09-10 (earliest available) |

- The first 6-month window starts on 2020-10-01. That gives the funding percentile a full 365-day history and every decision 1,000 prior daily candles, exactly like live (review C7).
- The economic calendar is `config/calendar_history.yaml` (2020-09 to 2026-09) merged with `config/calendar.yaml`.
- **The historical calendar was compiled by the developer without internet access and must be verified by the committee** against federalreserve.gov and bls.gov. Entries marked `verify: true` are the least certain.

## Decisions: identical to live
For UTC day D, the backtest gives `strategy.day_features` + `strategy.plan_for` exactly what the live `decide` sees at 00:30 UTC (08:30 HKT):
- the last `binance.daily_candles_to_load` (1000) closed daily candles;
- the last `binance.h4_candles_to_load` (300) closed 4h candles;
- funding from `binance.funding_days_to_load` (400) days before D;
- the event windows active at 00:30 UTC.

The same functions are used by the live engine, so the rules exist once. The test `test_backtest_decisions_equal_live_decide_on_20_days` checks score, gates, caps and plan field by field on 20+ days (committee check).

The following are live-only and not simulated: the live operational blocks (clock skew, calendar-expiry fail-safe, region). Liquidation (3x isolated, about 33% away, versus a stop about 1.5 ATR away) never binds.

## Execution and costs (conservative)
- **Fills:** decisions fill at the close of the 00:00–01:00 UTC 1h candle, 30 minutes after the decision. Entries pay `exits.entry_slippage_bps` (10 bps). Every exit (SL, TP, rule close, kill) pays `backtest.exit_slippage_bps` (10 bps).
- **Stops:** bracket SL/TP at ±1.5 / 3 ATR from the fill are checked on 1h candles from the fill on. A candle that touches both counts as the SL. The breakeven variant moves the SL once, after a 1h candle's favourable excursion reaches +1 ATR.
- **Costs:** exactly `shadow._r`, i.e. fees on both sides at `shadow.fee_rate_estimate` (0.05%) and Binance funding over the holding time (positive rate = longs pay), plus the exit slippage above.
- **Sizing:** `risk.compute_size` with this config: 1.5% risk × tier fraction, half risk for the first 10 trades of each run, 30% notional cap, 3x leverage cap. The notional-cap binding rate is reported.
- **Kill switches:** checked once a day at the fill time on mark-to-market equity, and after every closed trade.
  - Drawdown 15% from peak: close, pause 14 days (`backtest.kill_pause_days`), then resume with the peak reset.
  - Losing streak 8% (D11 tie rule ±0.1%): no new entries for 14 days; the position is kept.
  - Equity floor 75% of the run's starting equity: close and stop for the rest of the run.
  - Live checks 7 times a day, so the daily check here is slightly slower. The committee specified the automatic resume for the backtest; live, only the owner resumes.

## Variants (pre-registered, all with config 1.3.0 values)
| Name | Rules |
|---|---|
| `A_live` (**primary**) | Live rules: hybrid score, no breakeven move (V0), crowded funding closes the position |
| `B_breakeven` | A plus a one-time SL move to breakeven at +1 ATR (V2) |
| `C_control` | A, but \|score\| < 30 means flat (no entry; close an open position) |
| `A_live_fhold`, `B_breakeven_fhold`, `C_control_fhold` | Same as A / B / C, but crowded funding only blocks new entries and never closes |
| `A_live_noevents` | A without the event gate: a **sensitivity check only**, showing how much calendar errors can matter |

The owner decided on 2026-09-26 not to test dynamic bankroll or position-sizing variants for now.

## Windows
- Consecutive 6-month windows from 2020-10-01. Each window starts from USD 10,000 and is run 5 times, starting 0, 7, 14, 21 and 28 days later.
- There is also one chained full-period run per start offset.
- Nothing is fitted: every threshold (funding percentile, EMA, ATR) comes only from data before the decision.

## Pass / fail
- The verdict comes from `config/backtest_criteria.yaml` (draft-1). Rules C1–C5 apply to `A_live`; I1–I4 are informational and feed the reviews of the 30% cap, the 8% streak kill and the value of the hybrid score.
- `backtest run` refuses to start until the owner has confirmed the current file (`backtest confirm`, done by `Backtest.bat` after the owner types CONFIRM). The SHA-256 is stored in the append-only database.
- Results go to `data/backtest/results_<time>/`:
  - `summary.md` and `summary.json`, with the criteria, a verdict and a result hash (identical data gives an identical hash);
  - `runs.csv`, one row per run;
  - `trades_<variant>.csv`, the full period at offset 0.

## Known limits
- Binance spot prices stand in for Polymarket perp mark prices, because Polymarket Perps has no multi-year history. Basis and funding differ. The bot also keeps its own Polymarket dataset from day one, for a later comparison.
- 1h candles cannot show the order of moves within an hour. The SL-first rule is the conservative choice.
- Order-book depth is not modelled: slippage is a flat 10 bps each way, and the smoketest measures real fills.
