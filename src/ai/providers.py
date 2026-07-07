"""AIProvider 구현체.

- FakeAIProvider: 테스트/수동용. 실제 호출 없이 정해진 응답/예외를 낸다.
- OpenAIProvider: 실제 OpenAI 호출(openai SDK lazy import). 실거래/통합 테스트에서만 사용.
"""
from __future__ import annotations

import json

from src.ai.models import AIProviderError, AIRateLimitError, AITimeoutError


class FakeAIProvider:
    """결정된 raw 문자열 또는 예외를 반환하는 테스트 provider."""

    def __init__(self, *, response: dict | None = None, raw: str | None = None,
                 raise_exc: Exception | None = None, model_name: str = "fake-model"):
        self.model_name = model_name
        self._response = response
        self._raw = raw
        self._raise = raise_exc
        self.calls = 0

    def complete(self, system, user, *, temperature, timeout, response_schema) -> str:
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        if self._raw is not None:
            return self._raw
        return json.dumps(self._response or {})


class OpenAIProvider:
    """실제 OpenAI 호출. openai 패키지가 필요하며 lazy import 한다.
    OPENAI_API_KEY 환경변수를 사용한다. temperature=0 로 재현성을 높인다."""

    def __init__(self, model_name: str = "gpt-5.5-mini", api_key: str | None = None):
        self.model_name = model_name
        self._api_key = api_key

    def complete(self, system, user, *, temperature, timeout, response_schema) -> str:
        try:
            import openai  # lazy: 테스트 환경엔 없어도 됨
        except ImportError as e:  # pragma: no cover
            raise AIProviderError("openai 패키지가 설치되어 있지 않습니다 (pip install openai)") from e

        client = openai.OpenAI(api_key=self._api_key, timeout=timeout)
        try:
            resp = client.chat.completions.create(
                model=self.model_name,
                temperature=temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return resp.choices[0].message.content or ""
        except Exception as e:  # pragma: no cover - 실호출 경로
            name = type(e).__name__.lower()
            if "timeout" in name:
                raise AITimeoutError(str(e)) from e
            if "ratelimit" in name or "rate_limit" in name:
                raise AIRateLimitError(str(e)) from e
            raise AIProviderError(str(e)) from e
