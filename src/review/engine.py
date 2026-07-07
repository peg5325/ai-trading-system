"""AIReviewEngine — ReplayContext 를 GPT 로 사후 회고.

- 입력은 ReplayContext 만. 주문/브로커/파이프라인 미접근.
- AIProvider 인터페이스(OpenAIProvider/FakeReviewProvider)로 GPT 호출.
- 실패(timeout/rate limit/invalid JSON/schema mismatch) → REVIEW_FAILED.
- ReplayContext 는 read-only (to_prompt_dict 로 새 dict 만 읽음).
- 매매 판단/주문 없음. 회고와 개선 제안만.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Callable

from src.ai.models import AIProviderError, AIRateLimitError, AITimeoutError
from src.review.models import (
    QUALITIES,
    QUALITY_UNKNOWN,
    R_INVALID_JSON,
    R_PROVIDER_ERROR,
    R_RATE_LIMIT,
    R_SCHEMA_MISMATCH,
    R_TIMEOUT,
    REVIEW_FAILED,
    REVIEW_OK,
    REVIEW_SCHEMA,
    AIReviewLog,
    AIReviewReport,
)

PROMPT_VERSION = "review_v1"

SYSTEM_PROMPT = """You are a POST-TRADE REVIEWER for an automated trading system.
You evaluate a PAST decision cycle after the fact. You do NOT place orders, and you do NOT
make real-time trading decisions — you only produce a retrospective analysis to improve strategy.
You are given a full replay of one cycle (market snapshot, indicators, candidate, the AI entry
decision, the risk decision, any order, portfolio, and journal).

Judge whether the decision at this cycle was appropriate. Respond with ONLY a JSON object matching:
decision_quality: one of "EXCELLENT" | "GOOD" | "ACCEPTABLE" | "POOR" | "MISTAKE"
missed_signals: array of short strings (signals that were overlooked)
risk_assessment: short string (was risk handled well?)
alternative_action: short string (what could have been done instead)
rule_improvement: array of short strings (concrete rule/threshold improvement suggestions)
confidence_reassessment: integer 0-100 (what the entry confidence should have been in hindsight)
No prose outside the JSON."""


@dataclass(frozen=True)
class ReviewConfig:
    prompt_version: str = PROMPT_VERSION
    temperature: float = 0.0
    timeout_s: float = 15.0


class AIReviewEngine:
    def __init__(self, provider, config: ReviewConfig | None = None,
                 log_store=None, monotonic: Callable[[], float] = time.monotonic):
        self.provider = provider
        self.cfg = config or ReviewConfig()
        self.log_store = log_store
        self._monotonic = monotonic
        self.last_log: AIReviewLog | None = None

    def review(self, context) -> AIReviewReport:
        payload = context.to_prompt_dict()          # read-only: 새 dict
        cycle_id = getattr(context, "cycle_id", None)
        h = self._input_hash(payload)
        user = ("Review this past decision cycle. Return ONLY the JSON object.\n"
                + json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False))

        start = self._monotonic()
        try:
            raw = self.provider.complete(
                SYSTEM_PROMPT, user, temperature=self.cfg.temperature,
                timeout=self.cfg.timeout_s, response_schema=REVIEW_SCHEMA)
        except AITimeoutError:
            return self._fail(cycle_id, h, start, R_TIMEOUT)
        except AIRateLimitError:
            return self._fail(cycle_id, h, start, R_RATE_LIMIT)
        except AIProviderError:
            return self._fail(cycle_id, h, start, R_PROVIDER_ERROR)

        latency = self._ms(start)
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return self._fail(cycle_id, h, start, R_INVALID_JSON, raw=str(raw), latency=latency)
        try:
            self._validate(parsed)
        except ValueError:
            return self._fail(cycle_id, h, start, R_SCHEMA_MISMATCH,
                              raw=str(raw), parsed=parsed if isinstance(parsed, dict) else {},
                              latency=latency)

        report = AIReviewReport(
            status=REVIEW_OK, cycle_id=cycle_id,
            decision_quality=parsed["decision_quality"],
            missed_signals=list(parsed["missed_signals"]),
            risk_assessment=parsed["risk_assessment"],
            alternative_action=parsed["alternative_action"],
            rule_improvement=list(parsed["rule_improvement"]),
            confidence_reassessment=int(parsed["confidence_reassessment"]),
            model_name=self.provider.model_name, prompt_version=self.cfg.prompt_version)
        self._emit_log(cycle_id, h, latency, str(raw), parsed, REVIEW_OK)
        return report

    # ---- helpers ----
    def _input_hash(self, payload: dict) -> str:
        s = json.dumps({"payload": payload, "prompt_version": self.cfg.prompt_version,
                        "model_name": self.provider.model_name},
                       sort_keys=True, default=str)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    def _ms(self, start) -> int:
        return int((self._monotonic() - start) * 1000)

    def _fail(self, cycle_id, h, start, reason, *, raw="", parsed=None, latency=None) -> AIReviewReport:
        lat = latency if latency is not None else self._ms(start)
        report = AIReviewReport(
            status=REVIEW_FAILED, cycle_id=cycle_id, decision_quality=QUALITY_UNKNOWN,
            missed_signals=[], risk_assessment="", alternative_action="",
            rule_improvement=[], confidence_reassessment=0,
            model_name=self.provider.model_name, prompt_version=self.cfg.prompt_version,
            error=reason)
        self._emit_log(cycle_id, h, lat, raw, parsed or {}, REVIEW_FAILED, error=reason)
        return report

    def _validate(self, parsed) -> None:
        if not isinstance(parsed, dict):
            raise ValueError("not an object")
        for k in ("decision_quality", "missed_signals", "risk_assessment",
                  "alternative_action", "rule_improvement", "confidence_reassessment"):
            if k not in parsed:
                raise ValueError(f"missing {k}")
        if parsed["decision_quality"] not in QUALITIES:
            raise ValueError("bad decision_quality")
        if not isinstance(parsed["missed_signals"], list) or not isinstance(parsed["rule_improvement"], list):
            raise ValueError("arrays expected")
        if not isinstance(parsed["risk_assessment"], str) or not isinstance(parsed["alternative_action"], str):
            raise ValueError("strings expected")
        cs = parsed["confidence_reassessment"]
        if isinstance(cs, bool) or not isinstance(cs, (int, float)) or not (0 <= cs <= 100):
            raise ValueError("bad confidence_reassessment")

    def _emit_log(self, cycle_id, h, latency, raw, parsed, status, error=None) -> None:
        log = AIReviewLog(
            cycle_id=cycle_id, prompt_version=self.cfg.prompt_version,
            model_name=self.provider.model_name, input_hash=h, latency_ms=latency,
            raw_response=raw, parsed_response=parsed, status=status, error=error)
        self.last_log = log
        if self.log_store is not None:
            self.log_store.save(log)
