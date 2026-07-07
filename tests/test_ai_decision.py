"""AIDecisionEngine 테스트 (실제 OpenAI 미호출 — FakeAIProvider 사용):
timeout, invalid JSON, schema mismatch, low confidence, approve, reject,
cache, dry_run, max_calls, RiskManager 독립성, 로그 필드."""
from __future__ import annotations

from datetime import date

from src.ai.engine import AIConfig, AIDecisionEngine, is_entry_permitted
from src.ai.models import (
    DEC_APPROVE,
    DEC_REJECT,
    DEC_WAIT,
    R_DRY_RUN,
    R_INVALID_JSON,
    R_LOW_CONFIDENCE,
    R_MAX_CALLS,
    R_RATE_LIMIT,
    R_SCHEMA_MISMATCH,
    R_TIMEOUT,
    AITimeoutError,
    AIRateLimitError,
    MarketContext,
)
from src.ai.providers import FakeAIProvider
from src.risk.models import RiskDecision

# ---- 공통 입력 ----
CANDIDATE = {"symbol": "SOXL", "score": 82.5, "candidate_type": "LONG_SETUP",
             "reasons": ["price above EMA20"], "risk_flags": ["LEVERAGED_ETF"]}
INDICATORS = {"close": 194.8, "ema7": 194.0, "ema20": 193.2, "rsi14": 58.0, "vwap": 193.9}
MARKET = MarketContext(session="REGULAR")


class _Snap:
    account_value = 100_000.0
    session = "REGULAR"
    is_eod_flatten_window = False
    consecutive_losses = 0
    daily_pnl_pct = 0.0
    drawdown_pct = 0.0
    total_effective_exposure = 0.0


PORTFOLIO = _Snap()
RISK_ALLOW = RiskDecision(True, [], 10.0, ["LEVERAGED_ETF"])
RISK_BLOCK = RiskDecision(False, ["MAX_EFFECTIVE_EXPOSURE_EXCEEDED"], 0.0, ["LEVERAGED_ETF"])


def approve_resp(conf=84):
    return {"decision": DEC_APPROVE, "confidence_score": conf, "entry_quality": "GOOD",
            "risk_level": "MEDIUM", "reason": ["trend recovered above EMA20", "VWAP reclaim"],
            "warnings": ["SOXL is a 3x leveraged ETF"]}


def engine(provider, **cfg_kw):
    return AIDecisionEngine(provider, AIConfig(**cfg_kw))


def decide(eng):
    return eng.decide(CANDIDATE, INDICATORS, PORTFOLIO, RISK_ALLOW, MARKET)


# ---- approve ----
def test_approve_entry():
    eng = engine(FakeAIProvider(response=approve_resp(84)))
    d = decide(eng)
    assert d.decision == DEC_APPROVE
    assert d.confidence_score == 84
    assert d.entry_quality == "GOOD" and d.risk_level == "MEDIUM"
    assert d.model_name == "fake-model" and d.prompt_version == "entry_filter_v1"
    # 출력 예시 구조
    assert set(d.to_dict()) == {"decision", "confidence_score", "entry_quality",
                                "risk_level", "reason", "warnings", "model_name", "prompt_version"}


# ---- reject ----
def test_reject_entry():
    resp = approve_resp()
    resp["decision"] = DEC_REJECT
    d = decide(engine(FakeAIProvider(response=resp)))
    assert d.decision == DEC_REJECT


# ---- low confidence → WAIT ----
def test_low_confidence_becomes_wait():
    d = decide(engine(FakeAIProvider(response=approve_resp(conf=40)), min_confidence=60))
    assert d.decision == DEC_WAIT
    assert R_LOW_CONFIDENCE in d.reason
    assert d.confidence_score == 40   # 투명성 위해 원 신뢰도 유지


# ---- timeout → WAIT ----
def test_timeout_fallback_wait():
    d = decide(engine(FakeAIProvider(raise_exc=AITimeoutError("t"))))
    assert d.decision == DEC_WAIT and R_TIMEOUT in d.reason


# ---- rate limit → WAIT ----
def test_rate_limit_fallback_wait():
    d = decide(engine(FakeAIProvider(raise_exc=AIRateLimitError("r"))))
    assert d.decision == DEC_WAIT and R_RATE_LIMIT in d.reason


# ---- invalid JSON → WAIT ----
def test_invalid_json_fallback_wait():
    d = decide(engine(FakeAIProvider(raw="not json {{{")))
    assert d.decision == DEC_WAIT and R_INVALID_JSON in d.reason


# ---- schema mismatch → WAIT ----
def test_schema_mismatch_fallback_wait():
    bad = {"decision": "MAYBE", "confidence_score": 200}   # enum/필드 위반
    d = decide(engine(FakeAIProvider(response=bad)))
    assert d.decision == DEC_WAIT and R_SCHEMA_MISMATCH in d.reason


# ---- RiskManager 독립성: AI APPROVE 여도 risk block 이면 진입 불가 ----
def test_ai_approve_but_risk_block_not_permitted():
    d = engine(FakeAIProvider(response=approve_resp(90))).decide(
        CANDIDATE, INDICATORS, PORTFOLIO, RISK_BLOCK, MARKET)
    assert d.decision == DEC_APPROVE                     # AI 의견은 APPROVE
    assert is_entry_permitted(d, RISK_BLOCK) is False    # 그러나 최종 진입 불가
    assert is_entry_permitted(d, RISK_ALLOW) is True


# ---- 캐시: 동일 input 은 provider 재호출 없이 재사용 ----
def test_cache_reuses_result():
    provider = FakeAIProvider(response=approve_resp(84))
    eng = engine(provider, use_cache=True)
    d1 = decide(eng)
    d2 = decide(eng)
    assert provider.calls == 1           # 두 번째는 캐시
    assert d1.to_dict() == d2.to_dict()
    assert eng.last_log.cached is True


# ---- dry_run: 호출 없이 WAIT ----
def test_dry_run_no_call():
    provider = FakeAIProvider(response=approve_resp())
    d = decide(engine(provider, dry_run=True))
    assert d.decision == DEC_WAIT and provider.calls == 0
    assert R_DRY_RUN in d.reason


# ---- max_calls_per_day: 한도 초과 시 WAIT ----
def test_max_calls_per_day():
    provider = FakeAIProvider(response=approve_resp())
    # 캐시 끄고 매번 다른 입력으로 호출 카운트 유발
    eng = AIDecisionEngine(provider, AIConfig(use_cache=False, max_calls_per_day=1),
                           today_fn=lambda: date(2026, 7, 6))
    d1 = eng.decide({**CANDIDATE, "score": 1}, INDICATORS, PORTFOLIO, RISK_ALLOW, MARKET)
    d2 = eng.decide({**CANDIDATE, "score": 2}, INDICATORS, PORTFOLIO, RISK_ALLOW, MARKET)
    assert d1.decision == DEC_APPROVE
    assert d2.decision == DEC_WAIT and R_MAX_CALLS in d2.reason
    assert provider.calls == 1


# ---- ai_decision_log 필드 기록 ----
def test_decision_log_fields():
    eng = engine(FakeAIProvider(response=approve_resp(84)))
    decide(eng)
    log = eng.last_log.to_dict()
    for key in ("prompt_version", "model_name", "input_hash", "latency_ms",
                "raw_response", "parsed_response", "decision", "confidence_score"):
        assert key in log
    assert len(log["input_hash"]) == 64          # sha256 hex
    assert log["decision"] == DEC_APPROVE


# ---- 동일 입력은 동일 input_hash (캐시/재현성 근거) ----
def test_input_hash_stable():
    eng = engine(FakeAIProvider(response=approve_resp()))
    h1 = eng._input_hash(CANDIDATE, INDICATORS, PORTFOLIO, RISK_ALLOW, MARKET)
    h2 = eng._input_hash(CANDIDATE, INDICATORS, PORTFOLIO, RISK_ALLOW, MARKET)
    assert h1 == h2


# ---- log_store 로 저장 가능 ----
def test_log_store_receives_log():
    saved = []

    class Store:
        def save(self, log):
            saved.append(log)

    eng = AIDecisionEngine(FakeAIProvider(response=approve_resp()), AIConfig(), log_store=Store())
    decide(eng)
    assert len(saved) == 1 and saved[0].decision == DEC_APPROVE
