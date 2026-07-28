from __future__ import annotations

import json

import pytest

from src.research.orb.report import (
    render_development_report,
    result_json_bytes,
    write_evidence_file,
)


def _result() -> dict:
    stats = {
        "trade_count": 1,
        "trading_days_with_trade": 1,
        "cumulative_net_pnl": -1.25,
        "net_expectancy": -1.25,
        "profit_factor": 0.0,
        "mdd": 0.01,
        "positive_folds": 0,
    }
    spec = {
        "name": "primary",
        "authority": "decides_go_nogo",
        "params": {
            "opening_range_minutes": 15,
            "gap_threshold_abs": 0.003,
            "rvol_threshold": 1.5,
            "target_r_multiple": 2.0,
        },
        "stats": {"official": stats},
    }
    return {
        "generated_at_utc": "2026-07-28T08:00:00Z",
        "payload_sha256": "a" * 64,
        "payload": {
            "verdict": {
                "value": "NO_SELECTION",
                "failed": ["D1"],
                "next_stage": None,
            },
            "dev_gate": {
                "development_gate": "FAIL",
                "items": [
                    {
                        "name": "D1",
                        "criterion": ">=536",
                        "actual": 1,
                        "passed": False,
                    }
                ],
            },
            "preconditions": {
                "P1": {
                    "review_status": "approved",
                    "review_evidence_url": "https://github.com/x",
                    "implementation_merge_commit": "b" * 40,
                },
                "P2": {
                    "loader_boundary_status": "passed",
                    "loader_boundary_test": "test",
                },
                "P3": {
                    "snapshot_identity": "matched_all_four_hashes",
                    "tracked_manifest": "docs/research/manifest.json",
                    "tracked_manifest_file_sha256": "e" * 64,
                },
            },
            "snapshot": {
                "basename": "snapshot.sqlite",
                "snapshot_file_sha256": "1" * 64,
                "snapshot_content_sha256": "2" * 64,
                "manifest_data_sha256": "3" * 64,
                "raw_content_sha256": "4" * 64,
            },
            "producer_commit": "c" * 40,
            "prereg": {"prereg_merge_commit": "d" * 40},
            "specs": [spec],
            "stats": {
                "low": stats,
                "official": stats,
                "stress": stats,
            },
            "defect_ratios": {
                "data": {"numerator": 0, "denominator": 1, "ratio": 0.0},
                "operational": {
                    "numerator": 0,
                    "denominator": 1,
                    "ratio": 0.0,
                },
                "major_defect_count": 0,
                "conflict_total": 28,
            },
            "excluded": [],
            "skipped": [],
            "window": {
                "start": "2023-05-18",
                "end": "2026-01-20",
                "trading_days": 670,
            },
            "loader": {"purpose": "performance", "track": "intraday"},
            "single_pass": {"oos_opened": False},
        },
    }


def test_report_is_deterministic_and_discloses_all_safety_evidence():
    result = _result()
    first = render_development_report(result)
    second = render_development_report(json.loads(result_json_bytes(result)))
    assert first == second
    assert "NO_SELECTION" in first
    assert "P1 review: `approved`" in first
    assert "blind OOS opened: `False`" in first
    assert "primary" in first


def test_evidence_writer_reuses_identical_and_refuses_divergent(tmp_path):
    target = tmp_path / "result.json"
    write_evidence_file(str(target), b"same")
    write_evidence_file(str(target), b"same")
    with pytest.raises(FileExistsError, match="내용이 다르다"):
        write_evidence_file(str(target), b"different")
