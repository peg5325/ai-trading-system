"""Bounded HTTP transport with host allowlisting and sanitized receipts."""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from src.premarket.providers.artifacts import RawEvidenceStore
from src.premarket.providers.config import HttpPolicy

_RETRYABLE = frozenset({429, 500, 502, 503, 504})


class HttpCollectionError(RuntimeError):
    def __init__(self, code: str, *, receipt: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.receipt = receipt


def sanitize_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


class BoundedHttpClient:
    def __init__(
        self,
        policy: HttpPolicy,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.policy = policy
        self._sleep = sleep
        self._client = httpx.Client(
            timeout=httpx.Timeout(policy.timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        provider: str,
        resource: str,
        collected_at: str,
        raw_store: RawEvidenceStore,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        parsed = urlsplit(url)
        if parsed.scheme != "https":
            raise ValueError("only https provider URLs are allowed")
        if (parsed.hostname or "").lower() not in self.policy.allowed_hosts:
            raise ValueError(f"host not allowed: {parsed.hostname}")
        safe_url = sanitize_url(url)
        for attempt in range(self.policy.max_retries + 1):
            try:
                with self._client.stream("GET", url, params=params, headers=headers) as response:
                    body_parts = bytearray()
                    for chunk in response.iter_bytes():
                        body_parts.extend(chunk)
                        if len(body_parts) > self.policy.max_response_bytes:
                            raise HttpCollectionError("HTTP_RESPONSE_TOO_LARGE")
                    body = bytes(body_parts)
                    status_code = response.status_code
                    content_type = response.headers.get("content-type", "application/octet-stream")
            except httpx.RequestError as error:
                if attempt < self.policy.max_retries:
                    self._sleep(self.policy.retry_backoff_seconds * (2**attempt))
                    continue
                code = "HTTP_TIMEOUT" if isinstance(error, httpx.TimeoutException) else "HTTP_REQUEST_FAILED"
                raise HttpCollectionError(code) from error
            receipt = raw_store.emit(
                provider=provider,
                resource=resource,
                collected_at=collected_at,
                request_url=safe_url,
                status_code=status_code,
                content_type=content_type,
                body=body,
            )
            if status_code in _RETRYABLE and attempt < self.policy.max_retries:
                self._sleep(self.policy.retry_backoff_seconds * (2**attempt))
                continue
            if not 200 <= status_code < 300:
                raise HttpCollectionError(f"HTTP_STATUS_{status_code}", receipt=receipt)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HttpCollectionError("INVALID_JSON", receipt=receipt) from error
            if not isinstance(payload, dict):
                raise HttpCollectionError("JSON_ROOT_NOT_OBJECT", receipt=receipt)
            return payload, receipt
        raise AssertionError("unreachable")
