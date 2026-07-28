from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

import src.research.orb.development as development_module
from src.research.orb.contract import all_specs, load_contract, universe
from src.research.orb.development import DevelopmentPaths, run_development
from src.research.orb.gates import SnapshotDefectEvidence
from src.research.orb.identity import IdentityCheck, IdentityResult
from src.research.orb.manifest import (
    IMPLEMENTATION_MERGE_COMMIT,
    PreconditionEvidence,
)
from src.research.orb.simulator import DaySimulationResult, FeaturePass


def _preconditions() -> PreconditionEvidence:
    return PreconditionEvidence(
        implementation_merge_commit=IMPLEMENTATION_MERGE_COMMIT,
        review_status="approved",
        review_evidence_url=(
            "https://github.com/peg5325/ai-trading-system/pull/93"
            "#issuecomment-5100125967"
        ),
        loader_boundary_status="passed",
        loader_boundary_test=(
            "tests/test_oos_guard_wiring.py::"
            "test_no_direct_load_candles_outside_gate"
        ),
    )


def _matching_identity() -> IdentityResult:
    identity = load_contract()["data_identity"]
    pairs = (
        ("snapshot_file", "snapshot_file_sha256"),
        ("snapshot_content", "snapshot_content_sha256"),
        ("manifest_data", "manifest_data_sha256"),
        ("raw_content", "raw_content_sha256"),
    )
    return IdentityResult(
        checks=tuple(
            IdentityCheck(name, identity[key], identity[key], "match")
            for name, key in pairs
        ),
        ok=True,
        verdict=None,
    )


def test_run_development_wires_identity_feature_simulation_and_manifest(
    monkeypatch,
    tmp_path,
):
    contract = load_contract()
    day = date(2024, 3, 4)
    manifest = {
        "data_content_sha256": (
            contract["data_identity"]["manifest_data_sha256"]
        ),
        "symbols": [
            {"symbol": symbol, "conflict": 0}
            for symbol in sorted(universe(contract).all_symbols)
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    identity = _matching_identity()
    calls = []

    monkeypatch.setattr(
        development_module,
        "verify_snapshot_identity",
        lambda **kwargs: calls.append(("identity", kwargs)) or identity,
    )
    monkeypatch.setattr(
        development_module,
        "require_identity",
        lambda value: calls.append(("require", value)),
    )
    empty_pass = FeaturePass(
        days=(),
        symbols=(),
        by_opening_range_minutes={},
        decision_bars_by_day={},
        defects_by_day={},
    )
    monkeypatch.setattr(
        development_module,
        "build_development_feature_pass",
        lambda **kwargs: calls.append(("features", kwargs)) or empty_pass,
    )
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
        for spec in all_specs(contract)
    }
    monkeypatch.setattr(
        development_module,
        "simulate_all_specs",
        lambda **kwargs: calls.append(("simulate", kwargs)) or results,
    )
    monkeypatch.setattr(
        development_module,
        "SnapshotDefectEvidence",
        type(
            "FakeSnapshotDefectEvidence",
            (),
            {
                "from_sources": staticmethod(
                    lambda **_kwargs: SnapshotDefectEvidence(28, ())
                )
            },
        ),
    )
    monkeypatch.setattr(
        development_module,
        "build_result",
        lambda evidence, **kwargs: {
            "producer": evidence.producer_commit,
            "generated": kwargs["generated_at_utc"],
        },
    )

    run = run_development(
        paths=DevelopmentPaths(
            snapshot_path="/snapshot.sqlite",
            raw_root="/raw",
            manifest_path=str(manifest_path),
        ),
        producer_commit="a" * 40,
        generated_at_utc="2026-07-28T08:00:00Z",
        preconditions=_preconditions(),
    )
    assert run.result["producer"] == "a" * 40
    assert [name for name, _ in calls] == [
        "identity",
        "require",
        "features",
        "simulate",
    ]


def test_generated_time_and_review_evidence_are_fail_closed(tmp_path):
    paths = DevelopmentPaths(
        snapshot_path="/snapshot.sqlite",
        raw_root="/raw",
        manifest_path=str(tmp_path / "missing.json"),
    )
    with pytest.raises(ValueError, match="UTC Z"):
        run_development(
            paths=paths,
            producer_commit="a" * 40,
            generated_at_utc="now",
            preconditions=_preconditions(),
        )
    with pytest.raises(ValueError, match="PR #93"):
        run_development(
            paths=paths,
            producer_commit="a" * 40,
            generated_at_utc="2026-07-28T08:00:00Z",
            preconditions=replace(
                _preconditions(),
                review_evidence_url="https://example.com/review",
            ),
        )
