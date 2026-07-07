"""Review Batch Runner + ReviewStore 테스트 (FakeReviewProvider, 무네트워크).

ReviewStore CRUD/resume/필터, Runner 순회/요약/skip/resume/failed, review_version 비교, JSONL export.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ai.models import AITimeoutError
from src.journal.models import InMemoryJournalStore, JournalCycle
from src.replay.system import ReplaySystem
from src.review.batch_runner import ReviewBatchRunner, RunnerConfig
from src.review.engine import AIReviewEngine, ReviewConfig
from src.review.exporters import ReviewJsonlExporter
from src.review.models import AIReviewReport, REVIEW_FAILED, REVIEW_OK
from src.review.providers import FakeReviewProvider
from src.review.store import (
    REVIEW_SOURCE_BATCH,
    REVIEW_SOURCE_MANUAL,
    REVIEW_VERSION,
    InMemoryReviewStore,
    ReviewRecord,
    SqliteReviewStore,
)

GOOD = {
    "decision_quality": "ACCEPTABLE", "missed_signals": ["rsi divergence"],
    "risk_assessment": "ok", "alternative_action": "wait",
    "rule_improvement": ["raise min_score", "avoid overnight"],
    "confidence_reassessment": 60,
}


def _outcome(sym="SOXL"):
    return {"symbol": sym, "action": "ORDERED", "reason": "ORDERED",
            "indicator_snapshot": {"close": 100.0}, "candidate": {"score": 70.0},
            "ai_decision": {"decision": "APPROVE_ENTRY", "confidence_score": 70}}


def _cycle(cid, cts, outcomes):
    return JournalCycle(cycle_id=cid, cycle_ts=cts, session="REGULAR",
                        universe=[o["symbol"] for o in outcomes], status="RAN",
                        reason="OK", error=None, outcomes=outcomes, order_count=0,
                        portfolio={"account_value": 100000.0, "positions": {}},
                        market={"market": "US"})


def _journal_3cycles():
    j = InMemoryJournalStore()
    j.save_cycle(_cycle("20260707T041000Z", "2026-07-07T04:10:00Z", [_outcome("SOXL")]))
    j.save_cycle(_cycle("20260707T041500Z", "2026-07-07T04:15:00Z", [_outcome("QQQ")]))
    j.save_cycle(_cycle("20260707T042000Z", "2026-07-07T04:20:00Z", []))   # 무판단 → skip
    return j


def _runner(journal, provider, store, *, config=None, now="2026-07-07T05:00:00Z"):
    engine = AIReviewEngine(provider, ReviewConfig())
    replay = ReplaySystem(journal, candle_store=None)     # 캔들 불필요
    clock = iter([0.0, 1.5])
    return ReviewBatchRunner(journal, replay, engine, store, config=config,
                             sleep=lambda s: None, clock=lambda: next(clock),
                             now_iso=lambda: now)


# ---- Runner 순회/요약 ----
def test_run_reviews_and_summary():
    j = _journal_3cycles()
    store = InMemoryReviewStore()
    s = _runner(j, FakeReviewProvider(response=GOOD, model_name="gpt-test"), store).run()
    assert s.total_cycles == 3
    assert s.reviewed == 2 and s.ok == 2 and s.failed == 0
    assert s.skipped_no_outcomes == 1 and s.skipped_resumed == 0
    assert s.quality_distribution == {"ACCEPTABLE": 2}
    assert s.avg_confidence_reassessment == 60.0
    # rule_improvement 빈도 (2 cycle × 2 개 → 각 count 2)
    top = dict(s.top_rule_improvements)
    assert top["raise min_score"] == 2 and top["avoid overnight"] == 2
    assert s.elapsed_s == 1.5 and s.model_name == "gpt-test"
    # 영속화 확인
    assert store.has("20260707T041000Z", "review_v1", "gpt-test", REVIEW_VERSION)


def test_resume_skips_already_reviewed():
    j = _journal_3cycles()
    store = InMemoryReviewStore()
    prov = FakeReviewProvider(response=GOOD, model_name="gpt-test")
    _runner(j, prov, store).run()
    assert prov.calls == 2
    s2 = _runner(j, prov, store).run()                    # 재실행 → 전부 skip
    assert s2.reviewed == 0 and s2.skipped_resumed == 2
    assert prov.calls == 2                                # 추가 호출 없음


def test_no_resume_reruns():
    j = _journal_3cycles()
    store = InMemoryReviewStore()
    prov = FakeReviewProvider(response=GOOD, model_name="gpt-test")
    _runner(j, prov, store).run()
    s2 = _runner(j, prov, store).run(resume=False)
    assert s2.reviewed == 2 and s2.skipped_resumed == 0


def test_failed_review_persisted_and_listed():
    j = _journal_3cycles()
    store = InMemoryReviewStore()
    prov = FakeReviewProvider(raise_exc=AITimeoutError("t"), model_name="gpt-test")
    s = _runner(j, prov, store).run()
    assert s.ok == 0 and s.failed == 2
    assert {f["cycle_id"] for f in s.failed_cycles} == {"20260707T041000Z", "20260707T041500Z"}
    # 실패도 영속 → resume 이 재호출 막음
    assert store.has("20260707T041000Z", "review_v1", "gpt-test", REVIEW_VERSION)


def test_review_version_comparison():
    # 같은 cycle 을 다른 review_version 으로 회고 → 둘 다 저장(비교 가능)
    j = _journal_3cycles()
    store = InMemoryReviewStore()
    prov = FakeReviewProvider(response=GOOD, model_name="gpt-test")
    _runner(j, prov, store, config=RunnerConfig(review_version="v1")).run()
    _runner(j, prov, store, config=RunnerConfig(review_version="v2")).run()
    recs = store.get_by_cycle("20260707T041000Z")
    assert {r.review_version for r in recs} == {"v1", "v2"}


# ---- ReviewStore (SQLite) ----
def _report(cid="C1", status=REVIEW_OK, quality="GOOD", error=None):
    return AIReviewReport(status=status, cycle_id=cid, decision_quality=quality,
                          missed_signals=["m"], risk_assessment="r",
                          alternative_action="a", rule_improvement=["imp"],
                          confidence_reassessment=55, model_name="gpt-test",
                          prompt_version="review_v1", error=error)


def test_sqlite_store_crud_and_resume(tmp_path):
    store = SqliteReviewStore(tmp_path / "reviews.sqlite")
    rec = ReviewRecord.from_report(_report(), cycle_ts="2026-07-07T04:10:00Z",
                                   created_at="2026-07-07T05:00:00Z")
    store.save(rec)
    assert store.has("C1", "review_v1", "gpt-test", REVIEW_VERSION)
    got = store.get_by_cycle("C1")
    assert len(got) == 1 and got[0].decision_quality == "GOOD"
    # 같은 resume 키 재저장 → 덮어쓰기(중복 없음)
    store.save(ReviewRecord.from_report(_report(quality="MISTAKE"),
               cycle_ts="2026-07-07T04:10:00Z", created_at="2026-07-07T06:00:00Z"))
    got = store.get_by_cycle("C1")
    assert len(got) == 1 and got[0].decision_quality == "MISTAKE"
    store.close()


def test_sqlite_store_query_filters(tmp_path):
    store = SqliteReviewStore(tmp_path / "reviews.sqlite")
    store.save(ReviewRecord.from_report(_report("C1", quality="GOOD"),
               cycle_ts="2026-07-07T04:10:00Z", review_source=REVIEW_SOURCE_BATCH))
    store.save(ReviewRecord.from_report(_report("C2", quality="POOR"),
               cycle_ts="2026-07-07T04:15:00Z", review_source=REVIEW_SOURCE_MANUAL))
    assert len(store.query(review_source=REVIEW_SOURCE_BATCH)) == 1
    assert len(store.query(review_source=REVIEW_SOURCE_MANUAL)) == 1
    assert store.query(decision_quality="POOR")[0].cycle_id == "C2"
    assert len(store.query(status=REVIEW_OK)) == 2
    store.close()


# ---- JSONL export (review_source 포함) ----
def test_jsonl_export_includes_source(tmp_path):
    store = InMemoryReviewStore()
    store.save(ReviewRecord.from_report(_report("C1"), cycle_ts="2026-07-07T04:10:00Z",
               review_source=REVIEW_SOURCE_MANUAL, created_at="2026-07-07T05:00:00Z"))
    p = tmp_path / "out" / "reviews.jsonl"
    n = ReviewJsonlExporter().export(store.query(), p, meta={"review_version": "v1"})
    assert n == 1
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2                                # meta 헤더 + 1
    assert json.loads(lines[0])["__meta__"]["n_records"] == 1
    row = json.loads(lines[1])
    assert row["review_source"] == REVIEW_SOURCE_MANUAL
    assert row["cycle_id"] == "C1" and "rule_improvement" in row
