"""Configuration loading and validation.

All strategy / risk numbers live in config/config.yaml. Strategy code reads
them through this module and never hardcodes them.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    pass


class Section:
    """Read-only attribute access over a nested dict with clear errors."""

    def __init__(self, data: dict[str, Any], path: str = "") -> None:
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str) -> Any:
        data = object.__getattribute__(self, "_data")
        path = object.__getattribute__(self, "_path")
        if name.startswith("__"):
            raise AttributeError(name)
        if name not in data:
            raise ConfigError(f"missing config key: {path + name}")
        value = data[name]
        if isinstance(value, dict):
            return Section(value, f"{path}{name}.")
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        raise ConfigError("config is read-only")

    def get(self, name: str, default: Any = None) -> Any:
        data = object.__getattribute__(self, "_data")
        value = data.get(name, default)
        if isinstance(value, dict):
            return Section(value, f"{object.__getattribute__(self, '_path')}{name}.")
        return value

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(object.__getattribute__(self, "_data"))


# (dotted key, type or tuple of types, validator or None)
_NUM = (int, float)
_REQUIRED: list[tuple[str, Any, Any]] = [
    ("config_version", str, lambda v: len(v) > 0),
    ("market.symbol_candidates", list, lambda v: len(v) > 0),
    ("market.base_asset", str, None),
    ("market.quote_asset", str, None),
    ("market.category", str, None),
    ("polymarket.rest_url", str, lambda v: v.startswith("https://")),
    ("polymarket.ws_url", str, lambda v: v.startswith("wss://")),
    ("polymarket.chain_id", int, None),
    ("polymarket.geoblock_url", str, None),
    ("polymarket.http_timeout_seconds", _NUM, lambda v: v > 0),
    ("polymarket.command_timeout_seconds", _NUM, lambda v: v > 0),
    ("polymarket.book_depth", int, lambda v: v in (10, 100, 500, 1000)),
    ("polymarket.order_status_poll_attempts", int, lambda v: v >= 1),
    ("polymarket.order_status_poll_interval_seconds", _NUM, lambda v: v >= 0),
    ("polymarket.fills_lookback_hours", _NUM, lambda v: v > 0),
    ("polymarket.kline_backfill_max_days", _NUM, lambda v: v > 0),
    ("binance.spot_base_urls", list, lambda v: len(v) > 0),
    ("binance.futures_base_urls", list, lambda v: len(v) > 0),
    ("binance.symbol", str, None),
    ("binance.daily_candles_to_load", int, lambda v: 200 <= v <= 1000),
    ("binance.h4_candles_to_load", int, lambda v: 60 <= v <= 1000),
    ("binance.funding_days_to_load", int, lambda v: v >= 365),
    ("telegram.enabled", bool, None),
    ("notifications.windows_toast", bool, None),
    ("dashboard.port", int, lambda v: 1024 <= v <= 65535),
    ("dashboard.refresh_min_seconds", _NUM, lambda v: v >= 10),
    ("dashboard.auto_refresh_seconds", _NUM, lambda v: v >= 60),
    ("telegram.api_base", str, None),
    ("schedule.decide_times_hkt", list, None),
    ("schedule.manage_times_hkt", list, None),
    ("schedule.entry_window_start_hkt", str, None),
    ("schedule.entry_window_end_hkt", str, None),
    ("schedule.late_tolerance_minutes", _NUM, lambda v: v >= 0),
    ("schedule.missed_tolerance_minutes", _NUM, lambda v: v > 0),
    ("strategy.ema_trend_period", int, lambda v: v > 1),
    ("strategy.ema_regime_period", int, lambda v: v > 1),
    ("strategy.atr_period", int, lambda v: v > 1),
    ("strategy.min_daily_candles", int, lambda v: v >= 200),
    ("strategy.trend_weight", _NUM, None),
    ("strategy.trend_atr_multiple", _NUM, lambda v: v > 0),
    ("strategy.breakout_points", _NUM, None),
    ("strategy.clv_weight", _NUM, None),
    ("strategy.score_round_decimals", int, lambda v: 0 <= v <= 10),
    ("strategy.tier_low_max", _NUM, None),
    ("strategy.tier_mid_max", _NUM, None),
    ("strategy.tier_low_fraction", _NUM, lambda v: 0 < v <= 1),
    ("strategy.tier_mid_fraction", _NUM, lambda v: 0 < v <= 1),
    ("strategy.tier_high_fraction", _NUM, lambda v: 0 < v <= 1),
    ("strategy.min_entry_abs_score", _NUM, lambda v: 0 <= v < 100),
    ("strategy.flip_min_abs_score", _NUM, None),
    ("strategy.opposite_days_rule", int, lambda v: v >= 1),
    ("strategy.tighten_sl_on_same_direction", bool, None),
    ("strategy.cadence", str, lambda v: v in ("daily", "rolling_4h", "rolling_1h")),
    ("notifications.analysis_toast", bool, None),
    ("notifications.analysis_toast_only_actions", bool, None),
    ("strategy.flip_confirm_periods", int, lambda v: 1 <= v <= 6),
    ("schedule.period_entry_start_minutes", int, lambda v: 0 <= v < 240),
    ("schedule.period_entry_end_minutes", int, lambda v: 0 < v <= 240),
    ("gates.regime_cap", _NUM, lambda v: 0 < v <= 1),
    ("gates.h4_cap", _NUM, lambda v: 0 < v <= 1),
    ("gates.h4_ema_fast", int, lambda v: v > 1),
    ("gates.h4_ema_slow", int, lambda v: v > 1),
    ("gates.funding_lookback_days", int, lambda v: v >= 30),
    ("gates.funding_high_percentile", _NUM, lambda v: 50 < v <= 100),
    ("gates.funding_low_percentile", _NUM, lambda v: 0 <= v < 50),
    ("gates.funding_cutoff_tolerance_minutes", _NUM, lambda v: v >= 0),
    ("gates.event_anchor_hkt", str, None),
    ("gates.event_post_release_hours", _NUM, lambda v: v >= 0),
    ("gates.calendar_file", str, None),
    ("gates.calendar_coverage_warn_days", int, lambda v: v >= 0),
    ("gates.event_allows_rule_closes", bool, None),
    ("exits.sl_atr_multiple", _NUM, lambda v: v > 0),
    ("exits.tp_atr_multiple", _NUM, lambda v: v > 0),
    ("exits.atr_source", str, lambda v: v in ("daily", "1h")),     # v1.10.0
    ("exits.atr_1h_period", int, lambda v: 2 <= v <= 200),
    ("exits.sl_min_pct", _NUM, lambda v: 0 <= v < 50),
    ("exits.tp_min_pct", _NUM, lambda v: 0 <= v < 50),
    ("exits.entry_slippage_bps", _NUM, lambda v: 0 <= v <= 500),
    ("exits.entry_attempts", int, lambda v: 1 <= v <= 5),
    ("exits.close_slippage_bps", _NUM, lambda v: 0 <= v <= 2000),
    ("exits.close_attempts", int, lambda v: 1 <= v <= 10),
    ("risk.risk_per_trade_pct", _NUM, lambda v: 0 < v <= 5),
    ("risk.ramp_trades", int, lambda v: v >= 0),
    ("risk.ramp_factor", _NUM, lambda v: 0 < v <= 1),
    ("risk.leverage", int, lambda v: 1 <= v <= 50),        # v1.9.0: the per-trade maximum when sizing by position
    ("risk.cross_margin", bool, None),
    ("risk.notional_cap_pct_equity", _NUM, lambda v: 0 < v <= 300),
    ("risk.raise_to_min_notional", bool, None),
    ("risk.liq_min_sl_multiple", _NUM, lambda v: v >= 1),
    ("risk.max_margin_use_pct", _NUM, lambda v: 10 <= v <= 95),
    ("risk.liq_estimate_mmr_divisor", _NUM, lambda v: v > 0),
    ("risk.kill_drawdown_pct", _NUM, lambda v: 0 < v < 100),
    ("risk.kill_losing_streak_pct", _NUM, lambda v: 0 < v < 100),
    ("risk.expectancy_window_trades", int, lambda v: v >= 1),
    ("risk.equity_source", str, lambda v: v in ("wallet_plus_upnl", "total_account_value")),
    ("risk.equity_floor_pct_of_net_funded", _NUM, lambda v: 0 < v < 100),
    ("risk.losing_streak_tie_pct", _NUM, lambda v: 0 <= v < 5),
    ("risk.permanent_floor_pct_of_cumulative_funded", _NUM, lambda v: 0 < v < 100),
    ("risk.live_review_min_trades", int, lambda v: v >= 1),
    ("risk.live_review_window_trades", int, lambda v: v >= 5),
    ("schedule.max_clock_skew_seconds", _NUM, lambda v: 0 < v <= 300),
    ("polymarket.flat_confirm_delay_seconds", _NUM, lambda v: v >= 0),
    ("smoketest.bracket_reject_test", bool, None),
    ("risk.equity_crosscheck_tolerance_pct", _NUM, lambda v: v >= 0),
    ("risk.funding_payment_sign", int, lambda v: v in (1, -1)),
    ("key.expiry_warn_days", _NUM, lambda v: v >= 0),
    ("lock.wait_seconds", _NUM, lambda v: v >= 0),
    ("logging.level", str, None),
    ("shadow.enabled", bool, None),
    ("shadow.gate_trade_max_hold_days", _NUM, lambda v: v > 0),
    ("shadow.fee_rate_estimate", _NUM, lambda v: v >= 0),
    ("shadow.breakeven_trigger_atr", _NUM, lambda v: v > 0),
    ("reports.weekly_days", int, lambda v: v >= 1),
    ("backtest.data_start", str, None),
    ("backtest.first_window_start", str, None),
    ("backtest.window_months", int, lambda v: 1 <= v <= 24),
    ("backtest.start_offsets_days", list, lambda v: len(v) >= 1 and all(isinstance(x, int) and 0 <= x < 60 for x in v)),
    ("backtest.start_equity_usd", _NUM, lambda v: v > 0),
    ("backtest.kill_pause_days", int, lambda v: v >= 0),
    ("backtest.exit_slippage_bps", _NUM, lambda v: 0 <= v <= 500),
    ("backtest.quantity_decimals", int, lambda v: 0 <= v <= 8),
    ("backtest.calendar_history_file", str, None),
    ("backtest.criteria_file", str, None),
    ("backtest.stress_exit_slippage_bps", _NUM, lambda v: 0 <= v <= 1000),
    ("backtest.segments", list, lambda v: len(v) >= 1 and all(isinstance(x, list) and len(x) == 2 for x in v)),
    ("backtest.rolling_trades", int, lambda v: 5 <= v <= 500),
    ("backtest.polymarket_start", str, None),
    ("backtest.pm_replay_min_days", int, lambda v: v >= 1),
    ("reports.max_missed_decision_days", int, lambda v: v >= 0),
    ("smoketest.resting_order_offset_pct", _NUM, lambda v: 0 < v < 50),
    ("smoketest.probe_proxy_withdrawal", bool, None),
    ("smoketest.perps_deposit_contract", str, None),
    ("smoketest.collateral_token", str, None),
    # v1.7.0 bold mode (owner 2026-10-02)
    ("bold.enabled", bool, None),
    ("bold.notional_multiple", _NUM, lambda v: 0 < v <= 50),
    ("bold.target_multiple", _NUM, lambda v: v > 1),
    ("bold.max_loss_fraction", _NUM, lambda v: 0 < v < 1),
    ("bold.liq_buffer_pct", _NUM, lambda v: v >= 0),
    ("bold.hold_until_tp_sl", bool, None),
    # v2.0.0 intraday (owner 2026-10-06)
    ("schedule.intraday_every_minutes", int, lambda v: v == 15),
    ("schedule.intraday_offset_minutes", int, lambda v: 0 <= v < 15),
    ("intraday.enabled", bool, None),
    ("intraday.cache_backfill_days", _NUM, lambda v: 10 <= v <= 60),
    ("intraday.entry_max_delay_minutes", _NUM, lambda v: 1 <= v <= 14),
    ("intraday.swing_k", int, lambda v: 1 <= v <= 5),
    ("intraday.structure_lookback_hours", int, lambda v: 24 <= v <= 168),
    ("intraday.atr_period", int, lambda v: 2 <= v <= 100),
    ("intraday.er_hours", int, lambda v: 2 <= v <= 168),
    ("intraday.ctx_ema_fast", int, lambda v: v >= 2),
    ("intraday.ctx_ema_slow", int, lambda v: v >= 3),
    ("intraday.ctx_slope_bars", int, lambda v: 1 <= v <= 20),
    ("intraday.leg_min_atr", _NUM, lambda v: v > 0),
    ("intraday.retrace_min", _NUM, lambda v: 0 < v < 1),
    ("intraday.retrace_max", _NUM, lambda v: 0 < v < 1),
    ("intraday.pullback_max_bars", int, lambda v: 1 <= v <= 48),
    ("intraday.trigger_clv_min", _NUM, lambda v: -1 <= v < 1),
    ("intraday.reversal_window_hours", int, lambda v: 1 <= v <= 48),
    ("intraday.retest_zone_atr", _NUM, lambda v: v >= 0),
    ("intraday.reclaim_atr", _NUM, lambda v: v >= 0),
    ("intraday.stop_buffer_atr15", _NUM, lambda v: v >= 0),
    ("intraday.sl_min_atr1h", _NUM, lambda v: v >= 0),
    ("intraday.sl_min_pct", _NUM, lambda v: 0 <= v < 10),
    ("intraday.max_cost_r", _NUM, lambda v: 0 < v < 1),
    ("intraday.sl_max_atr1h", _NUM, lambda v: v > 0),
    ("intraday.sl_max_pct", _NUM, lambda v: 0 < v <= 10),
    ("intraday.tp1_r", _NUM, lambda v: v > 0),
    ("intraday.tp1_fraction", _NUM, lambda v: 0 < v < 1),
    ("intraday.tp2_r", _NUM, lambda v: v > 0),
    ("intraday.room_recent_bars", int, lambda v: 1 <= v <= 96),
    ("intraday.open_room_atr", _NUM, lambda v: v > 0),
    ("intraday.invalidation_timeframe", str, lambda v: v in ("15m", "1h")),
    ("intraday.trail_atr1h", _NUM, lambda v: v > 0),
    ("intraday.trail_min_step_atr15", _NUM, lambda v: v >= 0),
    ("intraday.be_trigger_r", _NUM, lambda v: v > 0),
    ("intraday.max_hold_hours", _NUM, lambda v: 0 < v <= 72),
    ("intraday.no_progress_hours", _NUM, lambda v: v > 0),
    ("intraday.no_progress_mfe_r", _NUM, lambda v: v >= 0),
    ("intraday.cooldown_bars", int, lambda v: 0 <= v <= 96),
    ("intraday.max_entries_per_day", int, lambda v: 1 <= v <= 96),
    ("intraday.max_entries_per_leg", int, lambda v: 1 <= v <= 10),
    ("intraday.range_enabled", bool, None),
    ("intraday.range_min_atr", _NUM, lambda v: v > 0),
    ("intraday.range_edge_atr", _NUM, lambda v: v >= 0),
    ("intraday.score_base_range", _NUM, lambda v: v >= 0),
    ("intraday.score_base_continuation", _NUM, lambda v: v >= 0),
    ("intraday.score_base_reversal", _NUM, lambda v: v >= 0),
    ("intraday.score_trend", _NUM, lambda v: v >= 0),
    ("intraday.score_ctx_aligned", _NUM, lambda v: v >= 0),
    ("intraday.score_ctx_neutral", _NUM, lambda v: v >= 0),
    ("intraday.score_room", _NUM, lambda v: v >= 0),
    ("intraday.score_room_full_r", _NUM, lambda v: v > 0),
    ("intraday.score_cost", _NUM, lambda v: v >= 0),
    ("intraday.break_full_atr", _NUM, lambda v: v > 0),
    ("intraday.stop_slippage_bps", _NUM, lambda v: 0 <= v <= 200),
    ("intraday.funding_hold_hours", _NUM, lambda v: v >= 0),
    ("intraday.fee_min_fills", int, lambda v: v >= 1),
    ("intraday.cost_book_depth", int, lambda v: v in (10, 100, 500, 1000)),
    ("intraday.event_block_before_minutes", _NUM, lambda v: v >= 0),
    ("intraday.event_block_after_minutes", _NUM, lambda v: v >= 0),
]


def _lookup(data: dict[str, Any], dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise ConfigError(f"missing config key: {dotted}")
        cur = cur[part]
    return cur


def validate(data: dict[str, Any]) -> None:
    errors: list[str] = []
    for key, typ, check in _REQUIRED:
        try:
            value = _lookup(data, key)
        except ConfigError as e:
            errors.append(str(e))
            continue
        if typ is bool:
            ok_type = isinstance(value, bool)
        elif typ is int:
            ok_type = isinstance(value, int) and not isinstance(value, bool)
        elif typ == _NUM:
            ok_type = isinstance(value, (int, float)) and not isinstance(value, bool)
        else:
            ok_type = isinstance(value, typ)
        if not ok_type:
            errors.append(f"config key {key}: wrong type {type(value).__name__}")
            continue
        if check is not None and not check(value):
            errors.append(f"config key {key}: invalid value {value!r}")
    try:
        base = data["risk"]["equity_floor_reset_baseline_usd"]
        if base is not None and (isinstance(base, bool) or not isinstance(base, (int, float)) or base <= 0):
            errors.append("config key risk.equity_floor_reset_baseline_usd: must be null or a positive number")
    except (KeyError, TypeError):
        errors.append("missing config key: risk.equity_floor_reset_baseline_usd")
    for key in ("equity_floor_reset_for", "permanent_floor_reset_for"):
        v = (data.get("risk") or {}).get(key, "missing")
        if v == "missing":
            errors.append(f"missing config key: risk.{key}")
        elif v is not None:
            try:
                from datetime import date as _d

                _d.fromisoformat(str(v))
            except ValueError:
                errors.append(f"config key risk.{key}: must be null or a date YYYY-MM-DD")
    pl = (data.get("risk") or {}).get("permanent_floor_lowered_in", "missing")
    if pl == "missing":
        errors.append("missing config key: risk.permanent_floor_lowered_in")
    elif pl is not None and not isinstance(pl, str):
        errors.append("config key risk.permanent_floor_lowered_in: must be null or a config_version string")
    la = (data.get("risk") or {}).get("liq_after_fill_sl_multiple", "missing")
    if la == "missing":
        errors.append("missing config key: risk.liq_after_fill_sl_multiple")
    elif la is not None and (isinstance(la, bool) or not isinstance(la, (int, float)) or la < 1):
        errors.append("config key risk.liq_after_fill_sl_multiple: must be null or a number >= 1")
    st = (data.get("strategy") or {}).get("size_tiers", "missing")
    if st == "missing":
        errors.append("missing config key: strategy.size_tiers")
    elif st is not None:
        ok = isinstance(st, list) and len(st) >= 1 and all(
            isinstance(r, list) and len(r) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in r)
            for r in st)
        if ok:
            ok = (st[0][0] == 0 and all(0 < r[1] <= 1 for r in st)
                  and all(a[0] < b[0] and a[1] <= b[1] for a, b in zip(st, st[1:])))
        if not ok:
            errors.append("config key strategy.size_tiers: null or [[0, f], [score, f], ...] with ascending scores "
                          "from 0 and non-decreasing fractions in (0, 1]")
    nm = (data.get("risk") or {}).get("notional_multiple_full_tier", "missing")
    if nm == "missing":
        errors.append("missing config key: risk.notional_multiple_full_tier")
    elif nm is not None:
        lev = (data.get("risk") or {}).get("leverage")
        if isinstance(nm, bool) or not isinstance(nm, (int, float)) or nm <= 0:
            errors.append("config key risk.notional_multiple_full_tier: must be null or a positive number")
        else:
            mu = (data.get("risk") or {}).get("max_margin_use_pct")
            room = lev * float(mu) / 100.0 if isinstance(lev, int) and isinstance(mu, (int, float)) else None
            if room is not None and nm > room:
                errors.append(f"config key risk.notional_multiple_full_tier: {nm} needs more than risk.leverage {lev} "
                              f"with margin up to risk.max_margin_use_pct {mu}% of equity")
    lr = (data.get("risk") or {}).get("live_review_expectancy_floor_r", "missing")
    if lr == "missing":
        errors.append("missing config key: risk.live_review_expectancy_floor_r")
    elif lr is not None and (isinstance(lr, bool) or not isinstance(lr, (int, float))):
        errors.append("config key risk.live_review_expectancy_floor_r: must be null or a number")
    try:
        s = data["strategy"]
        if not s["tier_low_max"] < s["tier_mid_max"]:
            errors.append("strategy.tier_low_max must be < strategy.tier_mid_max")
        g = data["gates"]
        if not g["h4_ema_fast"] < g["h4_ema_slow"]:
            errors.append("gates.h4_ema_fast must be < gates.h4_ema_slow")
        if data["binance"]["daily_candles_to_load"] < s["min_daily_candles"]:
            errors.append("binance.daily_candles_to_load must be >= strategy.min_daily_candles")
        sc = data["schedule"]
        if not sc["period_entry_start_minutes"] < sc["period_entry_end_minutes"]:
            errors.append("schedule.period_entry_start_minutes must be < schedule.period_entry_end_minutes")
        idy = data.get("intraday") or {}
        if idy:
            if not idy["retrace_min"] < idy["retrace_max"]:
                errors.append("intraday.retrace_min must be < intraday.retrace_max")
            if not idy["ctx_ema_fast"] < idy["ctx_ema_slow"]:
                errors.append("intraday.ctx_ema_fast must be < intraday.ctx_ema_slow")
            if not idy["tp1_r"] < idy["tp2_r"]:
                errors.append("intraday.tp1_r must be < intraday.tp2_r")
            if not idy["tp1_r"] < idy["score_room_full_r"]:
                errors.append("intraday.score_room_full_r must be > intraday.tp1_r")
            if not idy["sl_min_atr1h"] < idy["sl_max_atr1h"] or not idy["sl_min_pct"] < idy["sl_max_pct"]:
                errors.append("intraday stop minimums must be below the maximums (sl_min_* < sl_max_*)")
            if idy["no_progress_hours"] > idy["max_hold_hours"]:
                errors.append("intraday.no_progress_hours must be <= intraday.max_hold_hours")
            need_days = (idy["ctx_ema_slow"] + idy["ctx_slope_bars"] + 2) * 4 / 24.0 + 1
            if idy["cache_backfill_days"] < need_days:
                errors.append(f"intraday.cache_backfill_days must be >= {need_days:.1f} (the 4h background needs it)")
            if idy["enabled"] and (sc["decide_times_hkt"] or sc["manage_times_hkt"]):
                errors.append("schedule.decide_times_hkt / manage_times_hkt must be empty while intraday.enabled "
                              "(one 15-minute task runs everything)")
        step = {"rolling_4h": 4, "rolling_1h": 1}.get(s["cadence"])
        if step and not idy.get("enabled"):
            if sc["period_entry_end_minutes"] > step * 60:
                errors.append(f"schedule.period_entry_end_minutes must be <= {step * 60} (the {s['cadence']} period): "
                              f"entry windows of consecutive periods may not overlap")
            mins = []
            for x in sc["decide_times_hkt"]:
                hh, mm = str(x).split(":")
                mins.append((int(hh) * 60 + int(mm) - 8 * 60) % 1440)      # minutes after 00:00 UTC
            for h in range(0, 24, step):
                lo, hi = h * 60 + sc["period_entry_start_minutes"], h * 60 + sc["period_entry_end_minutes"]
                if not any(lo <= m <= hi for m in mins):
                    errors.append(f"schedule.decide_times_hkt: no decide time in the entry window of the "
                                  f"{h:02d}:00 UTC period (strategy.cadence {s['cadence']} decides every {step} hour"
                                  f"{'s' if step > 1 else ''})")
                    break
    except (KeyError, TypeError):
        pass
    if errors:
        raise ConfigError("; ".join(errors))


def load_config(path: Path) -> Section:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"config {path} is not a mapping")
    validate(raw)
    return Section(raw)


def config_from_dict(data: dict[str, Any]) -> Section:
    validate(data)
    return Section(copy.deepcopy(data))
