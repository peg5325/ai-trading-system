"""Runtime Metrics Store — cycle별 운영 지표 + health 를 SQLite 에 영속.

status.json 은 최신 스냅샷만 담지만, 여기서는 **매 cycle 을 append-only 로 기록**한다.
System Review Engine / Dashboard 가 읽는 운영 지표의 Primary Source.

- read-only 원본과 무관(운영 관측만). 매매/주문 없음.
- 각 row = 한 cycle 의 per-cycle delta(집계값 아님) + health + 지연.
- 결정론: created_at 는 외부 주입(now_iso).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from src.journal.models import ts_from_cycle_id

# RuntimeMetrics.to_dict() 의 순수 카운트 키(평균 제외) → per-cycle delta 대상
COUNT_KEYS = ("cycles_run", "skipped", "api_errors", "pipeline_errors", "exceptions",
              "sqlite_locks", "candidates", "ai_approve", "ai_wait", "ai_reject",
              "risk_block", "orders", "buys", "sells", "exits", "eods")


@dataclass
class CycleMetric:
    created_at: str | None
    cycle_id: str | None
    cycle_ts: str | None
    session: str | None
    status: str | None
    reason: str | None
    loop_ms: float
    ai_ms: float | None
    fetch_latency_ms: float | None      # 루프가 아직 별도 측정 안 함 → 보통 None(향후)
    health_status: str | None
    api_ok: bool | None
    ai_ok: bool | None
    broker_ok: bool | None
    journal_ok: bool | None
    replay_ok: bool | None
    # per-cycle 카운트
    cycles_run: int = 0
    skipped: int = 0
    api_errors: int = 0
    pipeline_errors: int = 0
    exceptions: int = 0
    sqlite_locks: int = 0
    candidates: int = 0
    ai_approve: int = 0
    ai_wait: int = 0
    ai_reject: int = 0
    risk_block: int = 0
    orders: int = 0
    buys: int = 0
    sells: int = 0
    exits: int = 0
    eods: int = 0
    reviews: int = 0                    # 런타임 루프는 회고 안 함(향후 System Review 대비)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CycleMetric":
        fields = {k: d.get(k) for k in cls.__dataclass_fields__}
        return cls(**fields)


def cycle_delta(before: dict, after: dict) -> dict:
    """두 누적 스냅샷의 per-cycle 증분."""
    return {k: int(after.get(k, 0)) - int(before.get(k, 0)) for k in COUNT_KEYS}


def build_cycle_metric(report, dt_s: float, health: dict, delta: dict, *,
                       created_at: str | None) -> CycleMetric:
    """report(없을 수 있음) + dt + health + per-cycle delta → CycleMetric."""
    health = health or {}
    if report is not None:
        cycle_id = report.cycle_id
        cycle_ts = ts_from_cycle_id(report.cycle_id)
        session = report.session
        status = report.status
        reason = report.reason
        ai_ms = _avg_ai_ms(report.pipeline_result or {})
    else:                                # 사이클 전체 예외(생존 흡수)
        cycle_id = cycle_ts = session = None
        status, reason, ai_ms = "ERROR", "EXCEPTION", None
    cm = CycleMetric(
        created_at=created_at, cycle_id=cycle_id, cycle_ts=cycle_ts, session=session,
        status=status, reason=reason, loop_ms=round(dt_s * 1000.0, 2), ai_ms=ai_ms,
        fetch_latency_ms=None,
        health_status=health.get("status"), api_ok=health.get("api"),
        ai_ok=health.get("ai"), broker_ok=health.get("broker"),
        journal_ok=health.get("journal"), replay_ok=health.get("replay"))
    for k in COUNT_KEYS:
        setattr(cm, k, int(delta.get(k, 0)))
    return cm


def _avg_ai_ms(pr: dict) -> float | None:
    lats = [o["ai_log"]["latency_ms"] for o in pr.get("outcomes", [])
            if o.get("ai_log") and o["ai_log"].get("latency_ms") is not None]
    return round(sum(lats) / len(lats), 2) if lats else None


class MetricsStore(Protocol):
    def record(self, cm: CycleMetric) -> None: ...
    def query(self, **filters) -> list[CycleMetric]: ...
    def aggregate(self, **filters) -> dict: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_cycle (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT,
    cycle_id         TEXT,
    cycle_ts         TEXT,
    session          TEXT,
    status           TEXT,
    reason           TEXT,
    loop_ms          REAL,
    ai_ms            REAL,
    fetch_latency_ms REAL,
    health_status    TEXT,
    cycles_run       INTEGER, skipped INTEGER, api_errors INTEGER,
    pipeline_errors  INTEGER, exceptions INTEGER, sqlite_locks INTEGER,
    candidates       INTEGER, ai_approve INTEGER, ai_wait INTEGER, ai_reject INTEGER,
    risk_block       INTEGER, orders INTEGER, buys INTEGER, sells INTEGER,
    exits            INTEGER, eods INTEGER, reviews INTEGER,
    data             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metric_created ON metric_cycle(created_at);
CREATE INDEX IF NOT EXISTS idx_metric_cycle   ON metric_cycle(cycle_id);
CREATE INDEX IF NOT EXISTS idx_metric_status  ON metric_cycle(status);
CREATE INDEX IF NOT EXISTS idx_metric_health  ON metric_cycle(health_status);
CREATE INDEX IF NOT EXISTS idx_metric_ts      ON metric_cycle(cycle_ts);
"""


class SqliteMetricsStore:
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

    def record(self, cm: CycleMetric) -> None:
        d = cm.to_dict()
        cols = ["created_at", "cycle_id", "cycle_ts", "session", "status", "reason",
                "loop_ms", "ai_ms", "fetch_latency_ms", "health_status", *COUNT_KEYS, "reviews"]
        vals = [d.get(c) for c in cols] + [json.dumps(d, ensure_ascii=False)]
        placeholders = ",".join(["?"] * (len(cols) + 1))
        self._conn.execute(
            f"INSERT INTO metric_cycle ({','.join(cols)},data) VALUES ({placeholders})", vals)
        self._conn.commit()

    def query(self, *, start: datetime | None = None, end: datetime | None = None,
              status: str | None = None, health_status: str | None = None,
              limit: int | None = None) -> list[CycleMetric]:
        sql = "SELECT data FROM metric_cycle WHERE 1=1"
        params: list = []
        if status is not None:
            sql += " AND status=?"; params.append(status)
        if health_status is not None:
            sql += " AND health_status=?"; params.append(health_status)
        sql, params = _apply_created_range(sql, params, start, end)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"; params.append(limit)
        return [CycleMetric.from_dict(json.loads(r["data"]))
                for r in self._conn.execute(sql, params).fetchall()]

    def aggregate(self, *, start: datetime | None = None,
                  end: datetime | None = None) -> dict:
        """per-cycle row 를 누적 요약(Dashboard/System Review 용)."""
        sums = ",".join(f"COALESCE(SUM({k}),0) AS {k}" for k in COUNT_KEYS)
        sql = (f"SELECT COUNT(*) AS rows, {sums}, "
               "COALESCE(AVG(loop_ms),0) AS avg_loop_ms, COALESCE(MAX(loop_ms),0) AS max_loop_ms, "
               "SUM(CASE WHEN health_status IS NOT NULL AND health_status!='OK' THEN 1 ELSE 0 END) "
               "AS degraded_cycles FROM metric_cycle WHERE 1=1")
        params: list = []
        sql, params = _apply_created_range(sql, params, start, end)
        row = self._conn.execute(sql, params).fetchone()
        out = {k: row[k] for k in row.keys()}
        out["avg_loop_ms"] = round(out["avg_loop_ms"], 2)
        out["max_loop_ms"] = round(out["max_loop_ms"], 2)
        return out


class InMemoryMetricsStore:
    """테스트/개발용. append-only."""
    def __init__(self) -> None:
        self.rows: list[CycleMetric] = []

    def record(self, cm: CycleMetric) -> None:
        self.rows.append(cm)

    def query(self, *, start=None, end=None, status=None, health_status=None,
              limit=None) -> list[CycleMetric]:
        lo, hi = _iso(start), _iso(end)
        out = []
        for cm in self.rows:
            if status is not None and cm.status != status:
                continue
            if health_status is not None and cm.health_status != health_status:
                continue
            if lo is not None and (cm.created_at is None or cm.created_at < lo):
                continue
            if hi is not None and (cm.created_at is None or cm.created_at > hi):
                continue
            out.append(cm)
        return out[:limit] if limit is not None else out

    def aggregate(self, *, start=None, end=None) -> dict:
        rows = self.query(start=start, end=end)
        out = {"rows": len(rows)}
        for k in COUNT_KEYS:
            out[k] = sum(getattr(r, k) or 0 for r in rows)
        loops = [r.loop_ms for r in rows if r.loop_ms is not None]
        out["avg_loop_ms"] = round(sum(loops) / len(loops), 2) if loops else 0.0
        out["max_loop_ms"] = round(max(loops), 2) if loops else 0.0
        out["degraded_cycles"] = sum(1 for r in rows
                                     if r.health_status not in (None, "OK"))
        return out


def _apply_created_range(sql, params, start, end):
    lo, hi = _iso(start), _iso(end)
    if lo is not None:
        sql += " AND created_at>=?"; params.append(lo)
    if hi is not None:
        sql += " AND created_at<=?"; params.append(hi)
    return sql, params


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt is not None else None
