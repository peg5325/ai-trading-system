"""SqliteJournalStore — Trading Journal 의 MVP SQLite 구현.

- journal_cycle: 사이클 전체를 JSON blob 으로 저장(주문 없는 사이클 포함).
- journal_outcome: symbol 별 outcome 을 별도 행으로 저장 → symbol/기간 조회 지원.
동일 인터페이스(JournalStore)로 추후 PostgreSQL/TimescaleDB 로 교체 가능.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from src.journal.models import JournalCycle, iso_z

_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_cycle (
    cycle_id    TEXT PRIMARY KEY,
    cycle_ts    TEXT,
    session     TEXT,
    status      TEXT,
    reason      TEXT,
    error       TEXT,
    order_count INTEGER,
    data        TEXT NOT NULL      -- JournalCycle 전체 JSON
);
CREATE TABLE IF NOT EXISTS journal_outcome (
    cycle_id    TEXT NOT NULL,
    cycle_ts    TEXT,
    symbol      TEXT NOT NULL,
    action      TEXT,
    reason      TEXT,
    data        TEXT NOT NULL      -- outcome dict JSON
);
CREATE INDEX IF NOT EXISTS idx_outcome_symbol ON journal_outcome(symbol);
CREATE INDEX IF NOT EXISTS idx_cycle_ts ON journal_cycle(cycle_ts);
"""


class SqliteJournalStore:
    def __init__(self, db_path: str | Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def flush(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def save_cycle(self, cycle: JournalCycle) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO journal_cycle
               (cycle_id, cycle_ts, session, status, reason, error, order_count, data)
               VALUES (?,?,?,?,?,?,?,?)""",
            (cycle.cycle_id, cycle.cycle_ts, cycle.session, cycle.status,
             cycle.reason, cycle.error, cycle.order_count,
             json.dumps(cycle.to_dict(), ensure_ascii=False)))
        # symbol 별 outcome 재기록 (멱등)
        self._conn.execute("DELETE FROM journal_outcome WHERE cycle_id=?", (cycle.cycle_id,))
        rows = [
            (cycle.cycle_id, cycle.cycle_ts, o.get("symbol"), o.get("action"),
             o.get("reason"), json.dumps(o, ensure_ascii=False))
            for o in cycle.outcomes
        ]
        if rows:
            self._conn.executemany(
                """INSERT INTO journal_outcome
                   (cycle_id, cycle_ts, symbol, action, reason, data) VALUES (?,?,?,?,?,?)""",
                rows)
        self._conn.commit()

    def get_cycle(self, cycle_id: str) -> JournalCycle | None:
        cur = self._conn.execute("SELECT data FROM journal_cycle WHERE cycle_id=?", (cycle_id,))
        row = cur.fetchone()
        return JournalCycle.from_dict(json.loads(row["data"])) if row else None

    def get_by_symbol(self, symbol: str, start: datetime | None = None,
                      end: datetime | None = None) -> list[dict]:
        sql = "SELECT cycle_id, cycle_ts, data FROM journal_outcome WHERE symbol=?"
        params: list = [symbol]
        sql, params = _apply_range(sql, params, start, end)
        sql += " ORDER BY cycle_ts"
        out = []
        for r in self._conn.execute(sql, params).fetchall():
            out.append({"cycle_id": r["cycle_id"], "cycle_ts": r["cycle_ts"],
                        **json.loads(r["data"])})
        return out

    def get_by_time_range(self, start: datetime | None = None,
                          end: datetime | None = None) -> list[JournalCycle]:
        sql = "SELECT data FROM journal_cycle WHERE 1=1"
        params: list = []
        sql, params = _apply_range(sql, params, start, end)
        sql += " ORDER BY cycle_ts"
        return [JournalCycle.from_dict(json.loads(r["data"]))
                for r in self._conn.execute(sql, params).fetchall()]


def _apply_range(sql, params, start, end):
    lo, hi = iso_z(start), iso_z(end)
    if lo is not None:
        sql += " AND cycle_ts>=?"
        params.append(lo)
    if hi is not None:
        sql += " AND cycle_ts<=?"
        params.append(hi)
    return sql, params
