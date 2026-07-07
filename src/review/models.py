"""AI Review Engine 모델 + 인터페이스.

Review Engine 은 과거를 평가한다. 실시간 주문에는 관여하지 않는다.
목적: 전략 개선의 근거 데이터를 만든다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# ---- 상태 ----
REVIEW_OK = "REVIEW_OK"
REVIEW_FAILED = "REVIEW_FAILED"

# ---- 실패 사유 ----
R_TIMEOUT = "REVIEW_TIMEOUT"
R_RATE_LIMIT = "REVIEW_RATE_LIMIT"
R_PROVIDER_ERROR = "REVIEW_PROVIDER_ERROR"
R_INVALID_JSON = "REVIEW_INVALID_JSON"
R_SCHEMA_MISMATCH = "REVIEW_SCHEMA_MISMATCH"

QUALITIES = frozenset({"EXCELLENT", "GOOD", "ACCEPTABLE", "POOR", "MISTAKE"})
QUALITY_UNKNOWN = "UNKNOWN"

# ---- 응답 JSON schema (강제) ----
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "decision_quality": {"type": "string", "enum": sorted(QUALITIES)},
        "missed_signals": {"type": "array", "items": {"type": "string"}},
        "risk_assessment": {"type": "string"},
        "alternative_action": {"type": "string"},
        "rule_improvement": {"type": "array", "items": {"type": "string"}},
        "confidence_reassessment": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": ["decision_quality", "missed_signals", "risk_assessment",
                 "alternative_action", "rule_improvement", "confidence_reassessment"],
    "additionalProperties": False,
}


@dataclass
class AIReviewReport:
    status: str                  # REVIEW_OK / REVIEW_FAILED
    cycle_id: str | None
    decision_quality: str
    missed_signals: list[str]
    risk_assessment: str
    alternative_action: str
    rule_improvement: list[str]
    confidence_reassessment: int
    model_name: str
    prompt_version: str
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status, "cycle_id": self.cycle_id,
            "decision_quality": self.decision_quality, "missed_signals": self.missed_signals,
            "risk_assessment": self.risk_assessment, "alternative_action": self.alternative_action,
            "rule_improvement": self.rule_improvement,
            "confidence_reassessment": self.confidence_reassessment,
            "model_name": self.model_name, "prompt_version": self.prompt_version,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AIReviewReport":
        return cls(
            status=d["status"], cycle_id=d.get("cycle_id"),
            decision_quality=d.get("decision_quality", QUALITY_UNKNOWN),
            missed_signals=list(d.get("missed_signals", [])),
            risk_assessment=d.get("risk_assessment", ""),
            alternative_action=d.get("alternative_action", ""),
            rule_improvement=list(d.get("rule_improvement", [])),
            confidence_reassessment=int(d.get("confidence_reassessment", 0)),
            model_name=d.get("model_name", ""), prompt_version=d.get("prompt_version", ""),
            error=d.get("error"))


@dataclass
class AIReviewLog:
    cycle_id: str | None
    prompt_version: str
    model_name: str
    input_hash: str
    latency_ms: int
    raw_response: str
    parsed_response: dict
    status: str
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "cycle_id": self.cycle_id, "prompt_version": self.prompt_version,
            "model_name": self.model_name, "input_hash": self.input_hash,
            "latency_ms": self.latency_ms, "raw_response": self.raw_response,
            "parsed_response": self.parsed_response, "status": self.status, "error": self.error,
        }


class AIReviewLogStore(Protocol):
    def save(self, log: AIReviewLog) -> None: ...
