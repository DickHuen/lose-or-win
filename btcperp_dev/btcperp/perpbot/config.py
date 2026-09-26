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
    ("strategy.flip_min_abs_score", _NUM, None),
    ("strategy.opposite_days_rule", int, lambda v: v >= 1),
    ("strategy.tighten_sl_on_same_direction", bool, None),
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
    ("exits.entry_slippage_bps", _NUM, lambda v: 0 <= v <= 500),
    ("exits.entry_attempts", int, lambda v: 1 <= v <= 5),
    ("exits.close_slippage_bps", _NUM, lambda v: 0 <= v <= 2000),
    ("exits.close_attempts", int, lambda v: 1 <= v <= 10),
    ("risk.risk_per_trade_pct", _NUM, lambda v: 0 < v <= 5),
    ("risk.ramp_trades", int, lambda v: v >= 0),
    ("risk.ramp_factor", _NUM, lambda v: 0 < v <= 1),
    ("risk.leverage", int, lambda v: 1 <= v <= 20),
    ("risk.cross_margin", bool, None),
    ("risk.notional_cap_pct_equity", _NUM, lambda v: 0 < v <= 300),
    ("risk.liq_min_sl_multiple", _NUM, lambda v: v >= 1),
    ("risk.liq_estimate_mmr_divisor", _NUM, lambda v: v > 0),
    ("risk.kill_drawdown_pct", _NUM, lambda v: 0 < v < 100),
    ("risk.kill_losing_streak_pct", _NUM, lambda v: 0 < v < 100),
    ("risk.expectancy_window_trades", int, lambda v: v >= 1),
    ("risk.equity_source", str, lambda v: v in ("wallet_plus_upnl", "total_account_value")),
    ("risk.equity_floor_pct_of_net_funded", _NUM, lambda v: 0 < v < 100),
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
    ("smoketest.resting_order_offset_pct", _NUM, lambda v: 0 < v < 50),
    ("smoketest.probe_proxy_withdrawal", bool, None),
    ("smoketest.perps_deposit_contract", str, None),
    ("smoketest.collateral_token", str, None),
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
        s = data["strategy"]
        if not s["tier_low_max"] < s["tier_mid_max"]:
            errors.append("strategy.tier_low_max must be < strategy.tier_mid_max")
        g = data["gates"]
        if not g["h4_ema_fast"] < g["h4_ema_slow"]:
            errors.append("gates.h4_ema_fast must be < gates.h4_ema_slow")
        if data["binance"]["daily_candles_to_load"] < s["min_daily_candles"]:
            errors.append("binance.daily_candles_to_load must be >= strategy.min_daily_candles")
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
