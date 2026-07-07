"""AIDecisionEngine 데이터 모델 + 인터페이스 + 예외.

핵심 원칙:
- AI 는 entry filter 다. 주문을 실행하지 않는다.
- AI 실패는 매매 금지(WAIT)다.
- 청산/손절/EOD flatten 은 AI 와 완전히 독립이다(여기서 다루지 않는다).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# ---- decision / quality / risk 상수 ----
DEC_APPROVE = "APPROVE_ENTRY"
DEC_REJECT = "REJECT_ENTRY"
DEC_WAIT = "WAIT"
DECISIONS = frozenset({DEC_APPROVE, DEC_REJECT, DEC_WAIT})

QUALITIES = frozenset({"POOR", "FAIR", "GOOD", "EXCELLENT"})
QUALITY_UNKNOWN = "UNKNOWN"
RISK_LEVELS = frozenset({"LOW", "MEDIUM", "HIGH"})
RISK_UNKNOWN = "UNKNOWN"

# ---- fallback reason codes (WAIT 사유) ----
R_TIMEOUT = "AI_TIMEOUT"
R_RATE_LIMIT = "AI_RATE_LIMIT"
R_PROVIDER_ERROR = "AI_PROVIDER_ERROR"
R_INVALID_JSON = "AI_INVALID_JSON"
R_SCHEMA_MISMATCH = "AI_SCHEMA_MISMATCH"
R_LOW_CONFIDENCE = "AI_LOW_CONFIDENCE"
R_DRY_RUN = "AI_DRY_RUN"
R_MAX_CALLS = "AI_MAX_CALLS_EXCEEDED"


# ---- 응답 JSON schema (강제) ----
ENTRY_FILTER_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": [DEC_APPROVE, DEC_REJECT, DEC_WAIT]},
        "confidence_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "entry_quality": {"type": "string", "enum": sorted(QUALITIES)},
        "risk_level": {"type": "string", "enum": sorted(RISK_LEVELS)},
        "reason": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "confidence_score", "entry_quality", "risk_level", "reason", "warnings"],
    "additionalProperties": False,
}


# ---- 예외 (Provider 가 발생시킴) ----
class AIProviderError(Exception):
    """일반 provider 오류."""


class AITimeoutError(AIProviderError):
    """호출 타임아웃."""


class AIRateLimitError(AIProviderError):
    """rate limit."""


# ---- 입력 컨텍스트 ----
@dataclass
class MarketContext:
    session: str = "REGULAR"
    is_eod_flatten_window: bool = False
    note: str = ""              # 뉴스/매크로 등 비정형 컨텍스트(옵션)

    def to_dict(self) -> dict:
        return {"session": self.session,
                "is_eod_flatten_window": self.is_eod_flatten_window,
                "note": self.note}


# ---- 출력 ----
@dataclass
class AIEntryDecision:
    decision: str               # APPROVE_ENTRY / REJECT_ENTRY / WAIT
    confidence_score: int       # 0~100
    entry_quality: str
    risk_level: str
    reason: list[str]
    warnings: list[str]
    model_name: str
    prompt_version: str

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "confidence_score": self.confidence_score,
            "entry_quality": self.entry_quality,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "warnings": self.warnings,
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
        }


# ---- ai_decision_log 저장 구조 ----
@dataclass
class AIDecisionLog:
    prompt_version: str
    model_name: str
    input_hash: str
    latency_ms: int
    raw_response: str
    parsed_response: dict
    decision: str
    confidence_score: int
    cached: bool = False
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "prompt_version": self.prompt_version,
            "model_name": self.model_name,
            "input_hash": self.input_hash,
            "latency_ms": self.latency_ms,
            "raw_response": self.raw_response,
            "parsed_response": self.parsed_response,
            "decision": self.decision,
            "confidence_score": self.confidence_score,
            "cached": self.cached,
            "error": self.error,
        }


# ---- 인터페이스 (Protocol) ----
class AIProvider(Protocol):
    model_name: str

    def complete(self, system: str, user: str, *, temperature: float,
                 timeout: float, response_schema: dict) -> str:
        """raw 텍스트(JSON 예상)를 반환. 실패 시 AITimeout/RateLimit/ProviderError 발생."""
        ...


class AICache(Protocol):
    def get(self, input_hash: str) -> AIEntryDecision | None: ...
    def set(self, input_hash: str, decision: AIEntryDecision) -> None: ...


class AIDecisionLogStore(Protocol):
    def save(self, log: AIDecisionLog) -> None: ...


# ---- 기본 인메모리 캐시 ----
class InMemoryAICache:
    def __init__(self) -> None:
        self._d: dict[str, AIEntryDecision] = {}

    def get(self, input_hash: str) -> AIEntryDecision | None:
        return self._d.get(input_hash)

    def set(self, input_hash: str, decision: AIEntryDecision) -> None:
        self._d[input_hash] = decision
