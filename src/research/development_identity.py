"""Blind-OOS-safe identity checks for frozen intraday research artifacts."""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from typing import Any

from src.history.daily_manifest import build_manifest
from src.history.raw_snapshot import RawSnapshotStore
from src.history.research_snapshot import file_sha256


def _read_json(path: str) -> Mapping[str, Any]:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise TypeError(f"identity JSON object expected: {path}")
    return value


def _check(name: str, expected: str, actual: str) -> dict[str, str]:
    status = "match" if actual == expected else "mismatch"
    if status != "match":
        raise RuntimeError(
            f"identity mismatch: {name} expected={expected} actual={actual}"
        )
    return {"name": name, "expected": expected, "actual": actual, "status": status}


def verify_development_identity(
    *,
    snapshot_path: str,
    raw_root: str,
    daily_db_path: str,
    universe_yaml: str,
    intraday_manifest_path: str,
    snapshot_receipt_path: str,
    daily_manifest_path: str,
    expected_snapshot_file: str,
    expected_snapshot_content: str,
    expected_intraday_manifest: str,
    expected_raw_content: str,
    expected_daily_full_content: str,
    expected_daily_full_rows: int,
    development_end: str,
    expected_daily_development_content: str,
    expected_daily_development_rows: int,
    daily_manifest_builder: Callable[..., Mapping[str, Any]] = build_manifest,
) -> list[dict[str, str]]:
    """Authenticate full artifacts without decoding blind-OOS OHLCV.

    The live snapshot is authenticated by its file SHA. Semantic content is
    transitively bound through the tracked creation receipt. Full daily history
    is checked from its tracked receipt, while the live DB content scan stops at
    ``development_end``.
    """
    if not os.path.isfile(snapshot_path):
        raise RuntimeError("frozen intraday snapshot missing")
    if not os.path.isdir(raw_root):
        raise RuntimeError("frozen raw snapshot root missing")

    snapshot_receipt = _read_json(snapshot_receipt_path)["snapshot"]
    intraday_manifest = _read_json(intraday_manifest_path)
    daily_manifest_receipt = _read_json(daily_manifest_path)
    snapshot_file_actual = file_sha256(snapshot_path)
    raw_actual = str(RawSnapshotStore(raw_root).manifest()["raw_content_sha256"])

    checks = [
        _check("snapshot_file", expected_snapshot_file, snapshot_file_actual),
        _check(
            "snapshot_receipt_file",
            snapshot_file_actual,
            str(snapshot_receipt["file_sha256"]),
        ),
        _check(
            "snapshot_content_receipt",
            expected_snapshot_content,
            str(snapshot_receipt["content_sha256"]),
        ),
        _check(
            "manifest_data_receipt",
            expected_intraday_manifest,
            str(intraday_manifest["data_content_sha256"]),
        ),
        _check("raw_content_index", expected_raw_content, raw_actual),
        _check(
            "daily_full_manifest_receipt",
            expected_daily_full_content,
            str(daily_manifest_receipt["data_content_sha256"]),
        ),
        _check(
            "daily_full_rows_receipt",
            str(expected_daily_full_rows),
            str(daily_manifest_receipt["summary"]["total_rows"]),
        ),
    ]
    daily_development = daily_manifest_builder(
        daily_db_path,
        universe_yaml,
        collection_params={},
        collection_code_commit=None,
        generated_at_utc="identity-check",
        as_of=development_end,
        collection_completed_through=development_end,
    )
    checks.extend(
        [
            _check(
                "daily_development_content",
                expected_daily_development_content,
                str(daily_development["data_content_sha256"]),
            ),
            _check(
                "daily_development_rows",
                str(expected_daily_development_rows),
                str(daily_development["summary"]["total_rows"]),
            ),
        ]
    )
    return checks
