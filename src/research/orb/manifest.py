"""ORB 결정론적 결과 envelope — 실제 결과 생성은 4-C."""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from typing import Any

SCHEMA = "orb-development-result/v1"
PREREG_MERGE_COMMIT = "e42dafcf4a5f803c1af9a3535a980492c93e9c96"
REQUIRED_PAYLOAD_KEYS = (
    "prereg", "producer_commit", "snapshot", "config_version", "schema_version",
    "window", "specs", "stats", "excluded", "skipped", "defect_ratios",
    "dev_gate", "verdict", "loader", "single_pass",
)


def canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_payload_bytes(payload)).hexdigest()


def build_result(payload: Mapping[str, Any], *, generated_at_utc: str) -> dict[str, Any]:
    deterministic = dict(payload)
    missing = [key for key in REQUIRED_PAYLOAD_KEYS if key not in deterministic]
    if missing:
        raise ValueError(f"ORB result payload 필수 필드 누락: {missing}")
    prereg = deterministic["prereg"]
    if prereg.get("prereg_merge_commit") != PREREG_MERGE_COMMIT:
        raise ValueError("prereg_merge_commit 불일치")
    loader = deterministic["loader"]
    if loader != {
        "function": "src.history.oos_guard.research_load",
        "purpose": "performance",
        "track": "intraday",
    }:
        raise ValueError("guarded loader provenance 불일치")
    if deterministic["single_pass"].get("oos_opened") is not False:
        raise ValueError("development result는 oos_opened=false 여야 한다")
    return {
        "generated_at_utc": generated_at_utc,
        "payload": deterministic,
        "payload_sha256": payload_sha256(deterministic),
        "schema": SCHEMA,
    }


def write_result(
    path: str,
    payload: Mapping[str, Any],
    *,
    generated_at_utc: str,
) -> None:
    """완성본만 atomic replace. generated_at은 payload 해시 밖이다."""
    result = build_result(payload, generated_at_utc=generated_at_utc)
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    partial = f"{path}.partial"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(partial, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, path)
