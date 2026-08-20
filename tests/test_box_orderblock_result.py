from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.research.box_orderblock_retest.contract import canonical_json_bytes
from src.research.box_orderblock_retest.report import render_markdown

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "docs/research/RESULT_P0_US30_BOX_ORDERBLOCK_RETEST_DEVELOPMENT.json"
MD_PATH = ROOT / "docs/research/RESULT_P0_US30_BOX_ORDERBLOCK_RETEST_DEVELOPMENT.md"


def _result() -> dict:
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


def test_result_payload_and_markdown_are_deterministic() -> None:
    result = _result()
    payload = dict(result)
    expected = payload.pop("payload_sha256")
    assert hashlib.sha256(canonical_json_bytes(payload)).hexdigest() == expected
    assert expected == "fd0f2cbc38e08ad4f2949aee6199a86963e14e6abedb4921fc106cee6af9e230"
    assert render_markdown(result) == MD_PATH.read_text(encoding="utf-8")
    assert hashlib.sha256(JSON_PATH.read_bytes()).hexdigest() == (
        "bd6482714c917bc938e4a98707a2b0448c8ef28886b7d73f79fb17c7a2773de3"
    )


def test_result_is_review_bound_exploratory_and_does_not_open_oos() -> None:
    result = _result()
    assert result["producer_commit"] == (
        "7d7952e41a0655cb093e9ad8daba4378ef02a613"
    )
    assert result["review_evidence_url"] == (
        "https://github.com/peg5325/ai-trading-system/pull/143"
        "#issuecomment-5353295290"
    )
    assert result["scope"] == "exploratory-development-only"
    assert result["blind_oos_opened"] is False
    assert result["promotion_allowed"] is False
    assert all(item["status"] == "match" for item in result["identity"])
    assert result["execution_audit"] == [
        {
            "phase": "post_result_artifact_reproduction_test",
            "issue": "markdown_used_python_mapping_repr_with_key_type_and_order_drift",
            "resolution": "canonical_sorted_json_for_inline_mapping_rendering",
            "performance_values_inspected_before_fix": True,
            "strategy_rules_changed": False,
            "result_regenerated": True,
        },
        {
            "phase": "second_post_result_artifact_reproduction_test",
            "issue": "markdown_gate_iteration_used_insertion_order_before_sorted_json_reload",
            "resolution": "sorted_data_and_economic_gate_rendering",
            "performance_values_inspected_before_fix": True,
            "strategy_rules_changed": False,
            "result_regenerated": True,
        }
    ]


def test_result_preserves_inconclusive_data_and_sample_failure() -> None:
    result = _result()
    assert result["verdict"] == "INCONCLUSIVE_DATA"
    assert result["signal_count"] == 42
    assert result["signal_audit"]["order_block_signals"] == 3
    assert result["arms"]["base"]["candidate_audit"]["prepared_candidates"] == 41
    assert result["arms"]["primary"]["candidate_audit"]["prepared_candidates"] == 1
    assert result["arms"]["primary"]["cost_levels"]["official"][
        "trade_count"
    ] == 1
    assert result["arms"]["primary"]["cost_levels"]["official"][
        "cumulative_net_pnl"
    ] == pytest.approx(-135.47750459759854)
    assert result["arms"]["primary"]["positive_folds_official"] == 0
    assert result["defects"]["invalid_daily_bar"] == 3
    assert result["data_gates"]["DQ5_major_defects_eq_0"] is False
    assert result["economic_gates"]["D1_min_trades"] is False
    assert result["economic_gates"]["D3_official_expectancy_gt_0"] is False


def test_result_contains_required_audits() -> None:
    result = _result()
    assert set(result["signal_audit"]) >= {
        "warmup_not_ready",
        "breakout_signals",
        "order_block_signals",
        "mean_box_width_fraction",
        "mean_atr_contraction_ratio",
        "mean_volume_ratio",
        "retest_delay_days",
    }
    for arm in ("base", "primary", "partial"):
        official = result["arms"][arm]["cost_levels"]["official"]
        assert set(official) >= {
            "entry_turnover_notional",
            "exit_turnover_notional",
            "total_cost",
            "holding_days_mean",
            "holding_days_median",
            "max_consecutive_losses",
        }
        assert "exit_reasons" in result["arms"][arm]
