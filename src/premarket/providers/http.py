"""Bounded HTTP transport with host allowlisting and sanitized receipts."""
from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from src.premarket.providers.artifacts import RawEvidenceStore
from src.premarket.providers.config import HttpPolicy
from src.premarket.schedule import iso_z

_RETRYABLE = frozenset({429, 500, 502, 503, 504})


class HttpCollectionError(RuntimeError):
    def __init__(self, code: str, *, receipt: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.receipt = receipt


def sanitize_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BoundedHttpClient:
    def __init__(
        self,
        policy: HttpPolicy,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.policy = policy
        self._sleep = sleep
        self._clock = clock
        self._monotonic = monotonic
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
        raw_store: RawEvidenceStore,
        headers: Mapping[str, str] | None = None,
        before_attempt: Callable[[], bool] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """``before_attempt`` reserves budget (e.g. the Marketaux quota ledger). It is
        not called again for a retry that follows a transport error/timeout (no HTTP
        response): that reservation was never answered, so it carries over. Every
        attempt that gets an HTTP response, of any status, consumes its own unit."""
        parsed = urlsplit(url)
        if parsed.scheme != "https":
            raise ValueError("only https provider URLs are allowed")
        if (parsed.hostname or "").lower() not in self.policy.allowed_hosts:
            raise ValueError(f"host not allowed: {parsed.hostname}")
        safe_url = sanitize_url(url)
        timeout = httpx.Timeout(timeout_seconds) if timeout_seconds is not None else None
        reservation_carried = False
        for attempt in range(self.policy.max_retries + 1):
            if (
                before_attempt is not None
                and not reservation_carried
                and not before_attempt()
            ):
                raise HttpCollectionError("HTTP_REQUEST_BUDGET_EXHAUSTED")
            reservation_carried = False
            requested_at = self._clock()
            started = self._monotonic()
            status_seen: int | None = None
            try:
                with self._client.stream(
                    "GET",
                    url,
                    params=params,
                    headers=headers,
                    **({"timeout": timeout} if timeout is not None else {}),
                ) as response:
                    status_seen = response.status_code
                    body_parts = bytearray()
                    for chunk in response.iter_bytes():
                        body_parts.extend(chunk)
                        if len(body_parts) > self.policy.max_response_bytes:
                            raise HttpCollectionError("HTTP_RESPONSE_TOO_LARGE")
                    body = bytes(body_parts)
                    status_code = response.status_code
                    content_type = response.headers.get("content-type", "application/octet-stream")
                    response_headers = response.headers
            except httpx.RequestError as error:
                self._log_attempt(
                    provider, resource, attempt, started,
                    type(error).__name__ if status_seen is None
                    else f"{status_seen}:{type(error).__name__}",
                )
                if attempt < self.policy.max_retries:
                    # Free retry only when no HTTP response arrived at all.
                    reservation_carried = status_seen is None
                    self._sleep(self.policy.retry_backoff_seconds * (2**attempt))
                    continue
                code = "HTTP_TIMEOUT" if isinstance(error, httpx.TimeoutException) else "HTTP_REQUEST_FAILED"
                raise HttpCollectionError(code) from None
            except HttpCollectionError as error:
                self._log_attempt(provider, resource, attempt, started, f"{status_seen}:{error.code}")
                raise
            self._log_attempt(provider, resource, attempt, started, status_code)
            received_at = self._clock()
            if received_at < requested_at:
                raise HttpCollectionError("HTTP_CLOCK_MOVED_BACKWARD")
            retry_delay = self._retry_delay(
                status_code,
                response_headers,
                attempt=attempt,
                received_at=received_at,
            )
            will_retry = (
                status_code in _RETRYABLE
                and attempt < self.policy.max_retries
            )
            receipt = raw_store.emit(
                provider=provider,
                resource=resource,
                requested_at=iso_z(requested_at),
                received_at=iso_z(received_at),
                request_url=safe_url,
                status_code=status_code,
                content_type=content_type,
                body=body,
                retry_delay_seconds=retry_delay if will_retry else None,
            )
            if will_retry:
                self._sleep(retry_delay)
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

    def _log_attempt(
        self,
        provider: str,
        resource: str,
        attempt: int,
        started: float,
        outcome: int | str,
    ) -> None:
        """One structured stderr line per attempt; not part of receipts or hashes."""
        print(json.dumps({
            "event": "http_attempt",
            "provider": provider,
            "resource": resource,
            "attempt": attempt + 1,
            "elapsed_ms": round((self._monotonic() - started) * 1000),
            "outcome": outcome,
        }, sort_keys=True), file=sys.stderr, flush=True)

    def _retry_delay(
        self,
        status_code: int,
        headers: Mapping[str, str],
        *,
        attempt: int,
        received_at: datetime,
    ) -> float:
        fallback = min(
            self.policy.retry_backoff_seconds * (2**attempt),
            self.policy.max_retry_after_seconds,
        )
        if status_code != 429:
            return fallback
        raw = headers.get("retry-after", "").strip()
        if not raw:
            return fallback
        try:
            delay = float(raw)
        except ValueError:
            try:
                target = parsedate_to_datetime(raw)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                delay = (target.astimezone(timezone.utc) - received_at.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return fallback
        return min(max(0.0, delay), self.policy.max_retry_after_seconds)
