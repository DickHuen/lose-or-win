"""Append-only SQLite store.

Every table has UPDATE and DELETE blocked by triggers. Every row carries UTC
and HKT timestamps, the config version and the code version. Current state is
always "the latest row". Secrets are never written here.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from perpbot.timeutil import Clock, fmt_hkt, fmt_utc, to_ms

COMMON = ("ts_ms INTEGER NOT NULL, ts_utc TEXT NOT NULL, ts_hkt TEXT NOT NULL, "
          "config_version TEXT NOT NULL, code_version TEXT NOT NULL, run_id TEXT")

TABLES: dict[str, str] = {
    "runs": "command TEXT, event TEXT, scheduled_for TEXT, status TEXT, error TEXT, duration_s REAL, lateness_min REAL, data TEXT",
    "decisions": "utc_day TEXT, score REAL, direction INTEGER, action TEXT, reason TEXT, data TEXT",
    "intents": "utc_day TEXT, action TEXT, target_direction INTEGER, data TEXT",
    "intent_events": "utc_day TEXT, step TEXT, status TEXT, data TEXT",
    "manage_log": "state TEXT, data TEXT",
    "orders": "purpose TEXT, event TEXT, client_order_id TEXT, order_id INTEGER, status TEXT, data TEXT",
    "fills": "trade_id INTEGER UNIQUE, order_id INTEGER, client_order_id TEXT, side TEXT, price REAL, quantity REAL, "
             "fee REAL, taker INTEGER, pnl REAL, liquidation INTEGER, previous_size REAL, fill_ts_ms INTEGER, data TEXT",
    "funding_payments": "payment_id INTEGER UNIQUE, pay_ts_ms INTEGER, funding REAL, size REAL, rate REAL",
    "trades": "trade_uid TEXT, event TEXT, direction INTEGER, data TEXT",
    "equity_log": "equity REAL, wallet REAL, upnl REAL, peak REAL, drawdown_pct REAL, data TEXT",
    "state_log": "position_state TEXT, paused INTEGER, pause_reasons TEXT, note TEXT",
    "market_snapshots": "data TEXT",
    "position_snapshots": "size REAL, entry_price REAL, mark REAL, upnl REAL, cumulative_funding REAL, liquidation_price REAL, data TEXT",
    "pm_klines_1h": "open_ms INTEGER UNIQUE, open REAL, high REAL, low REAL, close REAL, volume REAL",
    "pm_klines_1d": "open_ms INTEGER UNIQUE, open REAL, high REAL, low REAL, close REAL, volume REAL",
    "pm_funding": "fund_ts_ms INTEGER UNIQUE, rate REAL",
    "bn_klines_1d": "open_ms INTEGER UNIQUE, open REAL, high REAL, low REAL, close REAL, volume REAL",
    "bn_klines_4h": "open_ms INTEGER UNIQUE, open REAL, high REAL, low REAL, close REAL, volume REAL",
    "bn_funding": "fund_ts_ms INTEGER UNIQUE, rate REAL, mark REAL",
    "shadow_log": "kind TEXT, variant TEXT, unique_key TEXT UNIQUE, data TEXT",
    "alerts": "kind TEXT, dedupe_key TEXT, sent INTEGER, text TEXT",
    "alert_deliveries": "alert_id INTEGER UNIQUE",
    "dash_snapshots": "data TEXT",
    "backtest_log": "event TEXT, data TEXT",
    "telegram_updates": "update_id INTEGER UNIQUE, command TEXT, text TEXT",
    "flows": "flow_key TEXT UNIQUE, kind TEXT, amount REAL, status TEXT, flow_ts_ms INTEGER",
}


def _json(data: Any) -> str:
    return json.dumps(data, default=str, sort_keys=True)


class Store:
    def __init__(self, path: Path, clock: Clock, config_version: str, code_version: str) -> None:
        self.path = path
        self.clock = clock
        self.config_version = config_version
        self.code_version = code_version
        self.run_id: str | None = None
        self.redact: Any = None          # callable(str) -> str; set by the CLI (review D14)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self._migrate()

    def _migrate(self) -> None:
        for name, cols in TABLES.items():
            self.conn.execute(f"CREATE TABLE IF NOT EXISTS {name} (id INTEGER PRIMARY KEY AUTOINCREMENT, {COMMON}, {cols})")
            self.conn.execute(f"CREATE TRIGGER IF NOT EXISTS {name}_no_update BEFORE UPDATE ON {name} "
                              f"BEGIN SELECT RAISE(ABORT, 'append-only table {name}'); END")
            self.conn.execute(f"CREATE TRIGGER IF NOT EXISTS {name}_no_delete BEFORE DELETE ON {name} "
                              f"BEGIN SELECT RAISE(ABORT, 'append-only table {name}'); END")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_cmd ON runs(command, ts_ms)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_intents_day ON intents(utc_day)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_intent_events_day ON intent_events(utc_day)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_coid ON orders(client_order_id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_uid ON trades(trade_uid)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_key ON alerts(dedupe_key)")

    # ------------------------------------------------------------ writes
    def _common(self, when: datetime | None = None) -> dict[str, Any]:
        now = when or self.clock.now()
        return {"ts_ms": to_ms(now), "ts_utc": fmt_utc(now), "ts_hkt": fmt_hkt(now),
                "config_version": self.config_version, "code_version": self.code_version, "run_id": self.run_id}

    def _prep(self, fields: dict[str, Any]) -> dict[str, Any]:
        row = self._common()
        for k, v in fields.items():
            v = _json(v) if (k == "data" or isinstance(v, (dict, list))) else v
            if isinstance(v, str) and self.redact is not None:
                v = self.redact(v)
            row[k] = v
        return row

    def insert(self, table: str, **fields: Any) -> int:
        row = self._prep(fields)
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", list(row.values()))
        return int(cur.lastrowid or 0)

    def insert_ignore(self, table: str, **fields: Any) -> bool:
        row = self._prep(fields)
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        cur = self.conn.execute(f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({qs})", list(row.values()))
        return cur.rowcount > 0

    def insert_many_ignore(self, table: str, rows: Iterable[dict[str, Any]]) -> int:
        n = 0
        self.conn.execute("BEGIN")
        try:
            for r in rows:
                if self.insert_ignore(table, **r):
                    n += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return n

    # ------------------------------------------------------------ reads
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        rows = self.conn.execute(sql, list(params)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("data"), str):
                try:
                    d["data"] = json.loads(d["data"])
                except ValueError:
                    pass
            out.append(d)
        return out

    def latest(self, table: str, where: str = "1=1", params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(f"SELECT * FROM {table} WHERE {where} ORDER BY id DESC LIMIT 1", params)
        return rows[0] if rows else None

    def count(self, table: str, where: str = "1=1", params: Iterable[Any] = ()) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", list(params)).fetchone()[0])

    # ------------------------------------------------------------ export / backup
    def export_csv(self, out_dir: Path, start_ms: int | None = None, end_ms: int | None = None) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for table in TABLES:
            where, params = "1=1", []
            if start_ms is not None:
                where += " AND ts_ms >= ?"
                params.append(start_ms)
            if end_ms is not None:
                where += " AND ts_ms < ?"
                params.append(end_ms)
            cur = self.conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY id", params)
            cols = [c[0] for c in cur.description]
            p = out_dir / f"{table}.csv"
            with p.open("w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                for row in cur:
                    w.writerow(list(row))
            paths.append(p)
        return paths

    def backup_to(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            self.conn.backup(target)
        finally:
            target.close()

    def close(self) -> None:
        self.conn.close()
