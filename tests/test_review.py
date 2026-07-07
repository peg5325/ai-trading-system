"""AIReviewEngine 테스트 (실제 OpenAI 미호출 — FakeReviewProvider):
정상 리뷰, 실패 응답(invalid JSON/timeout/rate limit/schema), JSON roundtrip,
로그 저장 구조, input_hash 재현성, ReplayContext read-only."""
from __future__ import annotations

import json

from src.ai.models import AIRateLimitError, AITimeoutError
from src.replay.models import ReplayContext
from src.review.engine import AIReviewEngine, ReviewConfig
from src.review.models import (
    QUALITY_UNKNOWN,
    R_INVALID_JSON,
    R_RATE_LIMIT,
    R_SCHEMA_MISMATCH,
    R_TIMEOUT,
    REVIEW_FAILED,
    REVIEW_OK,
    AIReviewReport,
)
from src.review.providers import FakeReviewProvider


def make_ctx():
    return ReplayContext(
        cycle_id="20260706T150000Z", cycle_ts="2026-07-06T15:00:00Z", session="REGULAR",
        status="RAN", universe=["SOXL"],
        market_snapshot={"session": "REGULAR", "last_prices": {"SOXL": 194.8}},
        portfolio={"account_value": 100_000, "positions": {}},
        symbols=[{"symbol": "SOXL", "action": "SKIPPED", "reason": "AI_WAIT",
                  "indicator_snapshot": {"close": 194.8, "rsi14": 33},
                  "candidate": {"symbol": "SOXL", "score": 81},
                  "ai_decision": {"decision": "WAIT"},
                  "risk_decision": {"allowed": True}, "candles": []}],
        journal={"cycle_id": "20260706T150000Z"})


def good_response():
    return {"decision_quality": "GOOD", "missed_signals": ["volume spike ignored"],
            "risk_assessment": "leverage exposure acceptable",
            "alternative_action": "wait for VWAP reclaim",
            "rule_improvement": ["add volume filter", "tighten RSI threshold"],
            "confidence_reassessment": 70}


def engine(provider):
    return AIReviewEngine(provider, ReviewConfig())


# ---- 정상 리뷰 ----
def test_normal_review():
    eng = engine(FakeReviewProvider(response=good_response(), model_name="gpt-5.5"))
    rep = eng.review(make_ctx())
    assert rep.status == REVIEW_OK
    assert rep.cycle_id == "20260706T150000Z"
    assert rep.decision_quality == "GOOD"
    assert rep.missed_signals == ["volume spike ignored"]
    assert rep.confidence_reassessment == 70
    assert rep.rule_improvement == ["add volume filter", "tighten RSI threshold"]
    assert rep.model_name == "gpt-5.5" and rep.prompt_version == "review_v1"
    # 6개 분석 항목 모두 존재
    d = rep.to_dict()
    for k in ("decision_quality", "missed_signals", "risk_assessment",
              "alternative_action", "rule_improvement", "confidence_reassessment"):
        assert k in d


# ---- 실패: timeout ----
def test_timeout_review_failed():
    rep = engine(FakeReviewProvider(raise_exc=AITimeoutError("t"))).review(make_ctx())
    assert rep.status == REVIEW_FAILED and rep.error == R_TIMEOUT
    assert rep.decision_quality == QUALITY_UNKNOWN


# ---- 실패: rate limit ----
def test_rate_limit_review_failed():
    rep = engine(FakeReviewProvider(raise_exc=AIRateLimitError("r"))).review(make_ctx())
    assert rep.status == REVIEW_FAILED and rep.error == R_RATE_LIMIT


# ---- 실패: invalid JSON ----
def test_invalid_json_review_failed():
    rep = engine(FakeReviewProvider(raw="not json {{{")).review(make_ctx())
    assert rep.status == REVIEW_FAILED and rep.error == R_INVALID_JSON


# ---- 실패: schema mismatch ----
def test_schema_mismatch_review_failed():
    bad = {"decision_quality": "AMAZING", "confidence_reassessment": 999}
    rep = engine(FakeReviewProvider(response=bad)).review(make_ctx())
    assert rep.status == REVIEW_FAILED and rep.error == R_SCHEMA_MISMATCH


# ---- JSON roundtrip ----
def test_json_roundtrip():
    rep = engine(FakeReviewProvider(response=good_response())).review(make_ctx())
    s = json.dumps(rep.to_dict())
    back = AIReviewReport.from_dict(json.loads(s))
    assert back.to_dict() == rep.to_dict()


# ---- 로그 저장 구조 ----
def test_review_log_fields_and_store():
    saved = []

    class Store:
        def save(self, log):
            saved.append(log)

    eng = AIReviewEngine(FakeReviewProvider(response=good_response()), ReviewConfig(), log_store=Store())
    eng.review(make_ctx())
    log = eng.last_log.to_dict()
    for k in ("cycle_id", "prompt_version", "model_name", "input_hash", "latency_ms",
              "raw_response", "parsed_response", "status"):
        assert k in log
    assert len(log["input_hash"]) == 64
    assert log["status"] == REVIEW_OK
    assert len(saved) == 1


# ---- input_hash 재현성 ----
def test_input_hash_reproducible():
    eng = engine(FakeReviewProvider(response=good_response()))
    ctx = make_ctx()
    eng.review(ctx)
    h1 = eng.last_log.input_hash
    eng.review(ctx)
    h2 = eng.last_log.input_hash
    assert h1 == h2                       # 동일 ReplayContext → 동일 input_hash


# ---- ReplayContext read-only ----
def test_replay_context_not_mutated():
    ctx = make_ctx()
    before = json.dumps(ctx.to_dict(), sort_keys=True)
    engine(FakeReviewProvider(response=good_response())).review(ctx)
    assert json.dumps(ctx.to_dict(), sort_keys=True) == before


# ---- 주문/파이프라인 접근 없음 ----
def test_no_order_or_pipeline_access():
    eng = engine(FakeReviewProvider(response=good_response()))
    assert not hasattr(eng, "submit")
    assert not hasattr(eng, "run_once")
    assert not hasattr(eng, "place_order")
