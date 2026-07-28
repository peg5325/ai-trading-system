from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

import src.research.orb.manifest as manifest_module
from src.research.orb.contract import all_specs, load_contract
from src.research.orb.gates import SnapshotDefectEvidence
from src.research.orb.identity import IdentityCheck, IdentityResult
from src.research.orb.manifest import (
    IMPLEMENTATION_MERGE_COMMIT,
    PREREG_MERGE_COMMIT,
    DevelopmentResultEvidence,
    PreconditionEvidence,
    SnapshotEvidence,
    build_result,
    payload_sha256,
)
from src.research.orb.simulator import DaySimulationResult
from src.research.orb.windows import Fold


def _snapshot():
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


def _evidence(day):
    specs = all_specs(load_contract())
    results = {
        spec.name: (
            DaySimulationResult(
                day=day,
                spec_name=spec.name,
                trades=(),
                skipped=(),
                excluded=(),
                major_defects=(),
                open_positions_at_close=0,
            ),
        )
        for spec in specs
    }
    return DevelopmentResultEvidence(
        producer_commit="a" * 40,
        preconditions=PreconditionEvidence(
            implementation_merge_commit=IMPLEMENTATION_MERGE_COMMIT,
            review_status="approved",
            review_evidence_url=(
                "https://github.com/peg5325/ai-trading-system/pull/93"
            ),
            loader_boundary_status="passed",
            loader_boundary_test=(
                "tests/test_oos_guard_wiring.py::"
                "test_no_direct_load_candles_outside_gate"
            ),
        ),
        snapshot=_snapshot(),
        results_by_spec=results,
        snapshot_defects=SnapshotDefectEvidence(28, ()),
    )


@pytest.fixture
def one_day_window(monkeypatch):
    day = date(2024, 3, 4)
    monkeypatch.setattr(
        manifest_module, "development_eval_days", lambda _contract: (day,))
    monkeypatch.setattr(
        manifest_module,
        "folds",
        lambda _contract: (Fold("F01", day, day, 1),),
    )
    return day


def test_payload_hash_is_deterministic_and_generated_time_is_outside(one_day_window):
    evidence = _evidence(one_day_window)
    contract = load_contract()
    a = build_result(
        evidence, contract=contract,
        generated_at_utc="2026-07-28T00:00:00Z")
    b = build_result(
        evidence, contract=contract,
        generated_at_utc="2026-07-28T01:00:00Z")
    assert a["payload"] == b["payload"]
    assert a["payload_sha256"] == b["payload_sha256"]
    assert a["generated_at_utc"] != b["generated_at_utc"]
    assert a["payload_sha256"] == payload_sha256(a["payload"])
    assert a["payload"]["stats"]["official"]["profit_factor"] == "Infinity"
    assert set(a["payload"]["stats"]) == {"low", "official", "stress"}


def test_prereg_merge_commit_is_frozen_stage_three_merge():
    assert PREREG_MERGE_COMMIT == "e42dafcf4a5f803c1af9a3535a980492c93e9c96"


def test_implementation_merge_commit_is_frozen_stage_four_b_merge():
    assert IMPLEMENTATION_MERGE_COMMIT == (
        "ddaacabfb28fca11c1f3280b89fe522a3fca76c2"
    )


def test_preconditions_are_fail_closed(one_day_window):
    evidence = _evidence(one_day_window)
    bad_review = replace(
        evidence.preconditions,
        review_status="commented",
    )
    with pytest.raises(ValueError, match="독립 리뷰"):
        build_result(
            replace(evidence, preconditions=bad_review),
            contract=load_contract(),
            generated_at_utc="x",
        )

    bad_loader = replace(
        evidence.preconditions,
        loader_boundary_status="not_run",
    )
    with pytest.raises(ValueError, match="loader boundary"):
        build_result(
            replace(evidence, preconditions=bad_loader),
            contract=load_contract(),
            generated_at_utc="x",
        )


def test_nested_result_evidence_fails_closed_and_derives_defect_verdict(one_day_window):
    evidence = _evidence(one_day_window)
    missing_spec = dict(evidence.results_by_spec)
    missing_spec.pop("sensitivity")
    with pytest.raises(ValueError, match="spec 결과 집합"):
        build_result(
            replace(evidence, results_by_spec=missing_spec),
            contract=load_contract(),
            generated_at_utc="x",
        )

    raw_defect_results = dict(evidence.results_by_spec)
    raw_defect_results["primary"] = (
        replace(
            raw_defect_results["primary"][0],
            excluded=(("AAPL", "ohlc_invariant_violation"),),
            major_defects=(("AAPL", "ohlc_invariant_violation"),),
        ),
    )
    result = build_result(
        replace(evidence, results_by_spec=raw_defect_results),
        contract=load_contract(),
        generated_at_utc="x",
    )
    assert result["payload"]["defect_ratios"]["major_defect_count"] == 1
    assert result["payload"]["verdict"]["value"] == "INCONCLUSIVE_DATA"


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
    snapshot = SnapshotEvidence.from_identity(
        contract=contract, identity=matching)
    assert snapshot.snapshot_file_sha256 == expected["snapshot_file_sha256"]

    mismatch = replace(
        matching,
        checks=(
            replace(matching.checks[0], actual="bad", status="mismatch"),
            *matching.checks[1:],
        ),
        ok=False,
    )
    with pytest.raises(ValueError, match="전부 match"):
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
    with pytest.raises(ValueError, match="동결 계약 hash"):
        SnapshotEvidence.from_identity(
            contract=contract, identity=falsely_marked_match)
