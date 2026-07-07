"""SystemReviewEngine — 운영 품질을 GPT 로 회고(운영 회고 전용).

- 입력: RuntimeMetricsStore(read-only) + JournalStore 로 만든 window summary.
  Replay/TradingPipeline 미사용. 주문/Rule Engine/strategy 에 절대 영향 없음(제안만).
- AIProvider 인터페이스(OpenAIProvider/FakeReviewProvider)로 호출.
- 실패(timeout/rate limit/invalid JSON/schema mismatch) → SYSTEM_REVIEW_FAILED 안전 종료.
- 결정론: input_hash 는 window+prompt+model 로만 계산(created_at/latency 제외).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Callable

from src.ai.models import AIProviderError, AIRateLimitError, AITimeoutError
from src.system_review.models import (
    GRADES,
    GRADE_UNKNOWN,
    R_INVALID_JSON,
    R_PROVIDER_ERROR,
    R_RATE_LIMIT,
    R_SCHEMA_MISMATCH,
    R_TIMEOUT,
    SYSTEM_REVIEW_FAILED,
    SYSTEM_REVIEW_OK,
    SYSTEM_REVIEW_SCHEMA,
    SystemReviewLog,
    SystemReviewReport,
)
from src.system_review.window import build_window_summary

PROMPT_VERSION = "system_review_v1"

SYSTEM_PROMPT = """You are a SYSTEM OPERATIONS REVIEWER for an automated paper-trading system.
You evaluate OPERATIONAL QUALITY over a time window — NOT trading strategy, NOT P&L, NOT
whether individual entries were good (a separate reviewer handles that). You never place orders
and never change any rule or config; you only produce an operational retrospective.

You are given an aggregated operational summary of one window: runtime health, api_errors,
exceptions, sqlite_locks, average/max loop latency, candidate/order counts, AI approve/wait/reject
ratios, risk-block ratio, skipped cycles, degraded-health cycles, and data-quality signals.

Judge operational quality and respond with ONLY a JSON object matching:
grade: one of "A" | "B" | "C" | "D" | "F" (A = stable/healthy, F = severely degraded)
summary: short string
major_issues: array of short strings (operational problems: errors, latency, instability, data gaps)
recommendations: array of short strings (operational fixes only — infra/reliability/observability)
confidence: integer 0-100
No prose outside the JSON."""


@dataclass(frozen=True)
class SystemReviewConfig:
    prompt_version: str = PROMPT_VERSION
    temperature: float = 0.0
    timeout_s: float = 20.0


class SystemReviewEngine:
    def __init__(self, provider, metrics_store, journal_store,
                 config: SystemReviewConfig | None = None, log_store=None,
                 monotonic: Callable[[], float] = time.monotonic,
                 now_iso: Callable[[], str] | None = None):
        self.provider = provider
        self._metrics = metrics_store        # read-only 접근만
        self._journal = journal_store
        self.cfg = config or SystemReviewConfig()
        self.log_store = log_store
        self._monotonic = monotonic
        self._now_iso = now_iso or _utc_now_iso
        self.last_log: SystemReviewLog | None = None

    def review(self, *, start=None, end=None) -> SystemReviewReport:
        window = build_window_summary(self._metrics, self._journal, start=start, end=end)
        h = self._input_hash(window)
        user = ("Review the operational quality of this window. Return ONLY the JSON object.\n"
                + json.dumps(window, sort_keys=True, default=str, ensure_ascii=False))

        t0 = self._monotonic()
        try:
            raw = self.provider.complete(
                SYSTEM_PROMPT, user, temperature=self.cfg.temperature,
                timeout=self.cfg.timeout_s, response_schema=SYSTEM_REVIEW_SCHEMA)
        except AITimeoutError:
            return self._fail(window, h, t0, R_TIMEOUT)
        except AIRateLimitError:
            return self._fail(window, h, t0, R_RATE_LIMIT)
        except AIProviderError:
            return self._fail(window, h, t0, R_PROVIDER_ERROR)

        latency = self._ms(t0)
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return self._fail(window, h, t0, R_INVALID_JSON, raw=str(raw), latency=latency)
        try:
            self._validate(parsed)
        except ValueError:
            return self._fail(window, h, t0, R_SCHEMA_MISMATCH, raw=str(raw),
                              parsed=parsed if isinstance(parsed, dict) else {}, latency=latency)

        report = SystemReviewReport(
            status=SYSTEM_REVIEW_OK, grade=parsed["grade"], summary=parsed["summary"],
            major_issues=list(parsed["major_issues"]),
            recommendations=list(parsed["recommendations"]),
            confidence=int(parsed["confidence"]), model_name=self.provider.model_name,
            prompt_version=self.cfg.prompt_version, runtime_window=window)
        self._emit_log(window, h, latency, str(raw), parsed, report)
        return report

    # ---- helpers ----
    def _input_hash(self, window: dict) -> str:
        s = json.dumps({"window": window, "prompt_version": self.cfg.prompt_version,
                        "model_name": self.provider.model_name},
                       sort_keys=True, default=str)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    def _ms(self, t0) -> int:
        return int((self._monotonic() - t0) * 1000)

    def _fail(self, window, h, t0, reason, *, raw="", parsed=None, latency=None) -> SystemReviewReport:
        lat = latency if latency is not None else self._ms(t0)
        report = SystemReviewReport(
            status=SYSTEM_REVIEW_FAILED, grade=GRADE_UNKNOWN, summary="", major_issues=[],
            recommendations=[], confidence=0, model_name=self.provider.model_name,
            prompt_version=self.cfg.prompt_version, runtime_window=window, error=reason)
        self._emit_log(window, h, lat, raw, parsed or {}, report)
        return report

    def _validate(self, parsed) -> None:
        if not isinstance(parsed, dict):
            raise ValueError("not an object")
        for k in ("grade", "summary", "major_issues", "recommendations", "confidence"):
            if k not in parsed:
                raise ValueError(f"missing {k}")
        if parsed["grade"] not in GRADES:
            raise ValueError("bad grade")
        if not isinstance(parsed["summary"], str):
            raise ValueError("summary must be string")
        if not isinstance(parsed["major_issues"], list) or not isinstance(parsed["recommendations"], list):
            raise ValueError("arrays expected")
        c = parsed["confidence"]
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not (0 <= c <= 100):
            raise ValueError("bad confidence")

    def _emit_log(self, window, h, latency, raw, parsed, report: SystemReviewReport) -> None:
        w = window.get("window", {})
        log = SystemReviewLog(
            created_at=self._now_iso(), window_start=w.get("start"), window_end=w.get("end"),
            prompt_version=self.cfg.prompt_version, model_name=self.provider.model_name,
            input_hash=h, latency_ms=latency, runtime_window=window, status=report.status,
            grade=report.grade, confidence=report.confidence, raw_response=raw,
            parsed_response=parsed, error=report.error)
        self.last_log = log
        if self.log_store is not None:
            self.log_store.save(log)


def _utc_now_iso() -> str:
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
