from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.research.gross_edge.report import render_markdown
from src.research.gross_edge.run import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "docs/research/RESULT_P0_US30_GROSS_EDGE_SCREEN.json"
MD_PATH = ROOT / "docs/research/RESULT_P0_US30_GROSS_EDGE_SCREEN.md"


def load_result() -> dict[str, object]:
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


def test_result_payload_hash_and_markdown_are_exactly_reproducible() -> None:
    result = load_result()
    expected_hash = result.pop("payload_sha256")
    assert hashlib.sha256(canonical_json_bytes(result)).hexdigest() == expected_hash
    result["payload_sha256"] = expected_hash
    assert canonical_json_bytes(result) == JSON_PATH.read_bytes()
    assert render_markdown(result) == MD_PATH.read_text(encoding="utf-8")


def test_result_keeps_oos_locked_and_all_four_identity_checks_match() -> None:
    result = load_result()
    assert result["blind_oos_accessed"] is False
    assert result["development_window"] == {
        "start": "2023-05-18", "end": "2026-01-20", "trading_days": 670,
    }
    assert {item["status"] for item in result["data_identity"].values()} == {"match"}


def test_result_candidate_pairs_are_exact_opposites() -> None:
    candidates = {item["candidate_id"]: item for item in load_result()["candidates"]}
    for family in ("gap", "opening_30m", "first_hour", "morning"):
        continuation = candidates[f"{family}_continuation"]
        reversal = candidates[f"{family}_reversal"]
        assert continuation["trade_count"] == reversal["trade_count"]
        assert continuation["trading_days"] == reversal["trading_days"]
        assert continuation["mean_daily_gross_pct_points"] == pytest.approx(
            -reversal["mean_daily_gross_pct_points"], abs=1e-15
        )


def test_no_candidate_passes_gross_or_cost_gate_and_verdict_is_consistent() -> None:
    result = load_result()
    assert result["verdict"] == "NO_GROSS_EDGE"
    assert result["shortlisted_candidates"] == []
    assert result["gross_edge_only_candidates"] == []
    assert not any(item["gross_edge_pass"] for item in result["candidates"])
    assert not any(item["economic_pass"] for item in result["candidates"])


def test_first_hour_continuation_is_nearest_but_not_a_hidden_pass() -> None:
    candidates = {item["candidate_id"]: item for item in load_result()["candidates"]}
    candidate = candidates["first_hour_continuation"]
    assert candidate["positive_folds"] == 9
    assert candidate["sign_test_bonferroni_p"] == pytest.approx(0.050420413817741844)
    assert candidate["gross_edge_gates"]["G5_bonferroni_significance"] is False
    assert candidate["net_after_cost_pct_points"]["published_floor"] < 0

