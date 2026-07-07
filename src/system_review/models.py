"""System Review Engine 모델 + 응답 스키마.

운영 회고 결과(SystemReviewReport)와 감사 로그(SystemReviewLog).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---- 상태 ----
SYSTEM_REVIEW_OK = "SYSTEM_REVIEW_OK"
SYSTEM_REVIEW_FAILED = "SYSTEM_REVIEW_FAILED"

# ---- 실패 사유 ----
R_TIMEOUT = "SYSTEM_REVIEW_TIMEOUT"
R_RATE_LIMIT = "SYSTEM_REVIEW_RATE_LIMIT"
R_PROVIDER_ERROR = "SYSTEM_REVIEW_PROVIDER_ERROR"
R_INVALID_JSON = "SYSTEM_REVIEW_INVALID_JSON"
R_SCHEMA_MISMATCH = "SYSTEM_REVIEW_SCHEMA_MISMATCH"

# ---- 운영 품질 등급 ----
GRADES = frozenset({"A", "B", "C", "D", "F"})
GRADE_UNKNOWN = "UNKNOWN"

# ---- 응답 JSON schema (강제) ----
SYSTEM_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "grade": {"type": "string", "enum": sorted(GRADES)},
        "summary": {"type": "string"},
        "major_issues": {"type": "array", "items": {"type": "string"}},
        "recommendations": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
    },
    "required": ["grade", "summary", "major_issues", "recommendations", "confidence"],
    "additionalProperties": False,
}


@dataclass
class SystemReviewReport:
    status: str                          # SYSTEM_REVIEW_OK / SYSTEM_REVIEW_FAILED
    grade: str                           # A/B/C/D/F / UNKNOWN
    summary: str
    major_issues: list[str]
    recommendations: list[str]
    confidence: int
    model_name: str
    prompt_version: str
    runtime_window: dict                 # {window, runtime, journal, data_quality} 요약 스냅샷
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "status": self.status, "grade": self.grade, "summary": self.summary,
            "major_issues": self.major_issues, "recommendations": self.recommendations,
            "confidence": self.confidence, "model_name": self.model_name,
            "prompt_version": self.prompt_version, "runtime_window": self.runtime_window,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SystemReviewReport":
        return cls(
            status=d["status"], grade=d.get("grade", GRADE_UNKNOWN),
            summary=d.get("summary", ""), major_issues=list(d.get("major_issues", [])),
            recommendations=list(d.get("recommendations", [])),
            confidence=int(d.get("confidence", 0)), model_name=d.get("model_name", ""),
            prompt_version=d.get("prompt_version", ""),
            runtime_window=d.get("runtime_window", {}), error=d.get("error"))


@dataclass
class SystemReviewLog:
    """감사 로그 — 재현성/추적용. runtime_window/input_hash/latency 등 모두 저장."""
    created_at: str | None
    window_start: str | None
    window_end: str | None
    prompt_version: str
    model_name: str
    input_hash: str
    latency_ms: int
    runtime_window: dict
    status: str
    grade: str
    confidence: int
    raw_response: str
    parsed_response: dict
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "created_at": self.created_at, "window_start": self.window_start,
            "window_end": self.window_end, "prompt_version": self.prompt_version,
            "model_name": self.model_name, "input_hash": self.input_hash,
            "latency_ms": self.latency_ms, "runtime_window": self.runtime_window,
            "status": self.status, "grade": self.grade, "confidence": self.confidence,
            "raw_response": self.raw_response, "parsed_response": self.parsed_response,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SystemReviewLog":
        return cls(
            created_at=d.get("created_at"), window_start=d.get("window_start"),
            window_end=d.get("window_end"), prompt_version=d.get("prompt_version", ""),
            model_name=d.get("model_name", ""), input_hash=d.get("input_hash", ""),
            latency_ms=int(d.get("latency_ms", 0)), runtime_window=d.get("runtime_window", {}),
            status=d.get("status", SYSTEM_REVIEW_FAILED), grade=d.get("grade", GRADE_UNKNOWN),
            confidence=int(d.get("confidence", 0)), raw_response=d.get("raw_response", ""),
            parsed_response=d.get("parsed_response", {}), error=d.get("error"))
