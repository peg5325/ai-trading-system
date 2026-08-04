from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from src.history.raw_snapshot import RawSnapshotStore
from src.research.gap_rvol.d9_overlay import (
    CandidateIdentity,
    audit_bytes,
    build_overlay_audit,
    overlay_loader,
    render_overlay_audit,
    verify_candidate_identity,
)
from src.research.gap_rvol.development import FeaturePass
from src.research.gap_rvol.provider_repair import data_content_sha256_path


class Feature:
    def __init__(self, reasons=()):
        self.excluded_reasons = tuple(reasons)


def _feature_pass(reasons):
    day = date(2024, 8, 5)
    return FeaturePass(
        data_days=(day,),
        eval_days=(day,),
        symbols=("SPY",),
        features_by_spec={"primary": {day: {"SPY": Feature(reasons)}}},
        decision_bars_by_day={},
        defects_by_day={},
    )


def _identity():
    return CandidateIdentity(
        manifest_file_sha256="a" * 64,
        candidate_data_sha256="b" * 64,
        raw_content_sha256="c" * 64,
        candidate_rows=390,
        eligible_targets=(("SPY", "2024-08-05"),),
        ineligible_targets=(),
    )


def _baseline():
    return {
        "exclusion_audit": {
            "numerator": 1,
            "denominator": 1,
            "ratio": 1.0,
            "events": [{"symbol": "SPY", "day": "2024-08-05", "reasons": ["x"]}],
        },
        "corrected_d9": {"major_defect_count": 0},
    }


def test_build_overlay_audit_resolves_without_computing_performance():
    audit = build_overlay_audit(
        feature_pass=_feature_pass(()),
        baseline_audit=_baseline(),
        candidate_identity=_identity(),
        contract={"defect_policy": {"data_exclusion_ratio_lte": 0.05}},
        frozen_identity={"snapshot_file": "d" * 64},
        producer_commit="commit",
        review_evidence_url="url",
        generated_at_utc="2026-08-04T00:00:00Z",
    )
    assert audit["overlay"]["resolved_event_count"] == 1
    assert audit["corrected_d9"]["passes"] is True
    assert audit["scope"]["performance_computed"] is False
    assert audit["scope"]["blind_oos_opened"] is False


def test_overlay_audit_rejects_new_exclusion_event():
    baseline = _baseline()
    baseline["exclusion_audit"]["events"] = []
    with pytest.raises(ValueError, match="introduced new"):
        build_overlay_audit(
            feature_pass=_feature_pass(("new",)),
            baseline_audit=baseline,
            candidate_identity=_identity(),
            contract={"defect_policy": {"data_exclusion_ratio_lte": 0.05}},
            frozen_identity={},
            producer_commit="commit",
            review_evidence_url="url",
            generated_at_utc="2026-08-04T00:00:00Z",
        )


def test_overlay_audit_rejects_exclusion_divergence_across_specs():
    feature_pass = _feature_pass(())
    day = feature_pass.eval_days[0]
    feature_pass = FeaturePass(
        data_days=feature_pass.data_days,
        eval_days=feature_pass.eval_days,
        symbols=feature_pass.symbols,
        features_by_spec={
            "primary": feature_pass.features_by_spec["primary"],
            "alternate": {day: {"SPY": Feature(("still_excluded",))}},
        },
        decision_bars_by_day={},
        defects_by_day={},
    )
    with pytest.raises(ValueError, match="diverge across specs"):
        build_overlay_audit(
            feature_pass=feature_pass,
            baseline_audit=_baseline(),
            candidate_identity=_identity(),
            contract={"defect_policy": {"data_exclusion_ratio_lte": 0.05}},
            frozen_identity={},
            producer_commit="commit",
            review_evidence_url="url",
            generated_at_utc="2026-08-04T00:00:00Z",
        )


def test_overlay_loader_uses_candidate_only_for_eligible(monkeypatch):
    calls = []

    def fake(db, symbol, day):
        calls.append(db)
        return (db, symbol, day)

    monkeypatch.setattr("src.research.gap_rvol.d9_overlay.load_symbol_day", fake)
    load = overlay_loader(
        candidate_db="candidate.sqlite",
        eligible_targets=(("SPY", "2024-08-05"),),
    )
    assert load("frozen.sqlite", "SPY", date(2024, 8, 5))[0] == "candidate.sqlite"
    assert load("frozen.sqlite", "QQQ", date(2024, 8, 5))[0] == "frozen.sqlite"
    assert calls == ["candidate.sqlite", "frozen.sqlite"]


def test_candidate_identity_rejects_hash_mismatch(tmp_path):
    db = tmp_path / "candidate.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE candle_history(symbol TEXT,interval TEXT,ts_utc TEXT,"
        "ts_exchange TEXT,session TEXT,open REAL,high REAL,low REAL,close REAL,"
        "volume REAL,currency TEXT,adjusted INTEGER,source TEXT)"
    )
    con.execute("CREATE TABLE import_conflict(x INTEGER)")
    con.commit()
    con.close()
    manifest = {
        "schema": "gap-rvol-d9-toss-repair-candidate/v1",
        "repair_db_data_content_sha256": "bad",
        "raw_content_sha256": "bad",
        "raw_content_file_count": 0,
        "repair_db_rows": 0,
        "full_regular_session_count": 0,
        "targets": [],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest file hash mismatch"):
        verify_candidate_identity(
            manifest_path=str(path),
            candidate_db=str(db),
            candidate_raw_root=str(tmp_path / "raw"),
        )


def test_candidate_identity_freezes_exact_76_66_10_counts(tmp_path, monkeypatch):
    db = tmp_path / "candidate.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE candle_history(symbol TEXT,interval TEXT,ts_utc TEXT,"
        "ts_exchange TEXT,session TEXT,open REAL,high REAL,low REAL,close REAL,"
        "volume REAL,currency TEXT,adjusted INTEGER,source TEXT)"
    )
    con.execute("CREATE TABLE import_conflict(x INTEGER)")
    con.commit()
    con.close()
    raw = RawSnapshotStore(str(tmp_path / "raw")).manifest()
    manifest = {
        "schema": "gap-rvol-d9-toss-repair-candidate/v1",
        "repair_db_data_content_sha256": data_content_sha256_path(str(db)),
        "raw_content_sha256": raw["raw_content_sha256"],
        "raw_content_file_count": 0,
        "repair_db_rows": 0,
        "target_count": 75,
        "full_regular_session_count": 66,
        "required_feature_minutes_complete_count": 66,
        "targets": [],
    }
    path = tmp_path / "manifest.json"
    content = json.dumps(manifest).encode()
    path.write_bytes(content)
    monkeypatch.setattr(
        "src.research.gap_rvol.d9_overlay.CANDIDATE_MANIFEST_SHA256",
        hashlib.sha256(content).hexdigest(),
    )
    with pytest.raises(ValueError, match="not frozen at 76"):
        verify_candidate_identity(
            manifest_path=str(path),
            candidate_db=str(db),
            candidate_raw_root=str(tmp_path / "raw"),
        )


def test_tracked_overlay_result_is_deterministic_and_scope_closed():
    assert Path("scripts/audit_gap_rvol_d9_overlay.py").is_file()
    json_path = Path("docs/research/AUDIT_P0_US30_GAP_RVOL_D9_OVERLAY.json")
    md_path = json_path.with_suffix(".md")
    audit = json.loads(json_path.read_text(encoding="utf-8"))
    assert audit_bytes(audit) == json_path.read_bytes()
    assert render_overlay_audit(audit) == md_path.read_text(encoding="utf-8")
    assert audit["producer_commit"] == "389e1d1bbd3a1fda593cfab2d49a0e0cab14e424"
    assert audit["overlay"]["eligible_targets"] == 66
    assert audit["overlay"]["resolved_event_count"] == 0
    assert audit["overlay"]["changed_event_count"] == 0
    assert audit["corrected_d9"]["verdict"] == "INCONCLUSIVE_DATA"
    assert audit["corrected_d9"]["data_exclusion_numerator"] == 1296
    assert audit["scope"]["blind_oos_opened"] is False
    assert audit["scope"]["performance_computed"] is False


def test_eligible_target_reasons_are_dependency_only():
    manifest = json.loads(Path(
        "docs/research/MANIFEST_P0_US30_GAP_RVOL_D9_TOSS_REPAIR_CANDIDATE.json"
    ).read_text(encoding="utf-8"))
    eligible = [
        item for item in manifest["targets"]
        if item["full_regular_session"] is True
        and item["required_feature_minutes_complete"] is True
    ]
    reasons = {reason for item in eligible for reason in item["original_reasons"]}
    assert reasons == {"insufficient_rvol_lookback", "no_previous_regular_close"}
