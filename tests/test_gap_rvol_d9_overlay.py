from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from src.research.gap_rvol.d9_overlay import (
    CandidateIdentity,
    build_overlay_audit,
    overlay_loader,
    verify_candidate_identity,
)
from src.research.gap_rvol.development import FeaturePass


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
    with pytest.raises(ValueError, match="content hash mismatch"):
        verify_candidate_identity(
            manifest_path=str(path),
            candidate_db=str(db),
            candidate_raw_root=str(tmp_path / "raw"),
        )


def test_overlay_runner_is_registered_but_result_not_yet_present():
    assert Path("scripts/audit_gap_rvol_d9_overlay.py").is_file()
    assert not Path("docs/research/AUDIT_P0_US30_GAP_RVOL_D9_OVERLAY.json").exists()
