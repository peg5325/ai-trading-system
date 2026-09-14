"""Shared deterministic provider-result helpers."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from src.premarket.schedule import iso_z, parse_iso


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def payload_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def unavailable(provider: str, kind: str, source_url: str, as_of: str, code: str) -> dict[str, Any]:
    return result(provider, kind, "UNAVAILABLE", source_url, as_of, [], [], code)


def result(
    provider: str,
    kind: str,
    status: str,
    source_url: str,
    as_of: str,
    items: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    error_code: str | None = None,
) -> dict[str, Any]:
    receipt_hashes = sorted(receipt["body_sha256"] for receipt in receipts)
    content_sha = payload_sha256(receipt_hashes)
    received_at = max(
        (str(receipt["received_at"]) for receipt in receipts),
        default=None,
    )
    observed_candidates: list[str] = []
    for item in items:
        value = item.get("published_at") or item.get("accepted_at")
        if not value:
            continue
        try:
            observed_candidates.append(iso_z(parse_iso(str(value))))
        except (TypeError, ValueError):
            continue
    observed = max(observed_candidates, default=received_at)
    return {
        "provider": provider,
        "kind": kind,
        "status": status,
        "source": {
            "source_id": f"{provider}-{kind.lower()}",
            "kind": kind,
            "status": status,
            "observed_at": observed,
            "available_at": received_at,
            "ingested_at": received_at,
            "source_url": source_url,
            "content_sha256": content_sha,
        },
        "items": sorted(items, key=lambda item: (str(item.get("item_id", "")), canonical_bytes(item))),
        "receipt_ids": sorted(receipt["receipt_id"] for receipt in receipts),
        "error_code": error_code,
    }
