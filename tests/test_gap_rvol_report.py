from __future__ import annotations

import json

import pytest

from src.research.gap_rvol.report import (
    render_development_report,
    result_json_bytes,
    write_evidence_file,
)


def _stats() -> dict:
    return {
        "trade_count": 1,
        "trading_days_with_trade": 1,
        "cumulative_net_pnl": -1.25,
        "net_expectancy": -1.25,
        "profit_factor": 0.0,
        "mdd": 0.01,
        "positive_folds": 0,
    }


def _result() -> dict:
    spec = {
        "name": "primary",
        "authority": "decides_go_nogo",
        "params": {
            "gap_threshold": 0.003,
            "rvol_threshold": 1.5,
            "retention_fraction": 0.5,
            "target_r_multiple": 2.0,
        },
        "stats": {"official": _stats()},
    }
    return {
        "generated_at_utc": "2026-07-30T08:00:00Z",
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
                "P1": {"prereg_merge_commit": "d" * 40},
                "P2": {
                    "implementation_merge_commit": "b" * 40,
                    "implementation_review_evidence_url": "https://github.com/impl",
                    "implementation_map_status": "tracked",
                },
                "P3": {
                    "runner_reviewed_commit": "c" * 40,
                    "runner_code_review_status": "approved",
                    "runner_architect_status": "clear",
                    "runner_review_evidence_url": "https://github.com/x",
                },
                "P4": {
                    "snapshot_identity": "matched_all_four_hashes",
                    "tracked_manifest": "docs/research/manifest.json",
                    "tracked_manifest_file_sha256": "e" * 64,
                },
                "P5": {
                    "loader_boundary_status": "passed",
                    "loader_boundary_test": "test",
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
                "low": _stats(),
                "official": _stats(),
                "stress": _stats(),
            },
            "defect_ratios": {
                "data": {"numerator": 0, "denominator": 1, "ratio": 0.0},
                "operational": {
                    "numerator": 0,
                    "denominator": 1,
                    "ratio": 0.0,
                },
                "major_defect_count": 0,
                "conflict_total": 0,
                "identity_failures": [],
            },
            "excluded": [],
            "filtered": [],
            "skipped": [],
            "window": {
                "data_start": "2023-04-20",
                "data_end": "2026-01-20",
                "data_trading_days": 690,
                "eval_start": "2023-05-18",
                "eval_end": "2026-01-20",
                "eval_trading_days": 670,
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
    assert "P3 runner code-reviewer/architect: `approved` / `clear`" in first
    assert "blind OOS opened: `False`" in first
    assert "primary" in first


def test_evidence_writer_reuses_identical_and_refuses_divergent(tmp_path):
    target = tmp_path / "result.json"
    write_evidence_file(str(target), b"same")
    write_evidence_file(str(target), b"same")
    with pytest.raises(FileExistsError, match="differs"):
        write_evidence_file(str(target), b"different")
