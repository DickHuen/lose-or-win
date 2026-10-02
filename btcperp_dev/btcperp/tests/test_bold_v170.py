"""v1.7.0 bold mode (owner 2026-10-02: "I want to gamble", high return, no automatic stop): each entry is one all-in
bet, equity x 19 at 20x isolated, TP where equity doubles after fees, SL where it loses 70% incl. fees, held until
TP or SL."""

from datetime import date
from decimal import Decimal

import pytest

from perpbot.config import config_from_dict
from perpbot.engine import EngineError
from perpbot.exchange.mock import default_instrument
from perpbot.records import Records
from perpbot.risk import bold_plan, quantize_qty
from perpbot.strategy import Plan, hold_for_bold

from conftest import P, hkt, ok_leverage, shipped_config

# BTC-USD as read live on 2026-09-30
LIVE = dict(quantity_decimals=5, price_decimals=1, min_notional=10.0, max_leverage=50, price_bounds=0.02,
            risk_tiers=[(0.0, 50), (250_000.0, 25), (1_000_000.0, 20)])
FEE = 0.0005                                             # the bot's estimate (live taker 0.04%)
D1, D2, D3 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)


def _inst(**kw):
    inst = default_instrument()
    for k, v in dict(LIVE, **kw).items():
        setattr(inst, k, v)
    return inst


def _plan(equity=100.0, price=83_066.0, direction=1, inst=None, **kw):
    cfg = config_from_dict(shipped_config())
    b = cfg.bold
    args = dict(equity=equity, price=price, direction=direction, notional_multiple=float(b.notional_multiple),
                leverage=int(cfg.risk.leverage), target_multiple=float(b.target_multiple),
                max_loss_fraction=float(b.max_loss_fraction), fee_rate=FEE, liq_buffer_pct=float(b.liq_buffer_pct),
                inst=inst or _inst(), mmr_divisor=float(cfg.risk.liq_estimate_mmr_divisor))
    args.update(kw)
    return bold_plan(**args)


def test_shipped_bold_config():
    cfg = config_from_dict(shipped_config())
    b, r = cfg.bold, cfg.risk
    assert b.enabled is True and b.hold_until_tp_sl is True
    assert (b.notional_multiple, b.target_multiple, b.max_loss_fraction, b.liq_buffer_pct) == (19, 2.0, 0.70, 0.3)
    assert r.leverage == 20 and r.cross_margin is False
    assert (r.kill_drawdown_pct, r.kill_losing_streak_pct) == (95, 95)
    assert (r.equity_floor_pct_of_net_funded, r.permanent_floor_pct_of_cumulative_funded) == (5, 5)
    assert r.permanent_floor_lowered_in == cfg.config_version == "1.7.0"
    assert r.live_review_expectancy_floor_r is None


@pytest.mark.parametrize("direction", [1, -1])
def test_bold_plan_doubles_or_loses_70pct_with_100_usdc(direction):
    price = 83_066.0
    size, sl, tp, liq = _plan(direction=direction, price=price)
    assert size.ok and size.qty == Decimal("0.02287") and size.capped_by == ["bold"]
    assert 1_890 < size.notional <= 1_900 and size.effective_leverage == pytest.approx(19, abs=0.01)
    assert abs(tp / price - 1) == pytest.approx((1.0 + 2 * FEE * 19) / 19, rel=1e-9)        # ~5.36%
    assert abs(sl / price - 1) == pytest.approx((0.70 - 2 * FEE * 19) / 19, rel=1e-9)       # ~3.58%
    assert (tp - price) * direction > 0 and (sl - price) * direction < 0
    q = float(size.qty)
    win = q * abs(tp - price) - FEE * q * (price + tp)
    loss = q * abs(price - sl) + FEE * q * (price + sl)
    assert win == pytest.approx(100.0, abs=0.5) and loss == pytest.approx(70.0, abs=0.5)
    # liquidation ESTIMATE at 20x on max 50x: 1/20 - 0.5/50 = 4% away, beyond the 3.58% SL by more than 0.3%
    assert abs(liq / price - 1) == pytest.approx(0.04, abs=1e-12)
    assert abs(liq - price) > abs(sl - price) + 0.003 * price


@pytest.mark.parametrize("kw,needle", [
    (dict(inst=_inst(max_leverage=20, risk_tiers=[(0.0, 20)])), "liquidation"),     # 1/20 - 0.5/20 = 2.5% < SL
    (dict(leverage=10), "needs more than 10x"),
    (dict(max_loss_fraction=0.01), "does not cover the fees"),
    (dict(equity=0.4), "below instrument min_notional"),
    (dict(leverage=60), "above instrument max_leverage"),
])
def test_bold_plan_refusals(kw, needle):
    size, *_ = _plan(**kw)
    assert not size.ok and needle in size.reject_reason


def test_hold_for_bold_drops_strategy_exits_only_while_a_bet_is_open():
    for action, reason in (("flip", "flip"), ("close", "three_day_rule"), ("close", "funding_rule"),
                           ("close_then_enter", "funding_rule")):
        p = hold_for_bold(Plan(action, -1, close_reason=reason, enter_direction=-1 if action != "close" else 0,
                               enter_fraction=1.0 if action != "close" else 0.0), 1)
        assert (p.action, p.close_reason, p.enter_direction, p.target_direction) == ("hold", None, 0, 1)
        assert any("bold mode: TP/SL only" in n for n in p.notes)
    flat = hold_for_bold(Plan("enter", 1, enter_direction=1, enter_fraction=0.25), 0)
    assert (flat.action, flat.enter_direction, flat.enter_fraction) == ("enter", 1, 0.25)
    paused = hold_for_bold(Plan("paused", 1), 1)
    assert paused.action == "paused" and not paused.notes


# ---------------------------------------------------------------- engine, live-like BTC-USD, 100 USDC
def bold_world(world, start, cash=100.0):
    w = world(start, bold__enabled=True, risk__leverage=20, risk__kill_drawdown_pct=95,
              risk__kill_losing_streak_pct=95, risk__equity_floor_pct_of_net_funded=5,
              risk__permanent_floor_pct_of_cumulative_funded=5, risk__live_review_expectancy_floor_r=None)
    for k, v in LIVE.items():
        setattr(w.ex.inst, k, v)
    w.ex.cash = cash
    return w


def bet_long(w):
    w.bn.signal(D1, "strong_long")
    w.at(hkt(2026, 10, 5, 8, 30)).decide()
    assert w.pos() > 0
    return Records(w.store).open_trade()


def test_bold_entry_is_all_in_with_its_own_sl_tp(world):
    w = bold_world(world, hkt(2026, 10, 5, 8, 30))
    t = bet_long(w)
    fok = w.fok_calls()[-1]
    ref = P + w.ex.spread / 2
    assert Decimal(fok["quantity"]) == Decimal("0.01899")                 # 100 x 19 / 100,005
    assert float(fok["tp"]) / ref - 1 == pytest.approx(0.05363, abs=2e-4)
    assert 1 - float(fok["sl"]) / ref == pytest.approx(0.03584, abs=2e-4)
    assert ("update_leverage", {"leverage": 20, "cross": False}) in w.ex.calls
    assert w.tg.has("BOLD all-in: 70% of equity at the SL, x2 at the TP")
    assert w.pos() > 0 and not w.tg.has("liquidation check")              # exchange liq 4% away: kept
    assert t["sl_price"] == float(fok["sl"]) and t["tp_price"] == float(fok["tp"])


def test_bold_bet_closed_when_the_exchange_liquidation_is_too_close(world):
    w = bold_world(world, hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.ex.liq_price_override = P * (1 - 0.037)            # beyond the 3.58% SL, but not by 0.3% of the price
    w.decide()
    assert w.pos() == 0 and w.tg.has("liquidation check")
    assert Records(w.store).closed_trades()[0]["exit_reason"] == "liq_check"


@pytest.mark.parametrize("kind,funding", [("strong_short", None), ("weak_short", 0.01)])
def test_bold_bet_ignores_flip_and_funding_rule(world, kind, funding):
    w = bold_world(world, hkt(2026, 10, 5, 8, 30))
    bet_long(w)
    w.bn.signal(D2, kind)
    if funding is not None:
        w.bn.set_funding(D2, funding)
    w.at(hkt(2026, 10, 6, 8, 30)).decide()
    assert w.pos() > 0 and not Records(w.store).closed_trades()
    assert not [c for c in w.ex.calls if c[0] == "place_order" and c[1]["reduce_only"]]
    d = w.store.latest("decisions")
    assert d["action"] == "hold"


def test_bold_tp_doubles_equity_and_the_bot_bets_again(world):
    w = bold_world(world, hkt(2026, 10, 5, 8, 30))
    t = bet_long(w)
    w.clock.advance(hours=3)
    w.ex.set_mark(t["tp_price"] + 1)
    assert w.pos() == 0
    w.at(hkt(2026, 10, 5, 12, 30)).manage()
    c = Records(w.store).closed_trades()[0]
    assert c["exit_reason"] == "TP" and c["net_pnl"] == pytest.approx(100.0, abs=1.0)
    assert w.ex.cash == pytest.approx(200.0, abs=1.0)
    w.ex.set_mark(P, trigger=False)
    w.bn.signal(D2, "strong_long")
    equity = w.ex.cash
    w.at(hkt(2026, 10, 6, 8, 30)).decide()
    assert w.pos() > 0                                     # the whole ~200 goes on the next bet
    assert Decimal(w.fok_calls()[-1]["quantity"]) == quantize_qty(equity * 19 / (P + w.ex.spread / 2), 5)


def test_bold_keeps_betting_after_two_losses(world):
    """Owner: no automatic stop. 100 -> ~30 -> ~9: the 95% drawdown / streak lines and the 5% floors still allow
    the next bet."""
    w = bold_world(world, hkt(2026, 10, 5, 8, 30))
    for day, dd in ((D1, 5), (D2, 6)):
        w.ex.set_mark(P, trigger=False)
        w.bn.signal(day, "strong_long")
        w.at(hkt(2026, 10, dd, 8, 30)).decide()
        assert w.pos() > 0, day
        t = Records(w.store).open_trade()
        w.ex.set_mark(t["sl_price"] - 1)
        assert w.pos() == 0
        w.at(hkt(2026, 10, dd, 12, 30)).manage()
    losses = [c["net_pnl"] for c in Records(w.store).closed_trades()]
    assert len(losses) == 2 and all(x < 0 for x in losses)
    assert w.ex.cash == pytest.approx(100 * 0.3 * 0.3, abs=0.5)
    assert not w.state()["paused"] and not w.tg.has("KILL SWITCH")
    w.ex.set_mark(P, trigger=False)
    w.bn.signal(D3, "strong_long")
    w.at(hkt(2026, 10, 7, 8, 30)).decide()
    assert w.pos() > 0


# ---------------------------------------------------------------- permanent floor: lowered once, by name
def test_permanent_floor_lowered_once_in_the_named_config(world, cfg_dict):
    w = world(hkt(2026, 10, 5, 12, 30))                  # floor 50%, as installed before v1.7.0
    ok_leverage(w.ex)
    w.manage()
    assert w.store.latest("equity_log")["data"]["permanent_floor_pct_max"] == 50

    def install(version, pct, lowered_in):
        cfg_dict["config_version"] = version
        cfg_dict["risk"]["permanent_floor_pct_of_cumulative_funded"] = pct
        cfg_dict["risk"]["permanent_floor_lowered_in"] = lowered_in
        w.cfg = config_from_dict(cfg_dict)
        w.store.config_version = version

    install("1.7.0-test", 5, "1.6.9-test")               # names another version: refused as before
    with pytest.raises(EngineError, match="lowers"):
        w.at(hkt(2026, 10, 5, 16, 30)).manage()
    install("1.7.0-test", 5, "1.7.0-test")
    w.at(hkt(2026, 10, 5, 20, 30)).manage()
    assert w.tg.has("permanent floor lowered") and w.store.latest("equity_log")["data"]["permanent_floor_pct_max"] == 5
    w.at(hkt(2026, 10, 6, 0, 30)).manage()                # same config again: no new alert, stays 5
    assert sum("permanent floor lowered" in m for m in w.tg.sent) == 1
    install("1.7.1-test", 3, "1.7.0-test")               # a later config cannot reuse the name
    with pytest.raises(EngineError, match="lowers"):
        w.at(hkt(2026, 10, 6, 4, 30)).manage()


def test_analysis_text_shows_the_bold_bet(world):
    w = bold_world(world, hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    w.decide()
    text = "\n".join(w.store.latest("decisions", "score IS NOT NULL")["data"]["analysis"])
    assert "注碼：孤注，全部權益 ×19 倉位（20 倍逐倉）" in text and "中止損蝕權益約 70%，中止賺權益約 ×2" in text
    assert "（倉位約 $1,900）" in text and "止損約" in text and "(-3.6%)" in text.replace("（", "(").replace("）", ")")
    assert "孤注模式：持倉期間唔反手" in text and "反手條件" not in text and "打中止損最多蝕權益" not in text
    w.bn.signal(D2, "strong_short")
    w.at(hkt(2026, 10, 6, 8, 30)).decide()
    text = "\n".join(w.store.latest("decisions", "score IS NOT NULL")["data"]["analysis"])
    assert "孤注模式：只等止賺或止損" in text and w.pos() > 0


def test_bold_entry_recovered_after_a_crash_is_not_over_budget(world):
    w = bold_world(world, hkt(2026, 10, 5, 8, 30))
    w.bn.signal(D1, "strong_long")
    eng = w.engine()

    def boom(*a, **k):
        raise RuntimeError("process killed")
    eng._record_entry = boom
    with pytest.raises(RuntimeError):
        eng.cmd_decide()
    w.ex.set_mark(P * 0.99, trigger=False)              # 1% against the bet before the bot comes back: uPnL ~ -19
    w.at(hkt(2026, 10, 5, 8, 50)).decide()
    t = Records(w.store).open_trade()
    assert t is not None and t["adopted"] and not t["external"]
    assert not w.state()["paused"] and not w.tg.has("over risk budget")
    assert len(w.fok_calls()) == 1
