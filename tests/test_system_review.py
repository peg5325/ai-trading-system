"""System Review Engine 테스트 (FakeReviewProvider, 무네트워크).

정상 / 빈 데이터 / timeout / invalid json / rate limit / schema mismatch / 저장 / roundtrip /
input_hash 재현성 / window 요약 / store CRUD.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ai.models import AIRateLimitError, AITimeoutError
from src.journal.models import InMemoryJournalStore, JournalCycle
from src.review.providers import FakeReviewProvider
from src.runtime.metrics_store import CycleMetric, InMemoryMetricsStore
from src.system_review.engine import SystemReviewConfig, SystemReviewEngine
from src.system_review.models import (
    SYSTEM_REVIEW_FAILED,
    SYSTEM_REVIEW_OK,
    SystemReviewLog,
    SystemReviewReport,
)
from src.system_review.store import InMemorySystemReviewStore, SqliteSystemReviewStore

GOOD = {"grade": "A", "summary": "Runtime stable.", "major_issues": [],
        "recommendations": ["increase log retention"], "confidence": 93}
NOW = "2026-07-07T12:00:00Z"


def cm(status, *, health="OK", cycles_run=0, skipped=0, api_errors=0, exceptions=0,
       candidates=0, ai_approve=0, ai_reject=0, orders=0, loop_ms=3000.0):
    return CycleMetric(
        created_at="2026-07-07T04:10:00Z", cycle_id="C", cycle_ts="2026-07-07T04:10:00Z",
        session="REGULAR", status=status, reason="OK", loop_ms=loop_ms, ai_ms=None,
        fetch_latency_ms=None, health_status=health, api_ok=True, ai_ok=True,
        broker_ok=True, journal_ok=True, replay_ok=True,
        cycles_run=cycles_run, skipped=skipped, api_errors=api_errors, exceptions=exceptions,
        candidates=candidates, ai_approve=ai_approve, ai_reject=ai_reject, orders=orders)


def seed_stores():
    m = InMemoryMetricsStore()
    m.record(cm("RAN", cycles_run=1, candidates=3, ai_approve=3, orders=3))
    m.record(cm("RAN", cycles_run=1, candidates=2, ai_approve=1, orders=1, api_errors=1))
    m.record(cm("SKIPPED", skipped=1))
    m.record(cm("ERROR", health="DEGRADED", exceptions=1))
    j = InMemoryJournalStore()
    j.save_cycle(JournalCycle("C1", "2026-07-07T04:10:00Z", "REGULAR", ["SOXL"], "RAN",
                              "OK", None, outcomes=[{"symbol": "SOXL"}]))
    j.save_cycle(JournalCycle("C2", "2026-07-07T04:15:00Z", "REGULAR", ["SOXL"], "ERROR",
                              "PIPELINE_ERROR", "boom", outcomes=[]))
    return m, j


def engine(provider, m, j, store=None):
    return SystemReviewEngine(provider, m, j, SystemReviewConfig(), log_store=store,
                              now_iso=lambda: NOW)


# ---- 정상 ----
def test_normal_review():
    m, j = seed_stores()
    store = InMemorySystemReviewStore()
    rep = engine(FakeReviewProvider(response=GOOD, model_name="gpt-5.5"), m, j, store).review()
    assert rep.status == SYSTEM_REVIEW_OK and rep.grade == "A" and rep.confidence == 93
    assert rep.summary == "Runtime stable."
    rt = rep.runtime_window["runtime"]
    assert rt["cycles_run"] == 2 and rt["candidates"] == 5 and rt["orders"] == 4
    assert rt["api_errors"] == 1 and rt["exceptions"] == 1 and rt["degraded_cycles"] == 1
    assert rt["approve_ratio"] == 1.0        # ai_approve 4 / ai_total 4
    assert rep.runtime_window["journal"]["error_cycles"] == 1
    assert len(store.logs) == 1 and store.logs[0].status == SYSTEM_REVIEW_OK


# ---- 빈 데이터 ----
def test_empty_data():
    m, j = InMemoryMetricsStore(), InMemoryJournalStore()
    rep = engine(FakeReviewProvider(response=GOOD, model_name="gpt-5.5"), m, j).review()
    assert rep.status == SYSTEM_REVIEW_OK
    rt = rep.runtime_window["runtime"]
    assert rt["metric_rows"] == 0 and rt["cycles_run"] == 0 and rt["approve_ratio"] == 0.0
    assert rep.runtime_window["journal"]["cycles"] == 0


# ---- 실패: timeout / rate limit / invalid json / schema ----
def test_timeout_failed():
    m, j = seed_stores()
    store = InMemorySystemReviewStore()
    rep = engine(FakeReviewProvider(raise_exc=AITimeoutError("t"), model_name="x"), m, j, store).review()
    assert rep.status == SYSTEM_REVIEW_FAILED and rep.grade == "UNKNOWN"
    assert rep.error == "SYSTEM_REVIEW_TIMEOUT" and rep.confidence == 0
    assert store.logs[0].status == SYSTEM_REVIEW_FAILED       # 실패도 로그 저장


def test_rate_limit_failed():
    m, j = seed_stores()
    rep = engine(FakeReviewProvider(raise_exc=AIRateLimitError("r"), model_name="x"), m, j).review()
    assert rep.status == SYSTEM_REVIEW_FAILED and rep.error == "SYSTEM_REVIEW_RATE_LIMIT"


def test_invalid_json_failed():
    m, j = seed_stores()
    rep = engine(FakeReviewProvider(raw="not json {{{", model_name="x"), m, j).review()
    assert rep.status == SYSTEM_REVIEW_FAILED and rep.error == "SYSTEM_REVIEW_INVALID_JSON"


def test_schema_mismatch_failed():
    m, j = seed_stores()
    bad = {"grade": "Z", "summary": "x", "major_issues": [], "recommendations": [], "confidence": 50}
    rep = engine(FakeReviewProvider(response=bad, model_name="x"), m, j).review()
    assert rep.status == SYSTEM_REVIEW_FAILED and rep.error == "SYSTEM_REVIEW_SCHEMA_MISMATCH"


# ---- 저장 필드 (req 8) ----
def test_log_fields_saved():
    m, j = seed_stores()
    store = InMemorySystemReviewStore()
    eng = engine(FakeReviewProvider(response=GOOD, model_name="gpt-5.5"), m, j, store)
    eng.review(start=datetime(2026, 7, 7, 0, tzinfo=timezone.utc),
               end=datetime(2026, 7, 8, 0, tzinfo=timezone.utc))
    log = store.logs[0]
    assert log.prompt_version == "system_review_v1" and log.model_name == "gpt-5.5"
    assert len(log.input_hash) == 64 and log.latency_ms >= 0
    assert log.window_start == "2026-07-07T00:00:00Z" and log.window_end == "2026-07-08T00:00:00Z"
    assert "runtime" in log.runtime_window and log.created_at == NOW


# ---- roundtrip ----
def test_report_and_log_roundtrip():
    m, j = seed_stores()
    eng = engine(FakeReviewProvider(response=GOOD, model_name="gpt-5.5"), m, j)
    rep = eng.review()
    assert SystemReviewReport.from_dict(rep.to_dict()).to_dict() == rep.to_dict()
    log = eng.last_log
    assert SystemReviewLog.from_dict(log.to_dict()).to_dict() == log.to_dict()


def test_sqlite_store_roundtrip(tmp_path):
    m, j = seed_stores()
    store = SqliteSystemReviewStore(tmp_path / "sysrev.sqlite")
    engine(FakeReviewProvider(response=GOOD, model_name="gpt-5.5"), m, j, store).review()
    got = store.query()
    assert len(got) == 1 and got[0].grade == "A" and got[0].status == SYSTEM_REVIEW_OK
    assert len(store.query(grade="A")) == 1 and len(store.query(grade="F")) == 0
    assert len(store.query(status=SYSTEM_REVIEW_OK)) == 1
    store.close()


# ---- input_hash 재현성 ----
def test_input_hash_reproducible():
    m, j = seed_stores()
    h1 = engine(FakeReviewProvider(response=GOOD, model_name="gpt-5.5"), m, j).review().runtime_window
    e1 = engine(FakeReviewProvider(response=GOOD, model_name="gpt-5.5"), m, j)
    e1.review()
    e2 = engine(FakeReviewProvider(response=GOOD, model_name="gpt-5.5"), m, j)
    e2.review()
    assert e1.last_log.input_hash == e2.last_log.input_hash        # 같은 데이터/prompt/model → 동일
    # 데이터가 바뀌면 hash 변화
    m.record(cm("RAN", cycles_run=1, orders=5))
    e3 = engine(FakeReviewProvider(response=GOOD, model_name="gpt-5.5"), m, j)
    e3.review()
    assert e3.last_log.input_hash != e1.last_log.input_hash
