"""Toss-only repair candidates for the frozen Gap+RVOL D9 dependencies.

Only dependency symbol-days that the tracked provider probe marked available
may be collected.  The canonical database and frozen research snapshot are
never mutated; parsed candles and raw payloads live in an isolated candidate
root and are later applied through a conflict-checked read overlay.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.history.raw_snapshot import RawSnapshotStore
from src.history.store import HistoryStore
from src.models import Candle
from src.research.gap_rvol.dependency_repair import (
    FROZEN_PLAN_FILE_SHA256,
    FROZEN_TARGET_COUNT,
    load_frozen_dependency_plan,
)
from src.research.gap_rvol.features import window_sum_for_lookback
from src.research.gap_rvol.loading import regular_1m
from src.research.gap_rvol.provider_repair import (
    RepairTarget,
    TargetCollection,
    data_content_sha256,
    data_content_sha256_path,
    summarize_collection,
)
from src.research.gap_rvol.windows import minute_of_day, regular_close_minute_checked

SCHEMA = "gap-rvol-d9-toss-dependency-candidate/v1"
PROBE_FILE_SHA256 = "7d27367c20f4d43444a1f622ef4331f07c1bb666529cae54227165e5ce9e1717"
EXPECTED_AVAILABLE_COUNT = 265


@dataclass(frozen=True, order=True)
class DependencyCandidateTarget:
    day: date
    symbol: str
    roles: tuple[str, ...]
    required_by: tuple[tuple[str, str], ...]

    def as_repair_target(self) -> RepairTarget:
        return RepairTarget(
            day=self.day,
            symbol=self.symbol,
            original_reasons=self.roles,
        )


@dataclass(frozen=True)
class DependencyCandidateIdentity:
    manifest_file_sha256: str
    candidate_data_sha256: str
    raw_content_sha256: str
    candidate_rows: int
    eligible_targets: tuple[tuple[str, str], ...]
    ineligible_targets: tuple[tuple[str, str], ...]


def sha256_path(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_tracked_dependency_probe(path: str) -> dict[str, Any]:
    if sha256_path(path) != PROBE_FILE_SHA256:
        raise ValueError("dependency probe file hash mismatch")
    with open(path, encoding="utf-8") as handle:
        probe = json.load(handle)
    if probe.get("schema") != "gap-rvol-d9-dependency-probe/v1":
        raise ValueError("unexpected dependency probe schema")
    if int(probe.get("target_count", -1)) != FROZEN_TARGET_COUNT:
        raise ValueError("dependency probe target count mismatch")
    if int(probe.get("available_count", -1)) != EXPECTED_AVAILABLE_COUNT:
        raise ValueError("dependency probe available count mismatch")
    return probe


def build_dependency_candidate_targets(
    *, plan: Mapping[str, Any], probe: Mapping[str, Any]
) -> tuple[DependencyCandidateTarget, ...]:
    plan_targets = {
        (str(item["symbol"]), str(item["day"])): item
        for item in plan["targets"]
    }
    available = {
        (str(item["symbol"]), str(item["target_day"]))
        for item in probe["observations"]
        if item["has_target_day"] is True
    }
    if len(available) != EXPECTED_AVAILABLE_COUNT:
        raise ValueError("available dependency target set is not frozen at 265")
    if not available.issubset(plan_targets):
        raise ValueError("probe contains a target outside the frozen plan")
    targets = []
    for key in sorted(available, key=lambda item: (item[1], item[0])):
        item = plan_targets[key]
        targets.append(DependencyCandidateTarget(
            day=date.fromisoformat(str(item["day"])),
            symbol=str(item["symbol"]),
            roles=tuple(sorted(map(str, item["roles"]))),
            required_by=tuple(sorted(
                (str(event["day"]), str(event["reason"]))
                for event in item["required_by"]
            )),
        ))
    return tuple(targets)


def _has_usable_regular_close(candles: tuple[Candle, ...], day: date) -> bool:
    close = regular_close_minute_checked(day)
    return any(minute_of_day(candle) < close for candle in regular_1m(candles))


def summarize_dependency_collection(
    collection: TargetCollection,
    target: DependencyCandidateTarget,
    *,
    rvol_minutes: int = 15,
) -> dict[str, object]:
    base = summarize_collection(collection)
    candles = collection.candles
    has_close = _has_usable_regular_close(candles, target.day)
    has_rvol = window_sum_for_lookback(
        regular_1m(candles), day=target.day, minutes=rvol_minutes
    ) is not None
    role_passes = {
        "prior_regular_close": has_close,
        "rvol_15m_lookback": has_rvol,
    }
    unknown = set(target.roles) - set(role_passes)
    if unknown:
        raise ValueError(f"unknown dependency roles: {sorted(unknown)}")
    return {
        **base,
        "roles": list(target.roles),
        "required_by": [
            {"day": day, "reason": reason} for day, reason in target.required_by
        ],
        "has_usable_regular_close": has_close,
        "has_complete_rvol_window": has_rvol,
        "eligible_dependency": all(role_passes[role] for role in target.roles),
    }


def build_dependency_candidate_manifest(
    *,
    targets: Sequence[DependencyCandidateTarget],
    summaries: Sequence[Mapping[str, object]],
    raw_manifest: Mapping[str, object],
    repair_db_content_sha256: str,
    repair_db_rows: int,
    generated_at_utc: str,
) -> dict[str, object]:
    if len(targets) != EXPECTED_AVAILABLE_COUNT or len(summaries) != len(targets):
        raise ValueError("manifest requires exactly the frozen 265 available targets")
    expected = {(target.symbol, target.day.isoformat()) for target in targets}
    actual = {(str(item["symbol"]), str(item["day"])) for item in summaries}
    if actual != expected:
        raise ValueError("candidate summaries do not match the frozen available targets")
    eligible = sum(item["eligible_dependency"] is True for item in summaries)
    roles = Counter(role for target in targets for role in target.roles)
    return {
        "schema": SCHEMA,
        "generated_at_utc": generated_at_utc,
        "scope": {
            "canonical_db_written": False,
            "frozen_snapshot_written": False,
            "isolated_candidate_db_written": True,
            "blind_oos_opened": False,
            "performance_computed": False,
            "forward_returns_computed": False,
        },
        "selection_rule": (
            "frozen D9 dependency target AND tracked Toss dependency probe "
            "has_target_day=true"
        ),
        "provider": "toss",
        "interval": "1m",
        "sessions": ["PRE", "REGULAR", "POST"],
        "plan_file_sha256": FROZEN_PLAN_FILE_SHA256,
        "probe_file_sha256": PROBE_FILE_SHA256,
        "target_count": len(targets),
        "role_counts": {key: roles[key] for key in sorted(roles)},
        "eligible_dependency_count": eligible,
        "ineligible_dependency_count": len(targets) - eligible,
        "repair_db_rows": repair_db_rows,
        "repair_db_data_content_sha256": repair_db_content_sha256,
        "raw_content_file_count": raw_manifest["content_file_count"],
        "raw_content_sha256": raw_manifest["raw_content_sha256"],
        "http_429": sum(int(item["http_429"]) for item in summaries),
        "targets": sorted(
            (dict(item) for item in summaries),
            key=lambda item: (str(item["day"]), str(item["symbol"])),
        ),
    }


def manifest_bytes(manifest: Mapping[str, object]) -> bytes:
    return (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def render_dependency_candidate_manifest(manifest: Mapping[str, object]) -> str:
    missing = Counter(
        str(item["first_missing_regular_minute"])
        for item in manifest["targets"]
        if item["eligible_dependency"] is not True
    )
    return "\n".join([
        "# P0-US30 Gap+RVOL Toss dependency repair candidate",
        "",
        f"> generated: `{manifest['generated_at_utc']}`",
        "",
        "## 판정",
        "",
        "**TOSS_DEPENDENCY_REPAIR_CANDIDATE**",
        "",
        f"- frozen available targets: `{manifest['target_count']}`",
        f"- eligible dependencies: `{manifest['eligible_dependency_count']}`",
        f"- ineligible dependencies: `{manifest['ineligible_dependency_count']}`",
        f"- isolated candidate rows: `{manifest['repair_db_rows']}`",
        f"- HTTP 429 recovered: `{manifest['http_429']}`",
        (
            "- first missing REGULAR minute among ineligible targets: `"
            f"{json.dumps(dict(sorted(missing.items())), sort_keys=True)}`"
        ),
        "",
        "## Identity",
        "",
        f"- plan: `{manifest['plan_file_sha256']}`",
        f"- probe: `{manifest['probe_file_sha256']}`",
        f"- candidate data: `{manifest['repair_db_data_content_sha256']}`",
        f"- candidate raw: `{manifest['raw_content_sha256']}`",
        "",
        "## Scope",
        "",
        "- Toss-only / frozen available target intersection: `True`",
        "- canonical DB / frozen snapshot written: `False`",
        "- blind OOS opened: `False`",
        "- performance / forward returns computed: `False`",
        "",
        (
            "`has_target_day=true`는 날짜 존재만 뜻했다. 실제 full-day 수집 결과 "
            "고정 RVOL 입력인 09:30–09:44 ET가 완전한 대상은 0개였으므로, "
            "이번 후보 계층은 D9 결손을 해결하지 못한다."
        ),
        "",
    ])


def verify_dependency_candidate_identity(
    *, manifest_path: str, candidate_db: str, candidate_raw_root: str
) -> tuple[dict[str, Any], DependencyCandidateIdentity]:
    with open(manifest_path, "rb") as handle:
        payload = handle.read()
    manifest_file_sha = hashlib.sha256(payload).hexdigest()
    manifest = json.loads(payload)
    if manifest.get("schema") != SCHEMA:
        raise ValueError("unexpected dependency candidate manifest schema")
    if manifest.get("plan_file_sha256") != FROZEN_PLAN_FILE_SHA256:
        raise ValueError("dependency candidate plan identity mismatch")
    if manifest.get("probe_file_sha256") != PROBE_FILE_SHA256:
        raise ValueError("dependency candidate probe identity mismatch")
    if int(manifest.get("target_count", -1)) != EXPECTED_AVAILABLE_COUNT:
        raise ValueError("dependency candidate target count mismatch")
    actual_data = data_content_sha256_path(candidate_db)
    if actual_data != manifest["repair_db_data_content_sha256"]:
        raise ValueError("dependency candidate DB content hash mismatch")
    raw = RawSnapshotStore(candidate_raw_root).manifest()
    if raw["raw_content_sha256"] != manifest["raw_content_sha256"]:
        raise ValueError("dependency candidate raw content hash mismatch")
    if int(raw["content_file_count"]) != int(manifest["raw_content_file_count"]):
        raise ValueError("dependency candidate raw file count mismatch")
    all_targets = {
        (str(item["symbol"]), str(item["day"])) for item in manifest["targets"]
    }
    eligible = {
        (str(item["symbol"]), str(item["day"]))
        for item in manifest["targets"] if item["eligible_dependency"] is True
    }
    ineligible = all_targets - eligible
    if len(all_targets) != EXPECTED_AVAILABLE_COUNT:
        raise ValueError("dependency candidate target set is not unique")
    con = sqlite3.connect(f"file:{candidate_db}?mode=ro", uri=True)
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("dependency candidate DB integrity failed")
        rows = int(con.execute("SELECT COUNT(*) FROM candle_history").fetchone()[0])
        db_targets = {
            (str(symbol), str(day))
            for symbol, day in con.execute(
                "SELECT DISTINCT symbol,substr(ts_exchange,1,10) "
                "FROM candle_history WHERE interval='1m'"
            )
        }
        conflicts = int(con.execute("SELECT COUNT(*) FROM import_conflict").fetchone()[0])
    finally:
        con.close()
    if rows != int(manifest["repair_db_rows"]):
        raise ValueError("dependency candidate DB row count mismatch")
    if db_targets != all_targets:
        raise ValueError("dependency candidate DB target set mismatch")
    if conflicts:
        raise ValueError("dependency candidate DB has import conflicts")
    return manifest, DependencyCandidateIdentity(
        manifest_file_sha256=manifest_file_sha,
        candidate_data_sha256=actual_data,
        raw_content_sha256=str(raw["raw_content_sha256"]),
        candidate_rows=rows,
        eligible_targets=tuple(sorted(eligible)),
        ineligible_targets=tuple(sorted(ineligible)),
    )


def live_candidate_hash(store: HistoryStore) -> str:
    return data_content_sha256(store)
