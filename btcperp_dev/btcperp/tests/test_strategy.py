"""Score formula, CLV edge case, score 0, size tiers, gates, decision plan rules."""

from datetime import date, timedelta

import pytest

from perpbot.indicators import Candle, atr_wilder, clv, ema, percentile_rank
from perpbot.strategy import (
    DecisionContext,
    compute_score,
    decide_plan,
    evaluate_entry,
    funding_gate,
    funding_percentile,
    h4_gate,
    opposite_streak,
    regime_gate,
    tier_fraction,
)
from perpbot.timeutil import DAY_MS, day_start_ms

D = date(2026, 10, 5)


def flat_history(n: int = 300, price: float = 100.0, a: float = 1.0) -> list[Candle]:
    start = D - timedelta(days=n)
    out = []
    for i in range(n):
        o = day_start_ms(start + timedelta(days=i))
        out.append(Candle(o, price, price + a, price - a, price, 1, o + DAY_MS))
    return out


def with_last(c: list[Candle], o: float, h: float, l: float, cl: float) -> list[Candle]:
    last = c[-1]
    return c[:-1] + [Candle(last.open_ms, o, h, l, cl, 1, last.close_ms)]


def ctx(**kw):
    base = dict(direction=1, abs_score=60.0, tier_fraction=1.0, caps=[], funding_pct=50.0, funding_high=95.0,
                funding_low=5.0, event_active=False, position_dir=0, opposite_streak=0, entered_today=False,
                paused_reason=None, flip_min_abs_score=30.0, opposite_days_rule=3, event_allows_rule_closes=True)
    base.update(kw)
    return DecisionContext(**base)


# ------------------------------------------------------------------ score
def test_score_formula_matches_hand_calculation(cfg):
    hist = with_last(flat_history(), 100.0, 104.0, 99.0, 103.0)
    sc = compute_score(hist, D, cfg.strategy)
    closes = [c.close for c in hist]
    e50 = ema(closes, 50)[-1]
    atr = atr_wilder(hist, 14)[-1]
    trend = 50 * max(-1, min(1, (103.0 - e50) / (2 * atr)))
    breakout = 25  # 103 > previous high 101
    clv_v = ((103 - 99) - (104 - 103)) / (104 - 99)
    expected = round(trend + breakout + 25 * clv_v, 4)
    assert sc.score == pytest.approx(expected)
    assert sc.breakout_component == 25
    assert sc.clv == pytest.approx(0.6)
    assert sc.direction == 1


def test_score_short_breakdown_and_trend_clip(cfg):
    hist = with_last(flat_history(), 100.0, 100.5, 80.0, 80.0)   # huge down day: trend clipped at -50
    sc = compute_score(hist, D, cfg.strategy)
    assert sc.trend_component == pytest.approx(-50.0)
    assert sc.breakout_component == -25
    assert sc.clv_component == pytest.approx(-25.0)
    assert sc.score == pytest.approx(-100.0)
    assert sc.tier_fraction == 1.0


def test_clv_edge_case_high_equals_low(cfg):
    assert clv(5.0, 5.0, 5.0) == 0.0
    hist = with_last(flat_history(), 100.0, 100.0, 100.0, 100.0)  # H == L
    sc = compute_score(hist, D, cfg.strategy)
    assert sc.clv == 0.0
    assert sc.clv_component == 0.0


def test_score_exactly_zero_is_no_signal(cfg):
    sc = compute_score(flat_history(), D, cfg.strategy)
    assert sc.score == 0.0
    assert sc.direction == 0
    assert sc.tier_fraction == 0.0
    plan = decide_plan(ctx(direction=0, abs_score=0.0, tier_fraction=0.0))
    assert plan.action == "none" and plan.enter_direction == 0
    assert "score is exactly 0" in plan.entry_blocked[0]
    hold = decide_plan(ctx(direction=0, abs_score=0.0, tier_fraction=0.0, position_dir=1))
    assert hold.action == "hold" and hold.target_direction == 1 and hold.close_reason is None


def test_score_uses_only_closed_candles_and_requires_fresh_data(cfg):
    hist = flat_history()
    future = Candle(day_start_ms(D), 100, 200, 50, 200, 1, day_start_ms(D) + DAY_MS)
    sc1 = compute_score(hist, D, cfg.strategy)
    sc2 = compute_score(hist + [future], D, cfg.strategy)  # candle of day D itself is not closed yet
    assert sc1.score == sc2.score
    from perpbot.strategy import InsufficientData

    with pytest.raises(InsufficientData):
        compute_score(hist[:-1], D, cfg.strategy)  # yesterday's candle missing
    with pytest.raises(InsufficientData):
        compute_score(hist[-150:], D, cfg.strategy)  # fewer than 200 candles


# ------------------------------------------------------------------ tiers and caps
@pytest.mark.parametrize("score,frac", [(0.01, 0.25), (29.99, 0.25), (30.0, 0.5), (40, 0.5), (50.0, 0.5),
                                        (50.01, 1.0), (100, 1.0)])
def test_size_tiers(cfg, score, frac):
    assert tier_fraction(score, cfg.strategy) == frac


def test_tiers_come_from_config(cfg_dict):
    from perpbot.config import config_from_dict

    cfg_dict["strategy"]["tier_low_fraction"] = 0.2
    c = config_from_dict(cfg_dict)
    assert tier_fraction(10, c.strategy) == 0.2


def test_gate_minimum_never_multiply():
    d, f, b = evaluate_entry(ctx(tier_fraction=1.0, caps=[("ema200_regime", 0.5), ("h4_trend", 0.5)]))
    assert (d, f, b) == (1, 0.5, [])          # min(1.0, 0.5, 0.5) = 0.5, not 0.25
    d, f, _ = evaluate_entry(ctx(tier_fraction=0.25, caps=[("ema200_regime", 0.5)]))
    assert f == 0.25
    d, f, _ = evaluate_entry(ctx(tier_fraction=1.0, caps=[("x", 0.5), ("y", 0.3)]))
    assert f == 0.3


def test_regime_and_h4_gates():
    assert regime_gate(90, 100, 1, 0.5).triggered          # long in bear regime -> cap
    assert not regime_gate(110, 100, 1, 0.5).triggered
    assert regime_gate(110, 100, -1, 0.5).cap == 0.5        # short in bull regime
    assert h4_gate(99, 100, 0, 1, 0.5).triggered            # 4h down vs long
    assert not h4_gate(101, 100, 0, 1, 0.5).triggered
    assert not h4_gate(100, 100, 0, -1, 0.5).triggered      # flat -> no disagreement


def test_funding_percentile_point_in_time_and_gate():
    base = day_start_ms(D)
    recs = [(base - i * 8 * 3_600_000, 0.0001 + (i % 50) * 1e-6) for i in range(1, 1200)]
    recs.append((base, 0.01))                  # extreme print at 00:00
    recs.append((base + 8 * 3_600_000, -0.5))  # future print must be ignored
    st = funding_percentile(recs, base + 60_000, 365)
    assert st.current_rate == 0.01 and st.percentile > 99
    assert funding_gate(st.percentile, 1, 95, 5).blocks_entry       # no new long
    assert not funding_gate(st.percentile, -1, 95, 5).blocks_entry  # short allowed
    assert funding_gate(3.0, -1, 95, 5).blocks_entry                 # no new short below p5
    assert percentile_rank([1, 2, 3, 4], 4) == pytest.approx(87.5)


# ------------------------------------------------------------------ plan rules
def test_flat_enters_unless_blocked():
    assert decide_plan(ctx()).action == "enter"
    p = decide_plan(ctx(event_active=True))
    assert p.action == "none" and any("event" in b for b in p.entry_blocked)
    p = decide_plan(ctx(funding_pct=99.0))
    assert p.action == "none" and any("funding" in b for b in p.entry_blocked)
    p = decide_plan(ctx(entered_today=True))
    assert p.action == "none"
    p = decide_plan(ctx(paused_reason="manual_pause"))
    assert p.action == "paused"


def test_opposite_signal_strength_rules():
    strong = decide_plan(ctx(direction=-1, abs_score=30.0, position_dir=1))
    assert strong.action == "flip" and strong.enter_direction == -1 and strong.close_reason == "flip"
    weak = decide_plan(ctx(direction=-1, abs_score=29.99, position_dir=1, tier_fraction=0.25))
    assert weak.action == "hold" and weak.target_direction == 1
    same = decide_plan(ctx(direction=1, abs_score=80, position_dir=1))
    assert same.action == "hold"


def test_flip_suppressed_in_event_window():
    p = decide_plan(ctx(direction=-1, abs_score=80, position_dir=1, event_active=True))
    assert p.action == "hold" and p.close_reason is None


def test_three_day_rule_and_streak_counting():
    dirs = {"2026-10-05": -1, "2026-10-04": -1, "2026-10-03": -1, "2026-10-02": 1}
    assert opposite_streak(1, date(2026, 10, 2), dirs, date(2026, 10, 5)) == 3
    assert opposite_streak(1, date(2026, 10, 3), dirs, date(2026, 10, 5)) == 2   # only days after entry
    dirs["2026-10-04"] = 0                                                        # score 0 breaks the streak
    assert opposite_streak(1, date(2026, 10, 2), dirs, date(2026, 10, 5)) == 1
    p = decide_plan(ctx(direction=-1, abs_score=10, tier_fraction=0.25, position_dir=1, opposite_streak=3))
    assert p.close_reason == "three_day_rule" and p.enter_direction == -1 and p.enter_fraction == 0.25
    p2 = decide_plan(ctx(direction=-1, abs_score=10, tier_fraction=0.25, position_dir=1, opposite_streak=2))
    assert p2.action == "hold"


def test_funding_rule_closes_crowded_side():
    p = decide_plan(ctx(direction=-1, abs_score=10, tier_fraction=0.25, position_dir=1, funding_pct=97.0))
    assert p.close_reason == "funding_rule" and p.enter_direction == -1
    p = decide_plan(ctx(direction=1, abs_score=60, position_dir=1, funding_pct=97.0))
    assert p.close_reason == "funding_rule" and p.enter_direction == 0      # new long blocked by funding gate
    assert p.action == "close"
    p = decide_plan(ctx(direction=1, abs_score=60, position_dir=-1, funding_pct=2.0))
    assert p.close_reason == "funding_rule" and p.enter_direction == 1
