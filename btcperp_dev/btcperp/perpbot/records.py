"""Typed helpers over the append-only store: bot state, daily intents, trades."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from perpbot.storage import Store

POSITION_STATES = ("flat", "pending_entry", "open", "pending_exit")


def client_order_id(utc_day: str, action: str) -> str:
    """32 lowercase hex chars: hash of UTC date + action."""
    return hashlib.sha256(f"{utc_day}|{action}".encode()).hexdigest()[:32]


class Records:
    def __init__(self, store: Store) -> None:
        self.s = store

    # ------------------------------------------------------------ state
    def state(self) -> dict[str, Any]:
        row = self.s.latest("state_log")
        if row is None:
            return {"position_state": "flat", "paused": False, "pause_reasons": [], "since_ms": 0}
        reasons = json.loads(row["pause_reasons"] or "[]")
        return {"position_state": row["position_state"], "paused": bool(row["paused"]), "pause_reasons": reasons,
                "since_ms": row["ts_ms"]}

    def set_state(self, *, position_state: str | None = None, add_reason: str | None = None,
                  clear_reasons: bool = False, remove_reasons: tuple[str, ...] = (), note: str = "") -> dict[str, Any]:
        cur = self.state()
        ps = position_state or cur["position_state"]
        if ps not in POSITION_STATES:
            raise ValueError(f"bad position state {ps}")
        reasons = [] if clear_reasons else [r for r in cur["pause_reasons"] if r not in remove_reasons]
        if add_reason and add_reason not in reasons:
            reasons.append(add_reason)
        if ps == cur["position_state"] and reasons == cur["pause_reasons"] and not note:
            return cur
        self.s.insert("state_log", position_state=ps, paused=1 if reasons else 0, pause_reasons=json.dumps(reasons), note=note)
        return self.state()

    def display_state(self) -> str:
        st = self.state()
        if st["paused"]:
            return "paused" if st["position_state"] == "flat" else f"paused ({st['position_state']})"
        return st["position_state"]

    def last_resume_ms(self) -> int:
        row = self.s.latest("state_log", "note LIKE 'resume%'")
        return int(row["ts_ms"]) if row else 0

    # ------------------------------------------------------------ intents
    def intent(self, utc_day: str) -> dict[str, Any] | None:
        row = self.s.latest("intents", "utc_day = ?", [utc_day])
        return row["data"] if row else None

    def write_intent(self, utc_day: str, plan: dict[str, Any]) -> None:
        self.s.insert("intents", utc_day=utc_day, action=plan["action"], target_direction=plan["target_direction"], data=plan)

    def intent_event(self, utc_day: str, step: str, status: str, data: dict[str, Any] | None = None) -> None:
        self.s.insert("intent_events", utc_day=utc_day, step=step, status=status, data=data or {})

    def intent_events(self, utc_day: str, since_ms: int = 0) -> list[dict[str, Any]]:
        return self.s.query("SELECT * FROM intent_events WHERE utc_day = ? AND ts_ms >= ? ORDER BY id", [utc_day, since_ms])

    # ------------------------------------------------------------ trades
    def open_trade(self) -> dict[str, Any] | None:
        rows = self.s.query(
            "SELECT * FROM trades WHERE event='open' AND trade_uid NOT IN "
            "(SELECT trade_uid FROM trades WHERE event='close') ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        trade = dict(rows[0]["data"])
        for u in self.s.query("SELECT * FROM trades WHERE event='update' AND trade_uid=? ORDER BY id", [trade["trade_uid"]]):
            trade.update({k: v for k, v in u["data"].items() if k != "note"})
        return trade

    def record_trade(self, event: str, trade_uid: str, direction: int, data: dict[str, Any]) -> None:
        self.s.insert("trades", trade_uid=trade_uid, event=event, direction=direction, data=data)

    def closed_trades(self, since_ms: int = 0, live_only: bool = True) -> list[dict[str, Any]]:
        """Closed trades, newest first: open-data merged with close-data."""
        closes = self.s.query("SELECT * FROM trades WHERE event='close' AND ts_ms >= ? ORDER BY id DESC", [since_ms])
        out = []
        seen: set[str] = set()
        for c in closes:
            if c["trade_uid"] in seen:   # a later 'close' row is a correction of an earlier one
                continue
            seen.add(c["trade_uid"])
            o = self.s.latest("trades", "event='open' AND trade_uid=?", [c["trade_uid"]])
            if o is None:
                continue
            d = dict(o["data"])
            d.update(c["data"])
            d["closed_ts_ms"] = c["ts_ms"]
            d["code_version_open"] = o["code_version"]
            d["config_version_open"] = o["config_version"]
            d["code_version_close"] = c["code_version"]
            d["config_version_close"] = c["config_version"]
            if live_only and not d.get("live", True):
                continue
            out.append(d)
        return out

    def live_trades_opened(self) -> int:
        n = 0
        for r in self.s.query("SELECT data FROM trades WHERE event='open'"):
            if r["data"].get("live", True) and not r["data"].get("external", False):
                n += 1
        return n

    # ------------------------------------------------------------ alerts
    def alert_sent(self, dedupe_key: str) -> bool:
        return self.s.count("alerts", "dedupe_key = ?", [dedupe_key]) > 0
