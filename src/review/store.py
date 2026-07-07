"""ReviewStore — AI 회고(Review) 결과의 Primary Storage.

Rule Improvement Aggregator / Dashboard / System Review Engine 이 공용으로 읽는
Single Source of Truth. SQLite(MVP) → 추후 PG/Timescale 교체 가능(동일 인터페이스).

핵심:
- resume 키 = (cycle_id + prompt_version + model_name + review_version). 중복 회고 방지.
- review_version 도입: 같은 cycle 을 다른 모델/프롬프트/리뷰버전으로 재회고해도 비교 가능.
- review_source(AI/MANUAL/BATCH/SYSTEM): 회고 출처 구분(향후 수동/시스템 회고 통합).
- 조회 성능을 위한 인덱스 설계.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from src.review.models import QUALITY_UNKNOWN, AIReviewReport

# ---- 버전 / 출처 상수 ----
REVIEW_VERSION = "v1"

REVIEW_SOURCE_AI = "AI"           # 단일 ReplayContext 에 대한 LLM 회고
REVIEW_SOURCE_MANUAL = "MANUAL"   # 사용자가 직접 작성/수정
REVIEW_SOURCE_BATCH = "BATCH"     # Review Batch Runner 대량 처리
REVIEW_SOURCE_SYSTEM = "SYSTEM"   # Runtime metrics/health 기반 시스템 회고
REVIEW_SOURCES = frozenset({REVIEW_SOURCE_AI, REVIEW_SOURCE_MANUAL,
                            REVIEW_SOURCE_BATCH, REVIEW_SOURCE_SYSTEM})


@dataclass
class ReviewRecord:
    """영속 저장/조회/Export 단위. AIReviewReport + provenance 를 평탄화."""
    cycle_id: str
    cycle_ts: str | None
    symbol: str | None                  # 사이클 단위 회고는 None(향후 종목별 회고 확장 대비)
    status: str                         # REVIEW_OK / REVIEW_FAILED
    decision_quality: str
    missed_signals: list[str]
    risk_assessment: str
    alternative_action: str
    rule_improvement: list[str]
    confidence_reassessment: int
    model_name: str
    prompt_version: str
    review_version: str
    review_source: str
    created_at: str | None = None
    error: str | None = None

    @classmethod
    def from_report(cls, report: AIReviewReport, *, cycle_ts: str | None = None,
                    symbol: str | None = None, review_version: str = REVIEW_VERSION,
                    review_source: str = REVIEW_SOURCE_BATCH,
                    created_at: str | None = None) -> "ReviewRecord":
        return cls(
            cycle_id=report.cycle_id, cycle_ts=cycle_ts, symbol=symbol,
            status=report.status, decision_quality=report.decision_quality,
            missed_signals=list(report.missed_signals),
            risk_assessment=report.risk_assessment,
            alternative_action=report.alternative_action,
            rule_improvement=list(report.rule_improvement),
            confidence_reassessment=report.confidence_reassessment,
            model_name=report.model_name, prompt_version=report.prompt_version,
            review_version=review_version, review_source=review_source,
            created_at=created_at, error=report.error)

    def to_dict(self) -> dict:
        return {
            "cycle_id": self.cycle_id, "cycle_ts": self.cycle_ts, "symbol": self.symbol,
            "status": self.status, "decision_quality": self.decision_quality,
            "missed_signals": self.missed_signals, "risk_assessment": self.risk_assessment,
            "alternative_action": self.alternative_action,
            "rule_improvement": self.rule_improvement,
            "confidence_reassessment": self.confidence_reassessment,
            "model_name": self.model_name, "prompt_version": self.prompt_version,
            "review_version": self.review_version, "review_source": self.review_source,
            "created_at": self.created_at, "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewRecord":
        return cls(
            cycle_id=d["cycle_id"], cycle_ts=d.get("cycle_ts"), symbol=d.get("symbol"),
            status=d.get("status", "REVIEW_FAILED"),
            decision_quality=d.get("decision_quality", QUALITY_UNKNOWN),
            missed_signals=list(d.get("missed_signals", [])),
            risk_assessment=d.get("risk_assessment", ""),
            alternative_action=d.get("alternative_action", ""),
            rule_improvement=list(d.get("rule_improvement", [])),
            confidence_reassessment=int(d.get("confidence_reassessment", 0)),
            model_name=d.get("model_name", ""), prompt_version=d.get("prompt_version", ""),
            review_version=d.get("review_version", REVIEW_VERSION),
            review_source=d.get("review_source", REVIEW_SOURCE_BATCH),
            created_at=d.get("created_at"), error=d.get("error"))


class ReviewStore(Protocol):
    def save(self, record: ReviewRecord) -> None: ...
    def has(self, cycle_id: str, prompt_version: str, model_name: str,
            review_version: str) -> bool: ...
    def get_by_cycle(self, cycle_id: str) -> list[ReviewRecord]: ...
    def query(self, **filters) -> list[ReviewRecord]: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS review (
    cycle_id                TEXT NOT NULL,
    cycle_ts                TEXT,
    symbol                  TEXT,
    status                  TEXT,
    decision_quality        TEXT,
    confidence_reassessment INTEGER,
    model_name              TEXT,
    prompt_version          TEXT,
    review_version          TEXT,
    review_source           TEXT,
    created_at              TEXT,
    error                   TEXT,
    data                    TEXT NOT NULL
);
-- resume 키: 같은 (cycle, prompt, model, review_version) 재회고는 덮어씀
CREATE UNIQUE INDEX IF NOT EXISTS uq_review
    ON review(cycle_id, prompt_version, model_name, review_version);
CREATE INDEX IF NOT EXISTS idx_review_cycle    ON review(cycle_id);
CREATE INDEX IF NOT EXISTS idx_review_symbol   ON review(symbol);
CREATE INDEX IF NOT EXISTS idx_review_created  ON review(created_at);
CREATE INDEX IF NOT EXISTS idx_review_model    ON review(model_name);
CREATE INDEX IF NOT EXISTS idx_review_prompt   ON review(prompt_version);
CREATE INDEX IF NOT EXISTS idx_review_version  ON review(review_version);
CREATE INDEX IF NOT EXISTS idx_review_quality  ON review(decision_quality);
CREATE INDEX IF NOT EXISTS idx_review_source   ON review(review_source);
"""

_FILTER_COLS = ("cycle_id", "symbol", "model_name", "prompt_version",
                "review_version", "decision_quality", "review_source", "status")


class SqliteReviewStore:
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

    def save(self, record: ReviewRecord) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO review
               (cycle_id, cycle_ts, symbol, status, decision_quality,
                confidence_reassessment, model_name, prompt_version, review_version,
                review_source, created_at, error, data)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record.cycle_id, record.cycle_ts, record.symbol, record.status,
             record.decision_quality, record.confidence_reassessment,
             record.model_name, record.prompt_version, record.review_version,
             record.review_source, record.created_at, record.error,
             json.dumps(record.to_dict(), ensure_ascii=False)))
        self._conn.commit()

    def has(self, cycle_id: str, prompt_version: str, model_name: str,
            review_version: str) -> bool:
        cur = self._conn.execute(
            """SELECT 1 FROM review WHERE cycle_id=? AND prompt_version=?
               AND model_name=? AND review_version=? LIMIT 1""",
            (cycle_id, prompt_version, model_name, review_version))
        return cur.fetchone() is not None

    def get_by_cycle(self, cycle_id: str) -> list[ReviewRecord]:
        return self.query(cycle_id=cycle_id)

    def query(self, *, start: datetime | None = None, end: datetime | None = None,
              **filters) -> list[ReviewRecord]:
        sql = "SELECT data FROM review WHERE 1=1"
        params: list = []
        for col in _FILTER_COLS:
            val = filters.get(col)
            if val is not None:
                sql += f" AND {col}=?"
                params.append(val)
        lo, hi = _iso(start), _iso(end)
        if lo is not None:
            sql += " AND cycle_ts>=?"
            params.append(lo)
        if hi is not None:
            sql += " AND cycle_ts<=?"
            params.append(hi)
        sql += " ORDER BY cycle_ts, cycle_id"
        return [ReviewRecord.from_dict(json.loads(r["data"]))
                for r in self._conn.execute(sql, params).fetchall()]


class InMemoryReviewStore:
    """테스트/개발용. 동일 인터페이스. resume 키로 덮어쓴다."""
    def __init__(self) -> None:
        self._rows: dict[tuple, ReviewRecord] = {}

    def _key(self, r: ReviewRecord) -> tuple:
        return (r.cycle_id, r.prompt_version, r.model_name, r.review_version)

    def save(self, record: ReviewRecord) -> None:
        self._rows[self._key(record)] = record

    def has(self, cycle_id, prompt_version, model_name, review_version) -> bool:
        return (cycle_id, prompt_version, model_name, review_version) in self._rows

    def get_by_cycle(self, cycle_id: str) -> list[ReviewRecord]:
        return self.query(cycle_id=cycle_id)

    def query(self, *, start: datetime | None = None, end: datetime | None = None,
              **filters) -> list[ReviewRecord]:
        lo, hi = _iso(start), _iso(end)
        out = []
        for r in self._rows.values():
            if any(filters.get(c) is not None and getattr(r, c) != filters.get(c)
                   for c in _FILTER_COLS):
                continue
            if lo is not None and (r.cycle_ts is None or r.cycle_ts < lo):
                continue
            if hi is not None and (r.cycle_ts is None or r.cycle_ts > hi):
                continue
            out.append(r)
        return sorted(out, key=lambda r: (r.cycle_ts or "", r.cycle_id))


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt is not None else None
