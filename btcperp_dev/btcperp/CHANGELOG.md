# Changelog

Each version ships as `btcperp_vX.Y.Z.zip`. Code version = `VERSION`; config version = `config_version`
in `config/config.yaml`. Every log row records both, and reports never mix versions.

## 2.3.0 - 2026-10-08 (config 2.3.0) - owner: forecast engine, early reversal warnings, dynamic profit taking

The owner's v2.3.0 request ("每15分鐘重新思考：仲有幾多波幅可以食？而家走會唔會太早？", "判斷個個係高位...回落就做空，
低位...反彈就做多"), phases 1-6. Details, all numbers and the rollback: `FORECAST.md` (Chinese).

**Live trading is unchanged (the v2.2.0 rules and values).** New, analysis only:
- `perpbot/forecast.py`: every 15-minute decide run computes a forecast from closed Binance candles and stores it
  (`forecasts` table, decision record, analysis text): trend / regime, 10-90% price ranges for 15 / 30 / 60 / 120
  minutes, which way a 1 x ATR1h move goes first, typical further move up / down (targets), chances of a 500 /
  1,000 / 2,000 USD BTC move within 12 h, support / resistance / invalidation, confidence, and early reversal
  warnings (level 1 warning, 2 preparation = turned from the extreme, 3 confirmed = 1h change of character).
  Probabilities are frequencies of what followed similar situations (5 bucket features, mirror-symmetric) in the
  last 365 days of 1h candles, counting only outcomes finished at the time; nothing fitted. A failed forecast changes
  nothing else (tested: same exchange calls with and without it). 1h history is back-filled once (2 x 1,000 candles
  per run, public data, respects a Binance pause).
- Dynamic exit (`dynamic_exit`, mode "shadow"): HOLD / REDUCE / CLOSE / TIGHTEN_STOP is computed and logged for an
  open position, never executed. Live execution, the forecast entry filter and early-reversal entries are refused
  by the config check in this version (the owner's phase 6: only with supporting evidence - there is none).
- Backtest variants (intraday_bt `variant=`, `intraday_compare.py`): A live rules, B + forecast entry filter,
  C + dynamic exit (TP1 1.5 R, TP2 6 R, reduce / close / tighten on warnings, 24 h hold for a healthy runner),
  C0 dynamic exit only, E + early-reversal entries; D = the v2.0.0 / v2.1.0 values. New statistics: profit factor,
  average win / loss, trades per day, BTC move captured, trades capturing 500 / 1,000 / 2,000 USD (and how many
  reached them), give-back, long / short, market state. Sizing `risk1` (1% at the stop, <= x5) as the conservative
  plan; cost scenario `live_like` (4 + 1 bp). `--proxy-1h` runs the rules on 1h candles for long periods.
- New commands: `forecast-check` (walk-forward check of the forecast on the cached 1h candles) and
  `intraday-compare` (the variants); `windows\Forecast_Check.bat` runs both, read-only.
- Two neutral intraday knobs from the "開多啲單" tests: `min_room_r` (0.8 = TP1, as before) and `range_latest_edge`
  (false). Room 0.5 / 0.7 R and the latest-edge fade gave more trades and no better results on the owner's 21 real
  days, so they are not used.

Evidence (owner's Binance candles; all values fixed before the first run; every period shown):
- Forecast walk-forward, 21,334 hours (2024-04-30 -> 2026-10-06): 60 / 120-minute 80% bands hit 79.2% / 78.9%;
  500 / 1,000 / 2,000 USD within 12 h predicted 60 / 35 / 14%, happened 61 / 35 / 12%. Which way first: Brier skill
  -0.02% (no better than the base rate; quarters -1.7% .. +2.7%). Top warnings: down first 53.5% (level 1), 49.2%
  (2), 51.0% (3) vs 49.9% for all hours; bottom warnings 49.9 / 50.7 / 51.4%. Ranges and sizes are calibrated;
  direction and tops / bottoms are not predictable with these rules.
- Real 15m, 21 days: C beat A in all three weeks (live-like owner +15.8% vs -10.9%), ~30 trades - not evidence.
- 1h proxy, 879 days, live-like cost: A -95.4% (owner, stopped at the 5% floor) / -61.6% (risk1, PF 0.61); C
  -95.9% / -52.2% (PF 0.74); E -95.6% / -46.4% (PF 0.79); v2.1.0 / v2.0.0 values -89% / -68% (owner). All variants
  lose in 5 of 6 segments; only 2026-05 -> 10 gains. Zero cost: A PF 0.90, C 0.98. Stop-first vs target-first inside
  a candle changes these by 1-3 points. The dynamic exit captures 40-80% more 500 / 1,000 / 2,000 USD moves and
  raises the profit factor, but no variant is profitable after costs.
- Sizing check: `risk_per_trade_pct: 5` is not used while `notional_multiple_full_tier` is set; the loss at the stop
  was 4.3% of equity (median) and up to 12.1% on the 21 real days, up to 35% in the proxy (positions up to x20).
- Fix during development: the 15 / 30-minute outcomes looked up the 15m candle with minutes instead of milliseconds
  (caught by a test before release; it only affected the displayed 15 / 30-minute ranges).
- Tests: `test_forecast_v230.py` (+29): no future candle changes a forecast, live build == backtest incremental,
  only finished samples, mirror symmetry, counted frequencies and fallback, warning levels 1 / 2 (mirrored) and none
  without a move, every dynamic action both ways, no loosening, hold extension, entry filter, early reversal only
  on level 2 + 15m turn and only when asked, variant A == live rules, failed forecast = baseline, precomputed ==
  incremental runs, compare report, walk-forward report, live forecast stored with identical exchange calls, live
  failure harmless, back-fill respects the pause, honest analysis text. `test_intraday_v230.py` (+7) for the two
  knobs; older tests pin their own values. **Tests:** 485.

## 2.2.0 - 2026-10-07 (config 2.2.0) - owner: loosen further

The owner after v2.1.0 (about one trade every two days on recent real data): "再放多啲" (loosen more).

- **15m turn** (`intraday.trigger_beyond` new, "close"): the trigger closes beyond the previous 15m CLOSE (was beyond
  its high / low), in the trade direction, with its close above 40% of its range for a long (`trigger_clv_min`
  -0.2, was 0.0); mirrored for shorts.
- **TP1 0.8 R** (1.0): leg A takes profit earlier; the room check needs 0.8 R. TP2 stays 3 R.
- Pullback / retest / range-edge window 16 candles (12), leg >= 0.75 ATR1h (1.0), pullback up to 88.6% (78.6%), ranges
  >= 1.5 ATR1h (2.0) with edges 0.5 ATR1h (0.35), a leg up to 3 times (2), no cooldown after an exit (1 candle).
- Unchanged: the cost gate (cost <= 0.35 R), stop minimums, exits after TP1, time / invalidation / no-progress exits,
  position map x3-x20, leverage, kill switches, floors.
- On the owner's 21 days of real Binance 15m candles (2026-09-15 -> 10-06; coarse 15m-path approximation, 100 USD,
  owner sizing, fee 4 + slippage 1 bp per side): v2.1.0 11 trades (0.5 / day), v2.2.0 36 trades (1.7 / day); the
  result swings around zero between cost scenarios (live-like -10.9%, base +10.8%, max drawdown 24%) - noise, no
  evidence of an edge either way. More trades = more fees.
- Tests: the v2.0.0 / v2.1.0 tests pin their own values; `test_intraday_v220.py` (+8): shipped values, the weaker
  turn both ways, CLV -0.2, TP1 0.8 R on the exchange, mirror symmetry (range and reversal paths), more entries
  than v2.1.0 on random-walk markets, the cost gate still refuses. **Tests:** 449.

## 2.1.0 - 2026-10-07 (config 2.1.0) - owner: looser intraday rules, more trades

The owner after v2.0.0: no trade since the upgrade - "唔好太嚴，我要博多啲、食多啲波幅" (trade more often, catch more
of the swings). This reverses the earlier instruction not to loosen filters for more trades; the owner decides.

- **Fix (v2.0.0 bug):** the room check counted the trigger candle's OWN high (long) / low (short) as the next
  obstacle. A trigger closes near that extreme, so the "room" was a few dollars and most valid setups were refused.
  The recent-extreme obstacle now uses the candles before the trigger. On synthetic paths this alone doubled the
  entries of the v2.0.0 values; on the owner's 21 real days it changed nothing (5 trades either way).
- **Looser values** (v2.0.0 in brackets): stop floor max(0.75 (1.0) ATR1h, 0.3% (0.5%), cost / 0.35 (0.20)) - stops
  are closer and costs may take up to 0.35 R; leg >= 1.0 (1.5) ATR1h; pullback 23.6% (38.2%) - 78.6%; pullback /
  retest within 12 (8) 15m candles; reversal window 12 (6) h, retest zone and reclaim 0.5 (0.25) ATR1h; recent
  obstacle = the last 8 (16) candles; cooldown 1 (2) candle; 12 (6) entries a day; a leg may be traded twice (once).
- **New setup "range"** (`intraday.range_enabled`): in a sideways market (mixed swings) at least 2 x ATR1h wide,
  fade an edge: price came within 0.35 x ATR1h of the range low (high) in the last 12 15m candles and the last 15m
  candle turns back in, closing below (above) the middle of the range. Stop beyond that extreme (same minimums),
  same targets, room and cost gates, mirror-symmetric. Score base 20 (x3 unless the 4h background agrees).
- Unchanged: exits (TP1 1 R half, break-even + trail, TP2 3 R, 1h invalidation, 12 h time stop, no-progress), the
  owner's position map x3-x20, leverage, kill switches, floors, cost model, data checks, schedule.
- Frequency (synthetic random-walk paths, NOT evidence of profit): v2.0.0 as shipped 0.08 entries / day, v2.0.0
  values + the fix 0.18, v2.1.0 0.66. Stress through the live engine: 8 x 12 days, 84 trades (v2.0.0: 10), never
  unprotected, no duplicate orders, P&L reconciles.
- Why v2.0.0 made no trade (owner's database backup of 2026-10-07 03:00 HKT): all 21 decisions after the upgrade
  (21:46 - 02:46 HKT) were blocked only by the position opened by v1.10.0 at 10:30 HKT (LONG 0.00814 @ 85,593,
  SL 84,856, TP 86,709), as designed; the schedule, candle cache and data checks worked.
- First real-data look (the owner's cached Binance 15m candles, 2026-09-15 -> 10-06, 21 days, 15m path = coarse
  approximation, start 100 USD, owner sizing): v2.1.0 11 trades - live-like cost (fee 4 bps + 1 bp per side)
  +1.8%, max drawdown 15%; base cost (4 + 5 bps) 8 trades -6.3%; zero cost +17.8%; stress: no trade (cost gate).
  v2.0.0 values (with the fix): 5 trades, live-like +2.4%. Far too few trades to judge either version.
- Replay (`intraday-replay`) compares only decisions made under the current config version.
- Tests: the v2.0.0 scenario tests pin the v2.0.0 values; `test_intraday_v210.py` (+9): shipped values, the wick fix,
  range fades both ways and mirrored, a leg twice not three times, shallower pullbacks, more entries than v2.0.0 on
  the same market, live two-leg range entry, replay version check. **Tests:** 441.

## 2.0.0 - 2026-10-06 (config 2.0.0) - owner: intraday rules every 15 minutes, long and short

The owner asked for a complete intraday version in one release, after Codex's verification report (2026-10-06):
the hourly decisions still followed the daily score and stayed long through intraday drops; the 15m pullback and
breakout candidates Codex tested lost after costs (base cost 60 days: -75% / -90%; a 0.2% stop with 0.18% costs is
~0.9 R). New fixed rules, not copied from those candidates and not fitted to 2026-10-05. Details in Chinese:
`INTRADAY.md`.

- **Direction from closed 1h structure** (`perpbot/intraday.py`, shared by live and backtest): swing highs / lows
  (2 candles each side), up = higher highs and higher lows, down = mirrored, anything else = range (no trade). A
  break = a 1h CLOSE beyond the last higher low / lower high. The 4h EMA 20/50 background only scales the position
  (never blocks a side). Setups: continuation (38.2-78.6% pullback of a leg >= 1.5 ATR1h, recent extreme, 15m turn
  candle, not beyond the leg extreme) and reversal (structure break within 6 h, failed retest of the broken level,
  15m turn). Long and short are exact mirrors (test: every decision on a reflected price path flips). One entry per
  leg, 30-minute cooldown after an exit, at most 6 entries per UTC day, no adding.
- **Stops / exits**: stop beyond the setup's extreme + 0.25 ATR15, at least max(1 ATR1h, 0.5% of the price,
  round-trip cost / 0.20); refused above 3 ATR1h or 2%. Room to the next 1h swing level (or the last 4h extreme)
  must fit TP1 = 1 R. Two FOK legs with their own brackets: leg A TP1 1 R (half), leg B TP2 3 R, same stop (the two
  stops together cover the position). After TP1: leg A's leftover stop cancelled, a position stop at break-even +
  costs, then an ATR trail (2 ATR1h behind the best price, steps >= 0.25 ATR15); leg B's bracket stop stays as the
  backstop while stops are moved. Single leg when a leg would be under $10: break-even after a 1 R move. Before
  TP1: a 1h close beyond the setup level exits (`invalidation`); 12 h time stop; no-progress exit after 6 h with less
  than 0.5 R. Exit reasons TP1 / TP2 / SL / BE_stop / trail_stop / invalidation / time_stop / no_progress, "TP1+..."
  after the partial; MFE / MAE in R from the 15m candles.
- **Cost gate** (`perpbot/costs.py`): every fill taker; fee = the higher of the exchange's schedule and the rate
  charged on the account's recent taker fills; entry and exit slippage by walking the real order book (100 levels)
  for the order size; 5 bps stop slippage; Polymarket funding x 6 h when paid. Cost > 0.20 R: no entry, reason logged.
  Checked at the decision and again at order time on the fresh book and the final size.
- **Data** (`perpbot/candles.py`): Binance 15m / 1h / 4h candles cached in the database (`bn_klines_15m` new), one
  incremental request per interval per run (the first run back-fills 25 days) instead of ~25 pages of 1h candles
  every hour. Integrity check of the window each decision needs (count, gaps, alignment, OHLC, last close time =
  the latest boundary). Binance 429 / 418: Retry-After honoured; a short 429 is waited out once, anything else stops
  all requests until then, stored in `data_health` so the next runs send nothing; no other endpoint is tried to get
  around it. Stale or incomplete data: no new entries, no stop moves; the exchange SL / TP and the time stop work.
- **Schedule**: one Task Scheduler task `decide_15m` (HH:01 / :16 / :31 / :46 HKT, repetition PT15M, IgnoreNew, 13
  min limit, starting at the next slot). The 48 decide + 6 manage tasks are removed only after it registered. An
  entry only within 10 minutes of the candle close. The missed-run audit and the monthly report count 15-minute
  slots, only since this config version / the first 15-minute decision (no false alarms on the upgrade day).
- **Logging**: every 15-minute decision stores the structure, every setup considered with its pass / fail reason,
  score parts, costs (fee source, spread, depth, funding), room, R, stop / targets, sizing, blocks, and the inputs
  needed to recompute it; the dashboard and the daily report show it in Chinese.
- **Backtest** (`intraday-backtest download|run`, `windows\Backtest_Intraday.bat`): the same functions as live
  (signal, score, sizing with per-trade leverage and the volatility cut, quantity rounding to 0.00001 BTC, $10
  minimum, legs, exits, cost gate, event blackout, data checks), decisions 1 minute after each close, fills at the
  next 5m open, exchange stops / targets on the 5m path (stop first inside a candle), estimated liquidation; 4 cost
  scenarios (Codex's zero / optimistic / base / stress) x 2 sizings (owner x3-x20, 3% risk / 10x), full period,
  first 2/3 and last 1/3, long / short, setups, exit reasons. Every report lists its approximations.
  **Not run on real data for this release** (the development environment cannot reach Binance): only synthetic
  paths, which test the code, not the edge.
- **Live vs replay** (`intraday-replay`, `windows\Replay_Check.bat`): recomputes stored live decisions from the cached
  candles and the logged inputs; must print `DIFFERENT: 0`.
- **Upgrade**: an open position from an older version keeps its exchange SL / TP and is never touched by the new
  exits (no new entry while it is open). An entry interrupted after a fill is recovered as an intraday trade (legs by
  client order id), never entered twice. Owner settings unchanged: x3 / x6 / x10 / x20 by score (20 / 40 / 50 / 75),
  per-trade leverage up to 25x, margin 92%, liquidation 1.3 x / 1.15 x the stop, kills 95%, floors 5%.
- Event blackout for intraday entries: 30 min before to 60 min after a release (was the whole day from 08:30 HKT).
- Mock exchange: bracket triggers close their own quantity, siblings cancel (OCO). The v1.x score path stays
  (intraday.enabled false) for the old backtest, Preview of older configs and the rule tests.
- Fix found by the stress run: a second entry leg's fill (previous size non-zero) was counted as an exit fill.
- Tests: `test_intraday_v200.py` (+47): config / schedule, symmetric rules, reversal, closed-candles only, no chasing,
  cost and room gates, exits, sizing / legs, costs, integrity / cache / Retry-After / remembered ban, two-leg entry,
  idempotent re-run, TP1 -> break-even -> trail, invalidation, time stop, stale data, cost rejection logged, event
  blackout, late run, old position untouched, partial fills, interrupted entry, upgrade audit, backtest = live,
  replay, determinism, CLI. Release stress: 10 random 14-day paths through the live engine (14,137 runs, 20
  trades): never unprotected, no duplicate orders, P&L reconciles with the exchange cash; v1.x hourly path with
  crash injection: 0 problems. Release checks: clean install, upgrade from 1.10.0 with an open 1.10.0 position (kept,
  no order sent), older zip refused, restore test, 3 shuffled runs. **Tests:** 432.

## 1.10.0 - 2026-10-04 (config 1.10.0) - owner: SL / TP follow the hourly swings

The owner sent the 2026-10-02/03 trades and decisions: a x10 long from 84,574 (2026-10-03 01:30 HKT) had its TP at
88,089 (+4.2%, 1.5 x the daily ATR of ~2,340) while BTC moved between 84,122 and 84,943 (+/-0.5%) in the next 30
hours - "the target is too far, the smaller swings are missed". The owner chose brackets that follow the hourly
volatility.

- **Exits** (`exits.atr_source` "1h", new): SL = 2 x, TP = 3 x the Wilder ATR(`exits.atr_1h_period` 14) of the 1h
  candles closed at the decision period's start, at least `exits.sl_min_pct` 0.5% / `exits.tp_min_pct` 0.8% of the
  price (the round-trip fees, ~0.08% of the position = ~1.6% of equity at x20, must be covered). Quiet market (1h
  ATR ~0.3%): SL ~0.6%, TP ~0.9%; busy market: wider. Without enough 1h candles: the daily ATR (logged as such).
  "daily" keeps the v1.8.1 behaviour (multiples of the score's daily ATR).
- One function (`strategy.exit_distances`) decides the distances for live, Preview and the backtest; the decision
  stores them (`plan.exit`), and the entry, the sizing (loss at the stop), the per-trade leverage plan and the
  analysis text use them ("止損止賺跟 1 小時波幅：ATR ..."). Cadences other than rolling_1h fetch the last 1h candles.
- Trade-off: at x20 a TP of ~0.9% makes ~+16% of equity after fees and an SL of ~0.6% loses ~-14%: the strategy needs
  to be right about half the time (was ~40% with 1.5 : 1 on the daily ATR). After a TP the next hour can enter again
  if the score still allows it.
- With the closer stop the per-trade leverage rarely needs a volatility cut (x20 -> 22x).
- Shadow variants (daily cadence only) are skipped while the exits use the 1h ATR.
- An open position keeps the SL / TP it was opened with (the owner's x10 long from 84,574 keeps 82,230 / 88,089).
- Tests: the rule tests pin the daily-ATR brackets (conftest); `test_exits_v1100.py` (+11): distances, floors, only
  candles closed at the period start, daily fallback, validation, live entry brackets, backtest = live.
  **Tests:** 385.

## 1.9.0 - 2026-10-02 (config 1.9.0) - owner: decide every hour, positions up to x20

The owner asked for both in one version: a bigger map by score (below 20 no entry, 20-40 x3, 40-50 x6, 50-75 x10,
75 and above x20 equity) and a decision every hour.

- **Hourly decisions** (`strategy.cadence` rolling_1h): HH:30 HKT every hour, retry HH:50, entry window HH:30 to
  HH+1:00, one entry per hour. The daily candles ending at each UTC hour are built from Binance 1h candles (~25
  requests per run, stored once in the new `bn_klines_1h` table); the h4 gate uses the last 4h candle closed then;
  the 3-day rule counts 72 hours. 48 decide tasks; the 6 manage runs move to HH:10 (02:10, 06:10, ...) and only keep
  the manage heartbeat and the SL check. Config refuses an entry window longer than the period or an hour without
  a decide time. Preview, the analysis text ("每 1 小時"), the missed-period count in reports and the next decision
  time follow the cadence. A full hourly decision computes in ~0.7 s on 1,000 days of 1h candles.
- **Analysis popups** only when a decision opens, closes or flips (`notifications.analysis_toast_only_actions`);
  every hour would be 24 popups a day. Trade alerts are unchanged.
- **Position map** (`strategy.size_tiers`, fractions of `risk.notional_multiple_full_tier` 20): [[0, 0.15],
  [40, 0.30], [50, 0.50], [75, 1.00]] = x3 / x6 / x10 / x20; a gate against the trade caps at x10; below 20 no
  entry as before. Validated: ascending scores from 0, non-decreasing fractions in (0, 1].
- **Leverage per trade** (`risk.trade_leverage`): x20 does not fit at a fixed leverage - at 25x the liquidation
  (~3%) would sit next to the ~2.6% stop. Each trade uses the LOWEST isolated leverage whose margin fits in
  `risk.max_margin_use_pct` 92% of equity: x3 -> 4x, x6 -> 7x, x10 -> 11x, x20 -> 22x (`risk.leverage` 25 is now the
  maximum). If at that leverage the liquidation estimate is not `liq_min_sl_multiple` 1.3 x the stop away, the
  position is cut until it is (volatility cap: x20 at ATR 2.6%; ~x18 at 3%, ~x15 at 4%), with an alert and a line
  in the analysis. After the fill the exchange's liquidation price must be `liq_after_fill_sl_multiple` 1.15 x the
  stop away, or the position is closed. P&L does not depend on the leverage; only the margin and the liquidation
  distance do.
- On 100 USDC with ATR 2.6%: x20 = ~$2,000, ~-53% at the 1.0 ATR stop, ~+79% at the 1.5 ATR target; x10 ~-26% /
  +39%; x6 ~-16% / +24%; x3 ~-8% / +12%. Round-trip fees ~0.08% of the position (x20: ~1.6% of equity).
- **Backtest:** variant `R1h_live` (+ stress twin) decides every hour; the primary stays `R4h_live`. A synthetic
  full run takes longer (~+40%). Backtest.bat asks for CONFIRM again (code and config changed).
- Open alert names the leverage ("position x20.00 equity at 22x"); the instrument check no longer requires the
  exchange maximum to reach `risk.leverage` when the leverage is chosen per trade (it only caps the trade).
- Tests: rule tests pin the 4h / daily schedules, 3 tiers, risk sizing and 1.5 / 3 ATR (conftest); the fake
  Binance serves 1h candles. `test_sizing_v190.py` (replaces test_sizing_v180.py: the map, per-trade leverage, the
  volatility cut, the engine entry) and `test_hourly_v190.py` (schedule, validation, 1h = 4h at shared
  boundaries, 72-period rule, R1h stepping, engine hourly entry / hold / flip / missed hour, quiet popups).
  **Tests:** 374.

## 1.8.1 - 2026-10-02 (config 1.8.1) - owner: take profit halved, stop loss a third closer

- **Brackets:** `exits.tp_atr_multiple` 1.5 (was 3.0), `exits.sl_atr_multiple` 1.0 (was 1.5). Reward : risk 1.5 : 1
  (was 2 : 1), so the break-even win rate rises from ~33% to ~40% before fees. On 100 USDC with ATR 2,281 at 86,841
  (2.6%): x10 loses ~26% at the stop and makes ~39% at the target (were ~39% / ~79%); x5 ~13% / ~20%; x1.5 ~4% / ~6%.
  Round-trip fees (~0.08% of the position) are now ~3% of the loss at the stop. The backtest and shadow use the same.
- With the closer stop the 12x liquidation guard (liquidation >= 1.5 x the stop away) refuses entries only when ATR
  is above ~4.9% of the price (was ~3.3%).
- `risk.permanent_floor_lowered_in` "1.8.1": an install that skips 1.8.0 can still lower the 50% floor once; one that
  ran 1.7.x or 1.8.0 already holds 5% and nothing changes.
- The owner's live install was 1.7.1 (bold mode) with the old long (0.0002 BTC from 83,954) open: bold mode holds it
  to its own TP / SL; 1.8.1 keeps it as well (no top-up), and new entries follow the score map.
- Tests: the rule tests pin 1.5 / 3 ATR (conftest); `test_sizing_v180.py` checks 1.0 / 1.5 and the new guard limit.
  **Tests:** 354.

## 1.8.0 - 2026-10-02 (config 1.8.0) - owner: the analysis rules again, positions by score

The owner went back to the analysis rules (bold mode off) and asked for bigger positions when the score is high:
the live long at score +73.8 (entered under the v1.5.7 sizing, ~0.0003 BTC) made under $1 on +2,887 points.
The owner's map: below 20 no entry, 20-40 x1.5, 40-70 x5, above 70 x10 equity. Positions are not added to while
held (owner's choice).

- **Position sizing by score:** new `risk.notional_multiple_full_tier` (10): position = equity x 10 x the tier
  fraction. Tiers: `strategy.tier_low_max` 40 / `tier_mid_max` 70 (were 30 / 50), fractions 0.15 / 0.50 / 1.00
  (were 0.25 / 0.50 / 1.00). The gate caps (200-day line, 4h trend against the trade) still cap at 0.50 = x5.
  null = the v1.5.x sizing by risk %. While set, `risk_per_trade_pct` and `notional_cap_pct_equity` are not used.
  On 100 USDC at 86,841 with ATR 2,281: x10 = 0.01151 BTC, ~$39 at the 1.5 ATR stop, ~$79 at the 3 ATR target.
- **Entry threshold:** new `strategy.min_entry_abs_score` 20: no new position below it (closes, flips and the
  3-day rule are unchanged; a flip's re-entry needs it too). The backtest and shadow use the same rule.
- **12x isolated** (was 20x in bold mode, 10x before): the margin for x10 (~83% of equity) plus fees must fit, and
  the liquidation (~7.3% from entry at 12x on BTC-USD's max 50x) must lie beyond the stop; at 20x it (~4%) would sit
  inside the ~4% stop. `risk.liq_min_sl_multiple` 1.5 (was 2.0): entries stop when ATR is above ~3.3% of the price.
  Config refuses a multiple above the leverage.
- **Bold mode off** (`bold.enabled` false; its code and values stay). Kill switches 95%, floors 5%, live review
  line off as in 1.7.0 (owner: no automatic stop). `risk.permanent_floor_lowered_in` "1.8.0": an install that never
  ran 1.7.0 still holds the 50% floor and lowers it once with this config.
- Open alert: risk at the stop in $ and % of equity, and the position as a multiple of equity. The analysis text
  shows "倉位 = 本金 ×N（12 倍逐倉）" with the loss / gain at the stop / target, and "分數未到入場門檻" below 20.
  An entry recovered after a crash is checked against the full-tier position's loss at its stop.
- The backtest still sizes by risk % (its R results do not depend on the size); it uses the new tiers and threshold.
- Tests: the rule tests pin the v1.4 tiers and v1.5.6 risk lines (conftest); `test_sizing_v180.py` (+20) covers the
  map, the 100 USDC sizes, the leverage cap, the 12x liquidation guard, the engine entry, no entry below 20, no
  top-up and crash recovery. **Tests:** 354.

## 1.7.1 - 2026-10-02 (config 1.7.0) - docs: how to stop bold mode without Telegram

Docs only; the code and config are the same as 1.7.0 (config stays 1.7.0, the version that may lower the permanent
floor). The owner does not use Telegram (it is off by default). START_HERE and this changelog named a `Pause.bat`
and Telegram `/pause`, neither of which the owner has: the stop buttons are `Pause_New_Entries.bat` (no new bets;
an open bet keeps its TP/SL) and `Kill_Close_Position.bat` (close now and pause). Alerts arrive as Windows
notifications, on the dashboard and in `Alerts.bat`.

## 1.7.0 - 2026-10-02 (config 1.7.0) - owner: bold mode (all-in bets at 20x)

The owner: "I want to gamble", high return, small capital, "keep going until I say stop". After the numbers were
laid out (bold play maximises the chance of doubling; the expected value per bet is still negative after fees and
the strategy's edge is unproven, backtest run #1 FAIL), the owner chose 20x all-in bets. Live decides every 4 hours
as before; the strategy picks the direction.

- **New `bold` section:** each entry is one bet: position = equity x 19 (`notional_multiple`), isolated at 20x.
  TP where equity reaches x2 after both taker fees (~+5.36% at the 0.05% fee estimate), SL where the loss incl. fees
  is 70% of equity (~-3.58%). `risk.bold_plan` sizes it; the entry is refused if a rule of the instrument breaks, if
  the notional needs more than the leverage, or if the liquidation estimate (1/20 - 0.5/50 = 4% on BTC-USD) is not
  beyond the SL by `liq_buffer_pct` (0.3% of the price). After the fill the same buffer is checked against the
  exchange's liquidation price; if it fails the bet is closed (as before for the 2 x SL rule).
- **Held until TP or SL** (`hold_until_tp_sl`): while a bet is open, flips, the 3-day rule and the funding / flat
  rules are ignored (`strategy.hold_for_bold`, logged as a note). Pause / kill still work.
- **No automatic stop (owner):** drawdown and losing-streak kills 95% (were 25 / 20), equity floor 5% of net funded
  (was 75), permanent floor 5% (was 50), live review line off (was -0.196R). 100 USDC -> ~30 after one loss -> ~9
  after two: the bot still bets. `Pause_New_Entries.bat` stops new bets (an open bet keeps its TP/SL);
  `Kill_Close_Position.bat` closes it and stops.
- **Permanent floor lowered once:** the bot refuses any config that lowers the permanent floor (review v1.3.0 F1).
  New `risk.permanent_floor_lowered_in`: the ONE config version allowed to lower it (here "1.7.0"); the lower value
  becomes the new maximum and a later config cannot reuse the name. An alert records it.
- Not used while bold mode is on: `risk_per_trade_pct`, tiers, ramp, notional cap, raise to minimum,
  `liq_min_sl_multiple`. They still apply if `bold.enabled` is set to false.
- An entry recovered after a crash is checked against 70% of the equity before the bet (was 5%), so it does not
  pause the bot. Preview / dashboard analysis text shows the bet (position, SL, TP); the dashboard shows "孤注模式 20x".
- Tests run with bold mode off and the v1.5.6 risk lines (conftest); `test_bold_v170.py` covers the shipped values,
  the bet's numbers on 100 USDC (TP ~ +$100, SL ~ -$70), refusals, holding through a flip and the funding rule, TP
  and re-bet, two losses and a third bet, the liquidation buffer, crash recovery and the floor override.
  **Tests:** 334 (+18).

## 1.6.0 - 2026-10-02 (config 1.5.9) - backtest: deciding every 2 hours

The owner asked whether deciding more often would be better and chose to backtest a 2-hour cadence first. Live is
unchanged: it still decides every 4 hours (config 1.5.9).

- **Backtest variants `R2h_live` and `R2h_live_stress`:** the same score and rules at every 2-hour boundary (UTC 00,
  02, 04, ...). The daily candles ending at a 2-hour boundary are built from 1h candles (12 phase series); at
  4-hour boundaries they equal the 4h-built ones. The 3-day rule is 36 periods. The h4 gate uses the last 4h candle
  closed at the boundary (at 02:00 the one that closed at 00:00) - a test caught that it had demanded a 4h candle
  ending exactly at 02:00, which would have silently made R2h decide only every 4 hours.
- **Criteria draft-4:** new informational I8 = R2h_live minus R4h_live, full-period total R (median). Nothing else
  changes; the committee wording in the file header now says the owner confirms.
- `strategy.shifted_daily` takes the input candle length (`bar_ms`, default 4h); `period_features` takes the
  cadence. The live 4-hour path is unchanged (its gate cutoff is still the decision time).
- The code, criteria and config changed since the 2026-09-30 confirmation: `Backtest.bat` asks for CONFIRM again.
  A full synthetic run takes about 15% longer.
- Live config still accepts only `daily` and `rolling_4h`. **Tests:** 316 (+5).

## 1.5.9 - 2026-10-02 (config 1.5.9) - owner: 10x isolated

The owner chose 10x after the trade-offs were laid out. Sizing is by risk (5% at the 100% tier), so the size and
the loss at the stop do not change: a full trade on 100 USDC is still ~0.0015 BTC (~$125) and ~$5 at the stop.

- **Config 1.5.9:** `risk.leverage` 10 (was 3), still isolated. Margin per full trade ~$12.5 (was ~$42).
- **Liquidation:** the estimate sits ~9% from entry (1/10 - 0.5/50 at BTC-USD's 50x maximum), the stop ~4% (1.5 ATR).
  `liq_min_sl_multiple` 2 is unchanged: an entry whose liquidation would be nearer than 2 x the stop is refused, and
  a fill whose exchange liquidation price is nearer is closed. At 83,066 that happens when ATR is above ~3% of the
  price (~2,490), i.e. in volatile periods there are no entries at 10x. A gap through the stop beyond ~9% liquidates
  the position: the loss is the isolated margin plus the liquidation fee, more than the planned 5%.
- Tests keep 3x pinned for the rule tests; `test_sizing_v157.py` checks 10x and the liquidation guard at ATR 2,198 /
  2,400 (entry allowed) and 2,600 (refused). **Tests:** 311 (+3).

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
