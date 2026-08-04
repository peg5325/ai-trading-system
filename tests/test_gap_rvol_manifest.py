from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.research.gap_rvol.contract import all_specs, load_contract
from src.research.gap_rvol.manifest import (
    IMPLEMENTATION_MERGE_COMMIT,
    PREREG_MERGE_COMMIT,
    DevelopmentResultEvidence,
    PreconditionEvidence,
    SnapshotDefectEvidence,
    SnapshotEvidence,
    build_result,
    payload_sha256,
)
from src.research.gap_rvol.simulator import DaySimulationResult
from src.research.gap_rvol.windows import development_eval_days
from src.research.orb.identity import IdentityCheck, IdentityResult


def _snapshot() -> SnapshotEvidence:
    contract = load_contract()
    ident = contract["data_identity"]
    return SnapshotEvidence(
        basename=ident["snapshot_basename"],
        snapshot_file_sha256=ident["snapshot_file_sha256"],
        snapshot_content_sha256=ident["snapshot_content_sha256"],
        manifest_data_sha256=ident["manifest_data_sha256"],
        raw_content_sha256=ident["raw_content_sha256"],
        as_of=ident["as_of"],
        collection_run_id=ident["collection_run_id"],
    )


def _preconditions() -> PreconditionEvidence:
    return PreconditionEvidence(
        implementation_merge_commit=IMPLEMENTATION_MERGE_COMMIT,
        implementation_review_evidence_url=(
            "https://github.com/peg5325/ai-trading-system/pull/101"
            "#issuecomment-5126919041"
        ),
        implementation_map_status="tracked",
        runner_reviewed_commit="a" * 40,
        runner_code_review_status="approved",
        runner_architect_status="clear",
        runner_review_evidence_url=(
            "https://github.com/peg5325/ai-trading-system/pull/102"
            "#issuecomment-5127000000"
        ),
        loader_boundary_status="passed",
        loader_boundary_test=(
            "tests/test_gap_rvol_loading_and_map.py::"
            "test_performance_package_and_runner_have_no_loader_bypass"
        ),
    )


def _results() -> dict[str, tuple[DaySimulationResult, ...]]:
    days = development_eval_days(load_contract())
    return {
        spec.name: tuple(
            DaySimulationResult(
                day=day,
                spec_name=spec.name,
                trades=(),
                skipped=(),
                excluded=(),
                filtered=(),
                major_defects=(),
                open_positions_at_close=0,
            )
            for day in days
        )
        for spec in all_specs(load_contract())
    }


def _evidence() -> DevelopmentResultEvidence:
    return DevelopmentResultEvidence(
        producer_commit="a" * 40,
        preconditions=_preconditions(),
        snapshot=_snapshot(),
        results_by_spec=_results(),
        snapshot_defects=SnapshotDefectEvidence(0, ()),
    )


def test_payload_hash_is_deterministic_and_discloses_gap_rvol_scope():
    evidence = _evidence()
    contract = load_contract()
    a = build_result(
        evidence, contract=contract,
        generated_at_utc="2026-07-30T00:00:00Z"
    )
    b = build_result(
        evidence, contract=contract,
        generated_at_utc="2026-07-30T01:00:00Z"
    )
    assert a["payload"] == b["payload"]
    assert a["payload_sha256"] == b["payload_sha256"]
    assert a["payload_sha256"] == payload_sha256(a["payload"])
    assert a["payload"]["window"]["data_trading_days"] == 690
    assert a["payload"]["window"]["eval_trading_days"] == 670
    assert a["payload"]["single_pass"]["spec_count"] == 9
    assert set(a["payload"]["stats"]) == {"low", "official", "stress"}
    assert a["payload"]["single_pass"]["oos_opened"] is False
    assert a["payload"]["verdict"]["value"] == "NO_SELECTION"


def test_frozen_commits_are_gap_rvol_stage_values():
    assert PREREG_MERGE_COMMIT == "d16c6692beca828e6afba32b57fad8a0282089c6"
    assert IMPLEMENTATION_MERGE_COMMIT == (
        "caa507415c2d5e34a25279fa83a356d6b19a54e6"
    )


def test_d9_precedes_economic_failures_as_inconclusive_data():
    evidence = replace(
        _evidence(),
        snapshot_defects=SnapshotDefectEvidence(
            conflict_total=29,
            identity_failures=(),
            conflict_baseline_total=28,
            new_conflict_total=1,
            conflict_baseline_source="pre-result baseline",
        ),
    )
    result = build_result(
        evidence, contract=load_contract(), generated_at_utc="2026-07-30T00:00:00Z"
    )
    assert result["payload"]["verdict"]["value"] == "INCONCLUSIVE_DATA"
    assert "D9" in result["payload"]["verdict"]["failed"]
    assert "D1" in result["payload"]["verdict"]["failed"]


def test_pre_result_conflict_baseline_is_not_recounted_as_new_major_defect():
    evidence = replace(
        _evidence(),
        snapshot_defects=SnapshotDefectEvidence(
            conflict_total=28,
            identity_failures=(),
            conflict_baseline_total=28,
            new_conflict_total=0,
            conflict_baseline_source="pre-result baseline",
        ),
    )
    result = build_result(
        evidence, contract=load_contract(), generated_at_utc="2026-07-30T00:00:00Z"
    )
    defects = result["payload"]["defect_ratios"]
    assert defects["conflict_total"] == 28
    assert defects["conflict_baseline_total"] == 28
    assert defects["new_conflict_total"] == 0
    assert defects["major_defect_count"] == 0


def test_preconditions_are_fail_closed():
    evidence = _evidence()
    with pytest.raises(ValueError, match="code-reviewer"):
        build_result(
            replace(
                evidence,
                preconditions=replace(
                    evidence.preconditions,
                    runner_code_review_status="commented",
                ),
            ),
            contract=load_contract(),
            generated_at_utc="2026-07-30T00:00:00Z",
        )
    with pytest.raises(ValueError, match="producer_commit"):
        build_result(
            replace(
                evidence,
                preconditions=replace(
                    evidence.preconditions,
                    runner_reviewed_commit="b" * 40,
                ),
            ),
            contract=load_contract(),
            generated_at_utc="2026-07-30T00:00:00Z",
        )


def test_snapshot_evidence_requires_all_four_identity_matches():
    contract = load_contract()
    expected = contract["data_identity"]
    names = (
        ("snapshot_file", "snapshot_file_sha256"),
        ("snapshot_content", "snapshot_content_sha256"),
        ("manifest_data", "manifest_data_sha256"),
        ("raw_content", "raw_content_sha256"),
    )
    matching = IdentityResult(
        checks=tuple(
            IdentityCheck(name, expected[key], expected[key], "match")
            for name, key in names
        ),
        ok=True,
        verdict=None,
    )
    snapshot = SnapshotEvidence.from_identity(contract=contract, identity=matching)
    assert snapshot.raw_content_sha256 == expected["raw_content_sha256"]

    mismatch = replace(
        matching,
        checks=(
            replace(matching.checks[0], actual="bad", status="mismatch"),
            *matching.checks[1:],
        ),
        ok=False,
    )
    with pytest.raises(ValueError, match="four"):
        SnapshotEvidence.from_identity(contract=contract, identity=mismatch)

    falsely_marked_match = replace(
        matching,
        checks=(
            replace(
                matching.checks[0],
                expected="bad",
                actual="bad",
                status="match",
            ),
            *matching.checks[1:],
        ),
    )
    with pytest.raises(ValueError, match="frozen contract"):
        SnapshotEvidence.from_identity(
            contract=contract, identity=falsely_marked_match
        )


def test_snapshot_defects_count_only_conflicts_beyond_pre_result_baseline():
    contract = load_contract()
    expected = contract["data_identity"]
    identity = IdentityResult(
        checks=(
            IdentityCheck(
                "snapshot_file",
                expected["snapshot_file_sha256"],
                expected["snapshot_file_sha256"],
                "match",
            ),
            IdentityCheck(
                "snapshot_content",
                expected["snapshot_content_sha256"],
                expected["snapshot_content_sha256"],
                "match",
            ),
            IdentityCheck(
                "manifest_data",
                expected["manifest_data_sha256"],
                expected["manifest_data_sha256"],
                "match",
            ),
            IdentityCheck(
                "raw_content",
                expected["raw_content_sha256"],
                expected["raw_content_sha256"],
                "match",
            ),
        ),
        ok=True,
        verdict=None,
    )
    manifest = json.loads(Path(
        "docs/research/P0_US30_ORB_CLEAN_1M_MANIFEST.json"
    ).read_text(encoding="utf-8"))
    defects = SnapshotDefectEvidence.from_sources(
        manifest=manifest,
        identity=identity,
        contract=contract,
    )
    assert defects.conflict_total == 28
    assert defects.conflict_baseline_total == 28
    assert defects.new_conflict_total == 0
    assert dict(defects.conflict_baseline_by_symbol) == {
        "NVDA": 5,
        "QQQ": 7,
        "SOXL": 12,
        "SPY": 3,
        "VST": 1,
    }
    assert defects.snapshot_report_conflict_candles == 1
    assert defects.identity_failures == ()
