"""SystemReviewStore — 운영 회고 로그(SystemReviewLog) 영속.

prompt_version / model_name / latency / input_hash / runtime_window 모두 저장(재현성).
Dashboard / 운영 추이 분석이 읽는 저장소. SQLite(MVP).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Protocol

from src.system_review.models import SystemReviewLog

SYSTEM_REVIEW_VERSION = "v1"


class SystemReviewLogStore(Protocol):
    def save(self, log: SystemReviewLog) -> None: ...
    def query(self, **filters) -> list[SystemReviewLog]: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS system_review (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT,
    window_start   TEXT,
    window_end     TEXT,
    prompt_version TEXT,
    model_name     TEXT,
    input_hash     TEXT,
    latency_ms     INTEGER,
    status         TEXT,
    grade          TEXT,
    confidence     INTEGER,
    data           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sysrev_created ON system_review(created_at);
CREATE INDEX IF NOT EXISTS idx_sysrev_status  ON system_review(status);
CREATE INDEX IF NOT EXISTS idx_sysrev_grade   ON system_review(grade);
CREATE INDEX IF NOT EXISTS idx_sysrev_model   ON system_review(model_name);
CREATE INDEX IF NOT EXISTS idx_sysrev_window  ON system_review(window_start);
"""

_FILTER_COLS = ("status", "grade", "model_name", "prompt_version")


class SqliteSystemReviewStore:
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

    def save(self, log: SystemReviewLog) -> None:
        self._conn.execute(
            """INSERT INTO system_review
               (created_at, window_start, window_end, prompt_version, model_name,
                input_hash, latency_ms, status, grade, confidence, data)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (log.created_at, log.window_start, log.window_end, log.prompt_version,
             log.model_name, log.input_hash, log.latency_ms, log.status, log.grade,
             log.confidence, json.dumps(log.to_dict(), ensure_ascii=False)))
        self._conn.commit()

    def query(self, *, start: datetime | None = None, end: datetime | None = None,
              **filters) -> list[SystemReviewLog]:
        sql = "SELECT data FROM system_review WHERE 1=1"
        params: list = []
        for col in _FILTER_COLS:
            val = filters.get(col)
            if val is not None:
                sql += f" AND {col}=?"
                params.append(val)
        lo, hi = _iso(start), _iso(end)
        if lo is not None:
            sql += " AND created_at>=?"; params.append(lo)
        if hi is not None:
            sql += " AND created_at<=?"; params.append(hi)
        sql += " ORDER BY id"
        return [SystemReviewLog.from_dict(json.loads(r["data"]))
                for r in self._conn.execute(sql, params).fetchall()]


class InMemorySystemReviewStore:
    def __init__(self) -> None:
        self.logs: list[SystemReviewLog] = []

    def save(self, log: SystemReviewLog) -> None:
        self.logs.append(log)

    def query(self, *, start: datetime | None = None, end: datetime | None = None,
              **filters) -> list[SystemReviewLog]:
        lo, hi = _iso(start), _iso(end)
        out = []
        for lg in self.logs:
            if any(filters.get(c) is not None and getattr(lg, c) != filters.get(c)
                   for c in _FILTER_COLS):
                continue
            if lo is not None and (lg.created_at is None or lg.created_at < lo):
                continue
            if hi is not None and (lg.created_at is None or lg.created_at > hi):
                continue
            out.append(lg)
        return out


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt is not None else None
