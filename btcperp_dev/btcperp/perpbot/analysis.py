"""Readable analysis of a decision, in Traditional Chinese (v1.5.0).

Built only from the decision record the engine stores (or the same fields computed by `preview`), so the text
always matches what the bot actually decided. No I/O.
"""

from __future__ import annotations

from typing import Any

ACTION_ZH = {"enter": "開倉", "hold": "繼續持有", "none": "唔做嘢", "close": "平倉", "flip": "反手",
             "close_then_enter": "平倉再開倉", "paused": "暫停中（唔做策略動作）"}
CLOSE_ZH = {"flip": "反方向強訊號（反手）", "three_day_rule": "連續 3 日（72 小時）反方向", "funding_rule": "資金費擠擁",
            "flat_rule": "訊號太弱（空倉規則）"}
GATE_ZH = {"ema200_regime": "200 日線", "h4_trend": "4 小時趨勢", "extreme_funding": "資金費", "event_window": "經濟數據"}
NOTE_ZH = (
    ("bold mode: TP/SL only", "孤注模式：只等止賺或止損，策略平倉／反手唔執行"),
    ("below the entry threshold", "分數未到入場門檻：唔開新倉"),
    ("same direction: hold", "同方向：繼續持有"),
    ("weak opposite signal", "反方向但訊號弱：繼續持有，止損止賺不變"),
    ("score 0", "分數係 0：不變"),
    ("event window: flip suppressed", "經濟數據時段：唔反手，繼續持有"),
    ("opposite signal not yet confirmed", "反方向訊號未確認（要連續兩個時段）：繼續持有"),
    ("event window (FOMC/CPI/NFP)", "經濟數據時段（FOMC／CPI／非農）：唔開新倉"),
    ("extreme funding percentile", "資金費極端：唔開擠擁嗰邊"),
    ("already entered this", "呢個時段已經入過場"),
    ("late decision after the entry window", "過咗入場時間補做：只執行平倉規則，唔入場"),
    ("region blocked", "地區限制：唔開新倉"),
    ("economic calendar coverage ended", "經濟日曆過期：唔開新倉"),
    ("this computer's clock differs", "電腦時鐘同交易所差太遠：唔開新倉"),
    ("exchange server time unreadable", "讀唔到交易所時間：唔開新倉"),
    ("crowded funding: hold", "資金費擠擁：持有（變體）"),
    ("funding rule suppressed", "經濟數據時段：暫不執行資金費平倉"),
    ("3-day rule suppressed", "經濟數據時段：暫不執行 3 日規則"),
    ("paused:", "暫停中：倉位同止損止賺不變"),
)


def zh_note(text: str) -> str:
    for key, zh in NOTE_ZH:
        if text.startswith(key) or key in text:
            return zh
    return text


def _p(x: Any, d: int = 0) -> str:
    if x is None:
        return "–"
    return f"{float(x):,.{d}f}"


def _dir(d: int) -> str:
    return {1: "做多", -1: "做空"}.get(int(d or 0), "冇方向")


def render(decision: dict[str, Any], cfg: Any, *, equity: float | None = None, ramp: bool = False,
           next_hkt: str | None = None, position: dict[str, Any] | None = None, title: str = "BTC 決定分析") -> list[str]:
    """Lines of text. `decision`: the stored decision data (score, inputs, gates, plan, decision_hkt)."""
    sc = decision.get("score") or {}
    inp = decision.get("inputs") or {}
    gates = decision.get("gates") or {}
    plan = decision.get("plan") or {}
    bold = getattr(cfg, "bold", None)
    bold = bold if bold is not None and bool(bold.enabled) else None
    cadence = str(plan.get("cadence") or inp.get("cadence") or "daily")
    hours = {"rolling_4h": 4, "rolling_2h": 2, "rolling_1h": 1}.get(cadence)
    rolling = hours is not None
    period = str(plan.get("period_utc") or inp.get("period_utc") or decision.get("utc_day") or "")
    if period.endswith("Z"):                                  # 2026-10-05T04:00:00Z -> 2026-10-05 04:00
        period = period[:16].replace("T", " ")
    mark = plan.get("mark") or inp.get("mark")
    atr = sc.get("atr")
    close = sc.get("close")
    out = [f"【{title}】{decision.get('decision_hkt', '')}｜時段 {period} UTC"
           f"（{f'每 {hours} 小時' if rolling else '每日'}）"]
    out.append(f"價格：Polymarket 標記價 {_p(mark)}" + (f"｜日線收市 {_p(close)}" if close is not None else ""))
    score = float(sc.get("score") or 0.0)
    tier = float(sc.get("tier_fraction") or 0.0)
    full = getattr(cfg.risk, "notional_multiple_full_tier", None) if bold is None else None
    out.append(f"分數 {score:+.1f} → {_dir(sc.get('direction'))}，"
               + (f"倉位級別 本金 ×{tier * float(full):g}" if full else f"注碼級別 {tier * 100:.0f}%"))
    ema50, ema200 = sc.get("ema_trend"), sc.get("ema_regime")
    if ema50 is not None and atr:
        gap = (float(close) - float(ema50)) / float(atr)
        out.append(f"  趨勢 {float(sc.get('trend_component') or 0):+.1f}：收市{'高' if gap >= 0 else '低'}過 50 日線 "
                   f"{_p(ema50)}（相差 {abs(gap):.1f} 倍 ATR）")
    b = float(sc.get("breakout_component") or 0.0)
    if b > 0:
        out.append(f"  突破 {b:+.0f}：收市高過前一日最高 {_p(sc.get('prev_high'))}")
    elif b < 0:
        out.append(f"  突破 {b:+.0f}：收市低過前一日最低 {_p(sc.get('prev_low'))}")
    else:
        out.append(f"  突破 0：收市喺前一日高低位之間（{_p(sc.get('prev_low'))}–{_p(sc.get('prev_high'))}）")
    if sc.get("clv") is not None:
        pos_pct = (float(sc["clv"]) + 1.0) / 2.0 * 100.0
        out.append(f"  收市位置 {float(sc.get('clv_component') or 0):+.1f}：收喺當日高低之間嘅 {pos_pct:.0f}%"
                   f"（高 {_p(sc.get('high'))}／低 {_p(sc.get('low'))}）")
    if atr and close:
        day_word = "截至決定時間嘅 24 小時" if rolling else "UTC 00:00 收市嘅一日"
        out.append(f"  （日線 = {day_word}；ATR {_p(atr)} ≈ {float(atr) / float(close) * 100:.1f}%）")
    gl = []
    for name, g in gates.items():
        label = GATE_ZH.get(name, name)
        det = g.get("detail") or {}
        if name == "ema200_regime":
            txt = f"{label} {_p(ema200)}：" + ("同方向 ✓" if not g.get("triggered") else "逆方向，注碼最多一半")
        elif name == "h4_trend":
            trend = {"up": "向上", "down": "向下", "flat": "持平"}.get(det.get("trend"), det.get("trend"))
            txt = f"{label}：{trend}" + (" ✓" if not g.get("triggered") else "，同方向唔一致，注碼最多一半")
        elif name == "extreme_funding":
            pct = det.get("percentile")
            txt = f"{label}：過去一年第 {_p(pct)} 百分位" + (" ✓" if not g.get("triggered") else "，極端，唔開擠擁嗰邊")
        elif name == "event_window":
            evs = det.get("events") or []
            txt = f"{label}：" + ("冇 ✓" if not evs else "、".join(f"{e.get('type')} {e.get('release_utc')} UTC" for e in evs)
                                 + "，時段內唔開新倉")
        else:
            txt = f"{label}：{'觸發' if g.get('triggered') else '✓'}"
        gl.append(f"  {txt}")
    if gl:
        out.append("閘門：")
        out += gl
    pos_dir = int(plan.get("position_dir_at_decision") or inp.get("position_dir") or 0)
    if position and position.get("direction"):
        out.append(f"倉位：持有{_dir(position['direction'])}，入場 {_p(position.get('entry_price'))}｜止損 "
                   f"{_p(position.get('sl_price'))}｜止賺 {_p(position.get('tp_price'))}")
    else:
        out.append("倉位：" + (f"持有{_dir(pos_dir)}" if pos_dir else "空倉"))
    action = plan.get("action", "none")
    line = f"行動：{ACTION_ZH.get(action, action)}"
    if plan.get("close_reason"):
        line += f"（原因：{CLOSE_ZH.get(plan['close_reason'], plan['close_reason'])}）"
    out.append(line)
    enter = int(plan.get("enter_direction") or 0)
    if enter and mark and bold is not None:
        # v1.7.0 bold mode: the same numbers as risk.bold_plan, before rounding to the instrument's steps
        mult = float(bold.notional_multiple)
        fees = 2.0 * float(cfg.shadow.fee_rate_estimate) * mult
        tp_move = (float(bold.target_multiple) - 1.0 + fees) / mult
        sl_move = (float(bold.max_loss_fraction) - fees) / mult
        m = float(mark)
        sl, tp = m * (1 - enter * sl_move), m * (1 + enter * tp_move)
        out.append(f"  {_dir(enter)}：入場約 {_p(m)}｜止損約 {_p(sl)}（{(sl - m) / m * 100:+.1f}%）"
                   f"｜止賺約 {_p(tp)}（{(tp - m) / m * 100:+.1f}%）")
        line = (f"  注碼：孤注，全部權益 ×{mult:g} 倉位（{int(cfg.risk.leverage)} 倍逐倉）；中止損蝕權益約 "
                f"{float(bold.max_loss_fraction) * 100:.0f}%，中止賺權益約 ×{float(bold.target_multiple):g}")
        if equity:
            line += f"（倉位約 ${float(equity) * mult:,.0f}）"
        out.append(line)
    elif enter and mark and atr:
        ex = plan.get("exit") or {}            # v1.10.0: the distances decided with the plan (1h or daily ATR)
        sl_d = float(ex.get("sl_dist") or float(cfg.exits.sl_atr_multiple) * float(atr))
        tp_d = float(ex.get("tp_dist") or float(cfg.exits.tp_atr_multiple) * float(atr))
        sl = float(mark) - enter * sl_d
        tp = float(mark) + enter * tp_d
        risk_pct = float(cfg.risk.risk_per_trade_pct) * float(plan.get("enter_fraction") or 0.0)
        if ramp:
            risk_pct *= float(cfg.risk.ramp_factor)
        m = float(mark)
        out.append(f"  {_dir(enter)}：入場約 {_p(m)}｜止損約 {_p(sl)}（{(sl - m) / m * 100:+.1f}%）"
                   f"｜止賺約 {_p(tp)}（{(tp - m) / m * 100:+.1f}%）")
        if ex.get("source") == "1h":
            out.append(f"  止損止賺跟 1 小時波幅：ATR {_p(ex.get('atr'))}（≈ {float(ex['atr']) / m * 100:.2f}%）")
        mult = getattr(cfg.risk, "notional_multiple_full_tier", None)
        if mult:            # v1.8.0: position = equity x multiple x tier; the loss follows from the stop distance
            sizing = plan.get("sizing") or {}       # v1.9.0: the per-trade leverage (and a volatility cut)
            pos_x = float(sizing.get("multiple") or float(mult) * float(plan.get("enter_fraction") or 0.0))
            lev = int(sizing.get("leverage") or cfg.risk.leverage)
            line = (f"  注碼：倉位 = 本金 ×{pos_x:.3g}（{lev} 倍逐倉）；中止損約蝕本金 "
                    f"{pos_x * abs(sl - m) / m * 100:.0f}%，中止賺約賺 {pos_x * abs(tp - m) / m * 100:.0f}%")
            if equity:
                line += f"（倉位約 ${float(equity) * pos_x:,.0f}）"
            out.append(line)
            if sizing.get("note"):
                out.append(f"  備註：波動大，倉位由 ×{float(mult) * float(plan.get('enter_fraction') or 0):g} "
                           f"減到 ×{pos_x:.3g}，等爆倉價離止損夠遠")
        else:
            risk_line = f"  注碼：打中止損最多蝕權益 {risk_pct:.2f}%"
            if equity:
                risk_line += f" ≈ ${float(equity) * risk_pct / 100.0:,.2f}"
            risk_line += (f"；倉位最多權益 {float(cfg.risk.notional_cap_pct_equity):.0f}%"
                          + ("（頭 10 筆減半）" if ramp else ""))
            out.append(risk_line)
    notes = [zh_note(n) for n in (plan.get("notes") or []) + (plan.get("entry_blocked") or [])]
    for n in dict.fromkeys(notes):
        out.append(f"  備註：{n}")
    flip_min = float(cfg.strategy.flip_min_abs_score)
    held = pos_dir or enter
    if held and bold is not None and bool(bold.hold_until_tp_sl):
        out.append("孤注模式：持倉期間唔反手、唔提早平倉，只等止賺或止損")
    elif held:
        out.append(f"反手條件：分數去到 {(-held) * flip_min:+.0f} 或{'以下' if held > 0 else '以上'}"
                   + ("，並連續兩個時段" if rolling and int(cfg.strategy.flip_confirm_periods) > 1 else ""))
    if next_hkt:
        out.append(f"下一次決定：{next_hkt}")
    return out


def short_line(decision: dict[str, Any]) -> str:
    """One line for a desktop notification."""
    sc = decision.get("score") or {}
    plan = decision.get("plan") or {}
    act = ACTION_ZH.get(plan.get("action", "none"), plan.get("action"))
    return f"分數 {float(sc.get('score') or 0):+.0f}（{_dir(sc.get('direction'))}）→ {act}"


# ---------------------------------------------------------------- v2.0.0 intraday (15-minute decisions)
SETUP_ZH = {"continuation": "順勢回調", "reversal": "轉勢（破結構後回測失敗）"}
TREND_ZH = {1: "上升結構（高點、低點都抬高）", -1: "下跌結構（高點、低點都降低）", 0: "震盪（高低點混亂）"}
REASON_ZH = (
    ("range: mixed swings", "震盪市：唔做"),
    ("structure broken since the leg", "結構已經破咗：唔順勢追"),
    ("leg too young", "呢段走勢太短"),
    ("no pullback yet", "未有回調"),
    ("retracement", "回調幅度唔啱"),
    ("pullback extreme older", "回調低／高點太舊"),
    ("15m trigger", "15 分鐘K未轉向"),
    ("trigger closed beyond the leg extreme", "已經升／跌過前高／前低：唔追"),
    ("no structure break", "冇破結構"),
    ("break older than", "破結構太耐"),
    ("no retest yet", "未回測"),
    ("broken level reclaimed", "價格重返破位：轉勢取消"),
    ("no retest:", "未回測到破位"),
    ("retest older than", "回測太舊"),
    ("trigger closed on the wrong side", "收市喺破位錯邊"),
    ("this leg was already traded", "呢段已經做過：唔重複追"),
    ("cooldown", "啱啱平倉：冷靜期"),
    ("entries today already", "今日入場次數已滿"),
    ("costs", "成本太高（相對止損）"),
    ("room", "到下一個阻力／支持嘅空間唔夠第一目標"),
    ("stop ", "止損要放太遠：離結構太遠"),
    ("position open", "已有倉位"),
    ("not enough closed candles", "K 線唔夠"),
    ("market data stale", "數據過期或唔完整：唔開新倉"),
    ("run ", "遲咗執行：唔補入場"),
    ("paused:", "暫停中"),
    ("event blackout", "經濟數據公佈前後：唔開新倉"),
    ("manage run", "管理程序：唔開倉"),
    ("score ", "分數未到入場門檻"),
)


def zh_reason(text: str) -> str:
    for key, zh in REASON_ZH:
        if text.startswith(key) or f": {key}" in text:
            return f"{zh}（{text}）"
    return text


def render_intraday(record: dict[str, Any], cfg: Any, *, equity: float | None = None,
                    position: dict[str, Any] | None = None, title: str = "BTC 15 分鐘決定") -> list[str]:
    """Lines of text for one 15-minute decision (the stored record, or the same fields from preview)."""
    dec = record.get("decision") or {}
    st = dec.get("structure") or {}
    ctx = dec.get("context") or {}
    act = record.get("action") or dec.get("action")
    L = [f"{title}：K 線收 {record.get('bar_close_utc', '')} UTC（{record.get('run_hkt', '')}）"]
    L.append(f"1 小時結構：{TREND_ZH.get(int(st.get('trend') or 0), '–')}；效率 {float(st.get('efficiency') or 0):+.2f}"
             + (f"；最近破位 {_p(st.get('break_level'))}（{'向下' if st.get('break_dir') == -1 else '向上'}）"
                if st.get("break_dir") else ""))
    bias = int(ctx.get("bias") or 0)
    L.append(f"4 小時背景：{ {1: '偏多', -1: '偏空', 0: '中性'}[bias]}（只影響倉位大細，唔會禁止做多或做空）")
    if dec.get("atr1h"):
        L.append(f"波幅：1 小時 ATR {_p(dec.get('atr1h'))}，15 分鐘 ATR {_p(dec.get('atr15'))}")
    if act == "enter" or dec.get("action") == "enter":
        d = int(dec.get("direction") or 0)
        r = float(dec.get("r") or 0)
        entry = float(dec.get("entry_ref") or 0)
        L.append(f"訊號：{_dir(d)} — {SETUP_ZH.get(dec.get('setup'), dec.get('setup'))}")
        L.append(f"參考價 {_p(entry)}；止損 {_p(dec.get('stop'))}（{r / entry * 100 if entry else 0:.2f}% = 1R）；"
                 f"第一目標 {_p(dec.get('tp1'))}（{cfg.intraday.tp1_r}R，平一半）；第二目標 {_p(dec.get('tp2'))}"
                 f"（{cfg.intraday.tp2_r}R）")
        tf = "1 小時" if str(cfg.intraday.invalidation_timeframe) == "1h" else "15 分鐘"
        L.append(f"失效位 {_p(dec.get('invalidation'))}：{tf}收市穿咗就即刻走；到第一目標後止損移去保本＋成本，"
                 f"之後跟 {cfg.intraday.trail_atr1h} 個 1 小時 ATR 追蹤；最長持倉 {cfg.intraday.max_hold_hours} 小時")
        L.append(f"成本：來回約 {_p(dec.get('cost_per_unit'))} 點 = {float(dec.get('cost_r') or 0):.2f}R"
                 f"（上限 {cfg.intraday.max_cost_r}R）；空間 {_p(dec.get('room'))} 點到 {_p(dec.get('obstacle'))}")
        comp = dec.get("components") or {}
        L.append(f"信心分數 {float(dec.get('score') or 0):.0f}（形態 {comp.get('setup', 0):.0f} + 趨勢 {comp.get('trend', 0):.0f}"
                 f" + 背景 {comp.get('context', 0):.0f} + 空間 {comp.get('room', 0):.0f} + 成本 {comp.get('cost', 0):.0f}）"
                 f" → 倉位級別 {float(record.get('tier_fraction') or 0) * float(cfg.risk.notional_multiple_full_tier or 0):g}"
                 f" 倍本金")
    else:
        for r in (dec.get("reasons") or [])[:4]:
            L.append("唔入場：" + zh_reason(str(r)))
    for b in record.get("blocks") or []:
        L.append("限制：" + zh_reason(str(b)))
    entry = record.get("entry")
    if entry:
        L.append("落單：" + ("成交" if entry.get("ok") else f"冇開到（{entry.get('reason')}）"))
    L.append(f"結果：{ {'enter': '開倉', 'none': '唔做嘢', 'blocked': '有訊號但被限制', 'rejected': '有訊號但落單前被拒'}.get(act, act)}")
    if position and position.get("intraday"):
        L.append(f"持倉：{_dir(position.get('direction'))} {position.get('qty')} @ {_p(position.get('entry_price'))}，"
                 f"止損 {_p(position.get('sl_price'))}，階段 {'保本／追蹤' if position.get('stage') == 'runner' else '初始'}")
    elif position:
        L.append("持倉：v2.0.0 之前開嘅倉，只靠交易所止損止賺，唔用日內規則")
    return L
