"""Rule Improvement Aggregator 테스트.

빈 데이터 / 단일 리뷰 / 중복·유사 개선안 그룹핑 / 버전별 비교 / 기간별 비교 / 정규화 / JSON export.
ReviewStore(InMemory)만 사용. 매매/네트워크 없음.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.review.aggregator import (
    RuleImprovementAggregator,
    compare_reports,
    export_report_json,
    normalize_text,
)
from src.review.models import REVIEW_FAILED, REVIEW_OK
from src.review.store import InMemoryReviewStore, ReviewRecord


def rec(cid, cts, *, quality="GOOD", rule=None, missed=None, conf=60,
        status=REVIEW_OK, model="gpt-test", prompt="review_v1", rv="v1"):
    return ReviewRecord(
        cycle_id=cid, cycle_ts=cts, symbol=None, status=status,
        decision_quality=quality if status == REVIEW_OK else "UNKNOWN",
        missed_signals=list(missed or []), risk_assessment="r", alternative_action="a",
        rule_improvement=list(rule or []), confidence_reassessment=conf,
        model_name=model, prompt_version=prompt, review_version=rv,
        review_source="BATCH", created_at="2026-07-07T09:00:00Z",
        error=None if status == REVIEW_OK else "AI_TIMEOUT")


def _store(records):
    s = InMemoryReviewStore()
    for r in records:
        s.save(r)
    return s


# ---- 빈 데이터 ----
def test_empty():
    rep = RuleImprovementAggregator(_store([])).aggregate()
    assert rep.total_reviews == 0 and rep.ok_reviews == 0
    assert rep.rule_improvement_clusters == [] and rep.missed_signal_clusters == []
    assert rep.confidence == {"n": 0, "avg": None, "min": None, "max": None}


# ---- 단일 리뷰 ----
def test_single_review():
    s = _store([rec("C1", "2026-07-07T04:10:00Z", quality="POOR",
                    rule=["raise min score"], missed=["rsi divergence"], conf=40)])
    rep = RuleImprovementAggregator(s).aggregate()
    assert rep.total_reviews == 1 and rep.ok_reviews == 1
    assert rep.total_rule_improvements == 1
    assert rep.decision_quality_distribution == {"POOR": 1}
    assert rep.confidence == {"n": 1, "avg": 40.0, "min": 40, "max": 40}
    c = rep.rule_improvement_clusters[0]
    assert c.representative == "raise min score" and c.count == 1
    assert c.quality_distribution == {"POOR": 1}


# ---- 중복(정규화 exact) + 빈도 랭킹 ----
def test_duplicate_suggestions_ranked():
    s = _store([
        rec("C1", "2026-07-07T04:10:00Z", rule=["Raise min score.", "avoid overnight"]),
        rec("C2", "2026-07-07T04:15:00Z", rule=["raise  min score", "widen stop"]),
        rec("C3", "2026-07-07T04:20:00Z", rule=["raise min score!"]),
    ])
    rep = RuleImprovementAggregator(s).aggregate()
    top = rep.rule_improvement_clusters[0]
    assert top.count == 3                              # 3 변형이 같은 정규화로 병합
    assert normalize_text(top.representative) == "raise min score"
    assert rep.total_rule_improvements == 5      # C1:2 + C2:2 + C3:1


# ---- 유사(부분 일치) 그룹핑 (Jaccard) ----
def test_similar_grouping():
    s = _store([
        rec("C1", "2026-07-07T04:10:00Z", rule=["avoid overnight session"]),
        rec("C2", "2026-07-07T04:15:00Z", rule=["avoid overnight"]),
    ])
    rep = RuleImprovementAggregator(s, similarity_threshold=0.6).aggregate()
    # {avoid,overnight,session} vs {avoid,overnight} → jaccard 2/3 ≥ 0.6 → 1 클러스터
    assert len(rep.rule_improvement_clusters) == 1
    assert rep.rule_improvement_clusters[0].count == 2
    # 임계 높이면 분리
    rep2 = RuleImprovementAggregator(s, similarity_threshold=0.9).aggregate()
    assert len(rep2.rule_improvement_clusters) == 2


# ---- 실패 리뷰는 제안 집계에서 제외 ----
def test_failed_reviews_excluded():
    s = _store([
        rec("C1", "2026-07-07T04:10:00Z", rule=["raise min score"]),
        rec("C2", "2026-07-07T04:15:00Z", status=REVIEW_FAILED, rule=[]),
    ])
    rep = RuleImprovementAggregator(s).aggregate()
    assert rep.total_reviews == 2 and rep.ok_reviews == 1 and rep.failed_reviews == 1
    assert rep.total_rule_improvements == 1


# ---- 버전별 비교 ----
def test_version_comparison():
    s = _store([
        rec("C1", "2026-07-07T04:10:00Z", rv="v1", rule=["raise min score"]),
        rec("C2", "2026-07-07T04:15:00Z", rv="v1", rule=["raise min score"]),
        rec("C1", "2026-07-07T04:10:00Z", rv="v2", rule=["raise min score", "widen stop"], prompt="review_v2"),
    ])
    agg = RuleImprovementAggregator(s)
    a = agg.aggregate(review_version="v1")
    b = agg.aggregate(review_version="v2")
    assert a.ok_reviews == 2 and b.ok_reviews == 1
    diff = compare_reports(a, b)
    by = {c["representative"]: c for c in diff["changes"]}
    assert by["raise min score"]["a"] == 2 and by["raise min score"]["b"] == 1
    assert by["widen stop"]["delta"] == 1


# ---- 기간별 비교 ----
def test_period_comparison():
    s = _store([
        rec("C1", "2026-07-07T04:10:00Z", rule=["raise min score"]),
        rec("C2", "2026-07-07T06:10:00Z", rule=["widen stop"]),
    ])
    agg = RuleImprovementAggregator(s)
    early = agg.aggregate(end=datetime(2026, 7, 7, 5, 0, tzinfo=timezone.utc))
    late = agg.aggregate(start=datetime(2026, 7, 7, 5, 0, tzinfo=timezone.utc))
    assert early.ok_reviews == 1 and early.rule_improvement_clusters[0].representative == "raise min score"
    assert late.ok_reviews == 1 and late.rule_improvement_clusters[0].representative == "widen stop"


# ---- 정규화 ----
def test_normalize_text():
    assert normalize_text("  Raise   MIN_score! ") == "raise min_score"
    assert normalize_text("Avoid overnight.") == normalize_text("avoid  OVERNIGHT")


# ---- JSON export ----
def test_export_json(tmp_path):
    s = _store([rec("C1", "2026-07-07T04:10:00Z", rule=["raise min score"], missed=["rsi"])])
    rep = RuleImprovementAggregator(s).aggregate()
    p = export_report_json(rep, tmp_path / "out" / "rule.json")
    d = json.load(open(p, encoding="utf-8"))
    assert d["total_reviews"] == 1
    assert d["rule_improvement_clusters"][0]["representative"] == "raise min score"
    assert "confidence" in d and "filters" in d
