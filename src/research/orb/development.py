"""ORB 4-C development-only 공식 실행 조립기.

이 모듈은 동결된 development 창만 연다. 날짜·종목·spec·비용·게이트는
사전등록 계약에서 파생하며 blind OOS unlock을 받는 인자가 없다.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.research.orb.contract import load_contract
from src.research.orb.gates import SnapshotDefectEvidence
from src.research.orb.identity import require_identity, verify_snapshot_identity
from src.research.orb.manifest import (
    DevelopmentResultEvidence,
    PreconditionEvidence,
    SnapshotEvidence,
    build_result,
)
from src.research.orb.simulator import (
    build_development_feature_pass,
    simulate_all_specs,
)


@dataclass(frozen=True)
class DevelopmentPaths:
    snapshot_path: str
    raw_root: str
    manifest_path: str


@dataclass(frozen=True)
class DevelopmentRun:
    result: Mapping[str, Any]
    evidence: DevelopmentResultEvidence


def _load_manifest(path: str) -> Mapping[str, object]:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise TypeError("snapshot manifest 최상위는 object여야 한다")
    return value


def _validate_generated_at(value: str) -> None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise ValueError("generated_at_utc는 초 단위 UTC Z 형식이어야 한다")


def run_development(
    *,
    paths: DevelopmentPaths,
    producer_commit: str,
    generated_at_utc: str,
    preconditions: PreconditionEvidence,
) -> DevelopmentRun:
    """P1~P3를 envelope에 고정하고 development 8개 spec을 한 번 계산한다."""
    _validate_generated_at(generated_at_utc)
    preconditions.validate()
    contract = load_contract()
    identity = verify_snapshot_identity(
        contract=contract,
        snapshot_path=paths.snapshot_path,
        raw_root=paths.raw_root,
        manifest_path=paths.manifest_path,
    )
    require_identity(identity)
    snapshot = SnapshotEvidence.from_identity(
        contract=contract,
        identity=identity,
    )
    tracked_manifest = _load_manifest(paths.manifest_path)
    snapshot_defects = SnapshotDefectEvidence.from_sources(
        manifest=tracked_manifest,
        identity=identity,
        contract=contract,
    )
    feature_pass = build_development_feature_pass(
        contract=contract,
        db_path=paths.snapshot_path,
    )
    results_by_spec = simulate_all_specs(
        contract=contract,
        feature_pass=feature_pass,
    )
    evidence = DevelopmentResultEvidence(
        producer_commit=producer_commit,
        preconditions=preconditions,
        snapshot=snapshot,
        results_by_spec=results_by_spec,
        snapshot_defects=snapshot_defects,
    )
    return DevelopmentRun(
        result=build_result(
            evidence,
            contract=contract,
            generated_at_utc=generated_at_utc,
        ),
        evidence=evidence,
    )
