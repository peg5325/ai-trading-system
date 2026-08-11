from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from src.models import Candle
from src.research.gap_rvol.dependency_candidate import (
    DependencyCandidateTarget,
    build_dependency_candidate_manifest,
    build_dependency_candidate_targets,
    load_tracked_dependency_probe,
    manifest_bytes,
    render_dependency_candidate_manifest,
    summarize_dependency_collection,
    verify_dependency_candidate_identity,
)
from src.research.gap_rvol.dependency_overlay import dependency_overlay_loader
from src.research.gap_rvol.dependency_repair import load_frozen_dependency_plan
from src.research.gap_rvol.provider_repair import TargetCollection
from tests.gap_rvol_test_helpers import minute_bars


def _target(day: date) -> DependencyCandidateTarget:
    return DependencyCandidateTarget(
        day=day,
        symbol="SPY",
        roles=("rvol_15m_lookback",),
        required_by=(("2024-08-05", "insufficient_rvol_lookback"),),
    )


def test_tracked_available_dependency_intersection_is_exact_265():
    plan = load_frozen_dependency_plan(
        "docs/research/MANIFEST_P0_US30_GAP_RVOL_D9_DEPENDENCIES.json"
    )
    probe = load_tracked_dependency_probe(
        "docs/research/PROBE_P0_US30_GAP_RVOL_D9_DEPENDENCIES_TOSS.json"
    )
    targets = build_dependency_candidate_targets(plan=plan, probe=probe)
    assert len(targets) == 265
    assert {role for target in targets for role in target.roles} == {
        "rvol_15m_lookback"
    }
    assert not any("prior_regular_close" in target.roles for target in targets)


def test_dependency_summary_uses_role_specific_eligibility():
    day = date(2024, 8, 5)
    target = _target(day)
    collection = TargetCollection(
        target=target.as_repair_target(),
        candles=minute_bars(day, symbol="SPY")[:15],
        pages_fetched=1,
        http_429=0,
        stopped_reason="test",
    )
    summary = summarize_dependency_collection(collection, target)
    assert summary["has_complete_rvol_window"] is True
    assert summary["eligible_dependency"] is True
    assert summary["full_regular_session"] is False


def test_dependency_summary_rejects_incomplete_required_window():
    day = date(2024, 8, 5)
    target = _target(day)
    collection = TargetCollection(
        target=target.as_repair_target(),
        candles=minute_bars(day, symbol="SPY")[:14],
        pages_fetched=1,
        http_429=0,
        stopped_reason="test",
    )
    assert summarize_dependency_collection(collection, target)[
        "eligible_dependency"
    ] is False


def test_dependency_overlay_merges_missing_minutes(monkeypatch):
    day = date(2024, 8, 5)
    bars = minute_bars(day, symbol="SPY")
    frozen = bars[:10]
    candidate = bars[10:15]

    def fake(db, symbol, requested_day):
        assert symbol == "SPY" and requested_day == day
        return candidate if db == "candidate.sqlite" else frozen

    monkeypatch.setattr(
        "src.research.gap_rvol.dependency_overlay.load_symbol_day", fake
    )
    loader = dependency_overlay_loader(
        candidate_db="candidate.sqlite",
        eligible_targets=(("SPY", day.isoformat()),),
    )
    assert loader("frozen.sqlite", "SPY", day) == bars[:15]


def test_dependency_overlay_rejects_value_conflict(monkeypatch):
    day = date(2024, 8, 5)
    candle = minute_bars(day, symbol="SPY")[0]
    conflict = Candle(
        **{**candle.__dict__, "close": candle.close + 1.0}
    )

    def fake(db, symbol, requested_day):
        return (conflict,) if db == "candidate.sqlite" else (candle,)

    monkeypatch.setattr(
        "src.research.gap_rvol.dependency_overlay.load_symbol_day", fake
    )
    loader = dependency_overlay_loader(
        candidate_db="candidate.sqlite",
        eligible_targets=(("SPY", day.isoformat()),),
    )
    with pytest.raises(ValueError, match="conflicts with frozen"):
        loader("frozen.sqlite", "SPY", day)


def test_dependency_manifest_is_deterministic_and_scope_closed():
    plan = load_frozen_dependency_plan(
        "docs/research/MANIFEST_P0_US30_GAP_RVOL_D9_DEPENDENCIES.json"
    )
    probe = load_tracked_dependency_probe(
        "docs/research/PROBE_P0_US30_GAP_RVOL_D9_DEPENDENCIES_TOSS.json"
    )
    targets = build_dependency_candidate_targets(plan=plan, probe=probe)
    summaries = [
        {
            "day": target.day.isoformat(),
            "symbol": target.symbol,
            "http_429": 0,
            "eligible_dependency": True,
        }
        for target in targets
    ]
    manifest = build_dependency_candidate_manifest(
        targets=targets,
        summaries=summaries,
        raw_manifest={"content_file_count": 1, "raw_content_sha256": "a" * 64},
        repair_db_content_sha256="b" * 64,
        repair_db_rows=1,
        generated_at_utc="2026-08-11T00:00:00Z",
    )
    assert manifest["target_count"] == 265
    assert manifest["eligible_dependency_count"] == 265
    assert manifest["scope"]["blind_oos_opened"] is False
    assert manifest["scope"]["performance_computed"] is False
    assert manifest_bytes(manifest) == manifest_bytes(manifest)


def test_dependency_scripts_are_tracked():
    assert Path("scripts/collect_gap_rvol_d9_dependency_candidates.py").is_file()
    assert Path("scripts/audit_gap_rvol_d9_dependency_overlay.py").is_file()


def test_tracked_dependency_candidate_is_exact_and_scope_closed_when_present():
    path = Path(
        "docs/research/MANIFEST_P0_US30_GAP_RVOL_D9_TOSS_DEPENDENCY_CANDIDATE.json"
    )
    if not path.exists():
        pytest.skip("live dependency candidate collection has not run")
    manifest = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert manifest_bytes(manifest) == path.read_bytes()
    assert manifest["target_count"] == 265
    assert manifest["eligible_dependency_count"] == 0
    assert manifest["ineligible_dependency_count"] == 265
    assert sum(item["has_complete_rvol_window"] for item in manifest["targets"]) == 0
    assert sum(item["has_usable_regular_close"] for item in manifest["targets"]) == 265
    assert manifest["scope"]["blind_oos_opened"] is False
    assert manifest["scope"]["performance_computed"] is False
    md = path.with_suffix(".md")
    assert render_dependency_candidate_manifest(manifest) == md.read_text(encoding="utf-8")


def _tampered_manifest(tmp_path: Path, mutate) -> Path:
    tracked = Path(
        "docs/research/MANIFEST_P0_US30_GAP_RVOL_D9_TOSS_DEPENDENCY_CANDIDATE.json"
    )
    manifest = json.loads(tracked.read_text(encoding="utf-8"))
    mutate(manifest)
    path = tmp_path / "candidate.json"
    path.write_bytes(manifest_bytes(manifest))
    return path


def test_dependency_candidate_identity_rejects_manifest_file_tampering(tmp_path):
    path = _tampered_manifest(
        tmp_path,
        lambda manifest: manifest["targets"][0].__setitem__(
            "eligible_dependency", True
        ),
    )
    with pytest.raises(ValueError, match="manifest file hash mismatch"):
        verify_dependency_candidate_identity(
            manifest_path=str(path),
            candidate_db="not-read.sqlite",
            candidate_raw_root="not-read-raw",
        )


def test_dependency_candidate_identity_recomputes_role_eligibility(
    tmp_path, monkeypatch
):
    path = _tampered_manifest(
        tmp_path,
        lambda manifest: manifest["targets"][0].__setitem__(
            "eligible_dependency", True
        ),
    )
    monkeypatch.setattr(
        "src.research.gap_rvol.dependency_candidate."
        "FROZEN_CANDIDATE_MANIFEST_FILE_SHA256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    with pytest.raises(ValueError, match="stored eligibility disagrees"):
        verify_dependency_candidate_identity(
            manifest_path=str(path),
            candidate_db="not-read.sqlite",
            candidate_raw_root="not-read-raw",
        )


def test_dependency_candidate_identity_reconciles_aggregate_counts(
    tmp_path, monkeypatch
):
    path = _tampered_manifest(
        tmp_path,
        lambda manifest: manifest.__setitem__("eligible_dependency_count", 1),
    )
    monkeypatch.setattr(
        "src.research.gap_rvol.dependency_candidate."
        "FROZEN_CANDIDATE_MANIFEST_FILE_SHA256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    with pytest.raises(ValueError, match="eligible count mismatch"):
        verify_dependency_candidate_identity(
            manifest_path=str(path),
            candidate_db="not-read.sqlite",
            candidate_raw_root="not-read-raw",
        )
