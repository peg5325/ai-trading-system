from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "docs/research/PREREG_P0_US30_BOX_ORDERBLOCK_RETEST_V1.json"


def _contract() -> dict:
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


def test_adaptive_scope_is_exploratory_and_oos_stays_closed() -> None:
    contract = _contract()
    assert contract["status"] == "FROZEN_BEFORE_THIS_STRATEGY_RESULTS"
    disclosure = contract["adaptation_disclosure"]
    assert disclosure["prior_box_and_breakout_results_examined"] is True
    assert disclosure["exact_video_replication_claimed"] is False
    assert disclosure["current_strategy_results_examined"] is False
    assert disclosure["same_development_data_is_exploratory_only"] is True
    discipline = contract["discipline"]
    assert discipline["blind_oos_opened"] is False
    assert discipline["oos_grant_allowed"] is False
    assert discipline["paper_or_live_promotion_allowed"] is False


def test_box_breakout_orderblock_and_retest_are_exact() -> None:
    contract = _contract()
    assert contract["box"] == {
        "lookback_trading_days": 20,
        "window": "t-20 through t-1 inclusive",
        "box_high": "max(high[t-20:t-1])",
        "box_low": "min(low[t-20:t-1])",
        "max_width_fraction_of_previous_close": 0.12,
        "atr_fast_period": 5,
        "atr_slow_period": 20,
        "atr_window_ends": "t-1",
        "volatility_contraction": "atr5_pre_lte_0.80_times_atr20_pre",
    }
    breakout = contract["breakout"]
    assert breakout["minimum_volume_ratio"] == 1.5
    assert breakout["minimum_breakout"] == (
        "close_t_gt_box_high_plus_0.10_atr20_pre"
    )
    assert breakout["maximum_extension"] == (
        "close_t_lte_box_high_plus_1.00_atr20_pre"
    )
    order_block = contract["order_block"]
    assert order_block["source_candle"] == "t-1"
    assert order_block["retest_search_days"] == 3
    assert order_block["retest_first_match"] is True
    assert order_block["entry"] == (
        "next trading day open after first confirmed retest"
    )


def test_primary_owns_verdict_and_diagnostics_cannot_replace_it() -> None:
    contract = _contract()
    assert contract["arms"]["primary"] == "order_block_retest_fixed5"
    assert contract["arms"]["base_diagnostic"]["promotion_authority"] is False
    partial = contract["arms"]["partial_exit_sensitivity"]
    assert partial["candidate_and_entry_set"] == "identical to primary"
    assert partial["same_day_stop_and_target"] == (
        "assume full stop occurs first"
    )
    assert partial["promotion_authority"] is False
    assert contract["evaluation"]["multiple_arm_policy"] == (
        "primary alone owns verdict; base and partial are fixed diagnostics"
    )


def test_execution_risk_costs_and_verdicts_are_frozen() -> None:
    contract = _contract()
    execution = contract["execution"]
    assert execution["primary_time_exit"] == "fifth held trading day close"
    assert execution["entry_day_counts_as_holding_day_one"] is True
    assert execution["stop_precedes_time_exit"] is True
    assert contract["portfolio"]["risk_budget_usd"] == 500
    assert contract["portfolio"]["max_positions"] == 5
    assert contract["costs_round_trip_pct_points"]["official"] == 0.34
    assert contract["evaluation"]["verdicts"] == {
        "data_gate_failure": "INCONCLUSIVE_DATA",
        "economic_gate_failure": "EXPLORATORY_NO_SELECTION",
        "all_pass": "EXPLORATORY_GO_REQUIRES_FRESH_VALIDATION",
    }


def test_source_hashes_match_and_no_result_or_implementation_exists() -> None:
    contract = _contract()
    for container, hash_key, path_key in (
        (contract["evidence_boundary"], "ledger_sha256", "ledger_path"),
        (contract["data"], "manifest_file_sha256", "manifest_path"),
    ):
        path = ROOT / container[path_key]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == container[hash_key]
    assert not (ROOT / "src/research/box_orderblock_retest").exists()
    assert not (
        ROOT / "docs/research/RESULT_P0_US30_BOX_ORDERBLOCK_RETEST_DEVELOPMENT.json"
    ).exists()
