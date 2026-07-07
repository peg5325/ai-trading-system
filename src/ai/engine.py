"""AIDecisionEngine — Rule/Screener 진입 후보에 대한 AI entry filter.

- Broker/OrderManager 미접근. 입력: Candidate, Indicator snapshot, Portfolio snapshot,
  RiskDecision, MarketContext. 출력: AIEntryDecision.
- OpenAI 실패(timeout/rate limit/invalid JSON/schema mismatch) → WAIT 폴백.
- temperature=0(기본), JSON schema 강제, 캐시/호출제한/dry_run 비용 보호.
- AI 가 APPROVE 해도 RiskManager 가 block 이면 주문 불가(is_entry_permitted).
- 청산/손절/EOD flatten 은 여기서 절대 판단하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date
from typing import Callable

from src.ai.models import (
    DEC_APPROVE,
    DEC_WAIT,
    DECISIONS,
    ENTRY_FILTER_SCHEMA,
    QUALITIES,
    QUALITY_UNKNOWN,
    R_DRY_RUN,
    R_INVALID_JSON,
    R_LOW_CONFIDENCE,
    R_MAX_CALLS,
    R_PROVIDER_ERROR,
    R_RATE_LIMIT,
    R_SCHEMA_MISMATCH,
    R_TIMEOUT,
    RISK_LEVELS,
    RISK_UNKNOWN,
    AICache,
    AIDecisionLog,
    AIDecisionLogStore,
    AIEntryDecision,
    AIProvider,
    AIProviderError,
    AIRateLimitError,
    AITimeoutError,
    InMemoryAICache,
    MarketContext,
)

PROMPT_VERSION = "entry_filter_v1"

SYSTEM_PROMPT = """You are an ENTRY FILTER for an automated trading system. You are NOT an order executor.
Your ONLY job is to judge whether a NEW long entry candidate is worth taking, and return a
confidence score with reasons. You do NOT decide exits, stop-loss, take-profit, or EOD flatten —
those are handled by deterministic code and are completely independent of you.
Even if you APPROVE, a separate RiskManager may still block the order; your approval is advisory only.
The instrument may be a leveraged ETF (e.g., 3x); reflect that in risk_level and warnings.

Respond with ONLY a JSON object, no prose, matching this schema:
decision: one of "APPROVE_ENTRY" | "REJECT_ENTRY" | "WAIT"
confidence_score: integer 0-100
entry_quality: one of "POOR" | "FAIR" | "GOOD" | "EXCELLENT"
risk_level: one of "LOW" | "MEDIUM" | "HIGH"
reason: array of short strings
warnings: array of short strings
If information is insufficient or conflicting, prefer WAIT."""


@dataclass(frozen=True)
class AIConfig:
    prompt_version: str = PROMPT_VERSION
    temperature: float = 0.0
    timeout_s: float = 8.0
    min_confidence: int = 60        # APPROVE 최소 신뢰도(미만이면 WAIT)
    use_cache: bool = True
    dry_run: bool = False
    max_calls_per_day: int | None = None


class AIDecisionEngine:
    def __init__(self, provider: AIProvider, config: AIConfig | None = None,
                 cache: AICache | None = None,
                 log_store: AIDecisionLogStore | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 today_fn: Callable[[], date] = date.today):
        self.provider = provider
        self.cfg = config or AIConfig()
        self.cache = cache if cache is not None else (InMemoryAICache() if self.cfg.use_cache else None)
        self.log_store = log_store
        self._monotonic = monotonic
        self._today_fn = today_fn
        self._day: date | None = None
        self._calls = 0
        self.last_log: AIDecisionLog | None = None

    # ---- public ----
    def decide(self, candidate, indicators: dict, portfolio, risk_decision,
               market: MarketContext) -> AIEntryDecision:
        h = self._input_hash(candidate, indicators, portfolio, risk_decision, market)

        # 1) 캐시 히트 (비용 없음)
        if self.cache is not None:
            cached = self.cache.get(h)
            if cached is not None:
                self._emit_log(h, 0, "", cached.to_dict(), cached, cached_flag=True)
                return cached

        # 2) dry_run — 호출 없이 WAIT
        if self.cfg.dry_run:
            d = self._wait(R_DRY_RUN)
            self._emit_log(h, 0, "", {}, d, error=R_DRY_RUN)
            return d

        # 3) 일일 호출 한도
        if not self._can_call():
            d = self._wait(R_MAX_CALLS)
            self._emit_log(h, 0, "", {}, d, error=R_MAX_CALLS)
            return d

        user = self._user_prompt(candidate, indicators, portfolio, risk_decision, market)
        start = self._monotonic()

        # 4) provider 호출 — 실패는 WAIT
        try:
            raw = self.provider.complete(
                SYSTEM_PROMPT, user, temperature=self.cfg.temperature,
                timeout=self.cfg.timeout_s, response_schema=ENTRY_FILTER_SCHEMA)
            self._calls += 1
        except AITimeoutError:
            return self._fail(h, start, R_TIMEOUT)
        except AIRateLimitError:
            return self._fail(h, start, R_RATE_LIMIT)
        except AIProviderError:
            return self._fail(h, start, R_PROVIDER_ERROR)

        latency = self._ms(start)

        # 5) 파싱/검증 — 실패는 WAIT
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            d = self._wait(R_INVALID_JSON)
            self._emit_log(h, latency, str(raw), {}, d, error=R_INVALID_JSON)
            return d
        try:
            self._validate(parsed)
        except ValueError:
            d = self._wait(R_SCHEMA_MISMATCH)
            self._emit_log(h, latency, str(raw), parsed if isinstance(parsed, dict) else {},
                           d, error=R_SCHEMA_MISMATCH)
            return d

        # 6) 결정 구성 (+ low confidence → WAIT)
        decision = self._build_decision(parsed)
        if decision.decision == DEC_APPROVE and decision.confidence_score < self.cfg.min_confidence:
            decision = AIEntryDecision(
                DEC_WAIT, decision.confidence_score, decision.entry_quality,
                decision.risk_level, [R_LOW_CONFIDENCE] + decision.reason,
                decision.warnings, self.provider.model_name, self.cfg.prompt_version)

        if self.cache is not None:
            self.cache.set(h, decision)
        self._emit_log(h, latency, str(raw), parsed, decision)
        return decision

    # ---- helpers ----
    def _can_call(self) -> bool:
        today = self._today_fn()
        if self._day != today:
            self._day, self._calls = today, 0
        if self.cfg.max_calls_per_day is not None and self._calls >= self.cfg.max_calls_per_day:
            return False
        return True

    def _fail(self, h, start, reason) -> AIEntryDecision:
        d = self._wait(reason)
        self._emit_log(h, self._ms(start), "", {}, d, error=reason)
        return d

    def _ms(self, start) -> int:
        return int((self._monotonic() - start) * 1000)

    def _wait(self, reason_code: str, warnings=None) -> AIEntryDecision:
        return AIEntryDecision(
            DEC_WAIT, 0, QUALITY_UNKNOWN, RISK_UNKNOWN,
            [reason_code], warnings or [], self.provider.model_name, self.cfg.prompt_version)

    def _build_decision(self, parsed: dict) -> AIEntryDecision:
        return AIEntryDecision(
            decision=parsed["decision"],
            confidence_score=int(parsed["confidence_score"]),
            entry_quality=parsed["entry_quality"],
            risk_level=parsed["risk_level"],
            reason=list(parsed["reason"]),
            warnings=list(parsed["warnings"]),
            model_name=self.provider.model_name,      # 엔진이 권위 있음
            prompt_version=self.cfg.prompt_version,
        )

    def _validate(self, parsed) -> None:
        if not isinstance(parsed, dict):
            raise ValueError("not an object")
        for key in ("decision", "confidence_score", "entry_quality", "risk_level", "reason", "warnings"):
            if key not in parsed:
                raise ValueError(f"missing {key}")
        if parsed["decision"] not in DECISIONS:
            raise ValueError("bad decision")
        cs = parsed["confidence_score"]
        if isinstance(cs, bool) or not isinstance(cs, (int, float)):
            raise ValueError("bad confidence type")
        if not (0 <= cs <= 100):
            raise ValueError("confidence out of range")
        if parsed["entry_quality"] not in QUALITIES:
            raise ValueError("bad entry_quality")
        if parsed["risk_level"] not in RISK_LEVELS:
            raise ValueError("bad risk_level")
        if not isinstance(parsed["reason"], list) or not isinstance(parsed["warnings"], list):
            raise ValueError("reason/warnings must be arrays")

    def _emit_log(self, h, latency, raw, parsed, decision, cached_flag=False, error=None) -> None:
        log = AIDecisionLog(
            prompt_version=self.cfg.prompt_version, model_name=self.provider.model_name,
            input_hash=h, latency_ms=latency, raw_response=raw, parsed_response=parsed,
            decision=decision.decision, confidence_score=decision.confidence_score,
            cached=cached_flag, error=error)
        self.last_log = log
        if self.log_store is not None:
            self.log_store.save(log)

    def _portfolio_view(self, portfolio) -> dict:
        get = lambda k, d=None: getattr(portfolio, k, d)
        total_eff = getattr(portfolio, "total_effective_exposure", None)
        return {
            "account_value": get("account_value"),
            "session": get("session"),
            "is_eod_flatten_window": get("is_eod_flatten_window"),
            "consecutive_losses": get("consecutive_losses"),
            "daily_pnl_pct": get("daily_pnl_pct"),
            "drawdown_pct": get("drawdown_pct"),
            "total_effective_exposure": total_eff,
        }

    def _input_hash(self, candidate, indicators, portfolio, risk_decision, market) -> str:
        payload = {
            "candidate": _as_dict(candidate),
            "indicators": indicators,
            "portfolio": self._portfolio_view(portfolio),
            "risk": _as_dict(risk_decision),
            "market": _as_dict(market),
            "prompt_version": self.cfg.prompt_version,
            "model_name": self.provider.model_name,
        }
        s = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    def _user_prompt(self, candidate, indicators, portfolio, risk_decision, market) -> str:
        payload = {
            "candidate": _as_dict(candidate),
            "indicator_snapshot": indicators,
            "portfolio": self._portfolio_view(portfolio),
            "risk_decision": _as_dict(risk_decision),
            "market_context": _as_dict(market),
        }
        return ("Evaluate this NEW long entry candidate as an entry filter. "
                "Return ONLY the JSON object.\n"
                + json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False))


def _as_dict(obj):
    if obj is None:
        return None
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return obj
    return str(obj)


def is_entry_permitted(ai_decision: AIEntryDecision, risk_decision) -> bool:
    """최종 진입 가부: AI 가 APPROVE 이고 RiskManager 가 allow 여야 한다.
    (AI 가 APPROVE 해도 risk block 이면 False)"""
    allowed = getattr(risk_decision, "allowed", False)
    return ai_decision.decision == DEC_APPROVE and bool(allowed)
