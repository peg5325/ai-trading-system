"""Review Engine용 provider.

- FakeReviewProvider: 테스트/수동용. 실제 GPT 호출 없이 정해진 응답/예외 반환.
- 실제 호출은 src.ai.providers.OpenAIProvider 를 그대로 사용(complete 인터페이스 동일).
"""
from __future__ import annotations

import json


class FakeReviewProvider:
    def __init__(self, *, response: dict | None = None, raw: str | None = None,
                 raise_exc: Exception | None = None, model_name: str = "fake-review-model"):
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
