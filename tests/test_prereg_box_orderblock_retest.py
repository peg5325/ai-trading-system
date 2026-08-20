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
    assert contract["version"] == "1.2"
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
    assert "broad diagnostic only" in contract["arms"]["base_diagnostic"][
        "comparison_interpretation"
    ]
    partial = contract["arms"]["partial_exit_sensitivity"]
    assert partial["candidate_and_entry_set"] == "identical to primary"
    assert partial["same_day_stop_and_target"] == (
        "assume full stop occurs first even when open is at or above target"
    )
    assert partial["daily_priority"][:3] == [
        "gap_stop_for_current_active_stop",
        "if_low_lte_stop_and_max_open_high_gte_target_assume_full_stop_first",
        "otherwise_intraday_stop",
    ]
    assert partial["daily_priority"].index(
        "if_low_lte_stop_and_max_open_high_gte_target_assume_full_stop_first"
    ) < partial["daily_priority"].index(
        "if_open_gte_target_fill_half_at_fixed_target_price"
    )
    assert partial["target_gap_fill"] == (
        "conservative fixed target price, not favorable open"
    )
    assert partial["target_on_time_exit_day"] == (
        "sell half at target then sell remaining half at same-day close when stop was not touched"
    )
    assert partial["promotion_authority"] is False
    assert contract["evaluation"]["multiple_arm_policy"] == (
        "primary alone owns verdict; base and partial are fixed diagnostics; base is not an incremental-value gate"
    )


def test_execution_risk_costs_and_verdicts_are_frozen() -> None:
    contract = _contract()
    execution = contract["execution"]
    assert execution["primary_time_exit"] == "fifth held trading day close"
    assert execution["entry_day_counts_as_holding_day_one"] is True
    assert execution["stop_precedes_time_exit"] is True
    assert contract["portfolio"]["risk_budget_usd"] == 500
    assert contract["portfolio"]["max_positions"] == 5
    assert contract["portfolio"]["partial_exit_fractional_shares_research_only"] is True
    assert "exactly 50 percent" in contract["portfolio"]["partial_exit_quantity"]
    assert execution["entry_lte_initial_stop"].startswith("skip")
    assert contract["costs_round_trip_pct_points"]["official"] == 0.34
    assert contract["evaluation"]["verdicts"] == {
        "data_gate_failure": "INCONCLUSIVE_DATA",
        "economic_gate_failure": "EXPLORATORY_NO_SELECTION",
        "all_pass": "EXPLORATORY_GO_REQUIRES_FRESH_VALIDATION",
    }


def test_source_hashes_match_and_no_result_exists() -> None:
    contract = _contract()
    for container, hash_key, path_key in (
        (contract["evidence_boundary"], "ledger_sha256", "ledger_path"),
        (contract["data"], "manifest_file_sha256", "manifest_path"),
    ):
        path = ROOT / container[path_key]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == container[hash_key]
    assert not (
        ROOT / "docs/research/RESULT_P0_US30_BOX_ORDERBLOCK_RETEST_DEVELOPMENT.json"
    ).exists()


def test_fold_boundaries_and_data_quality_denominator_are_exact() -> None:
    evaluation = _contract()["evaluation"]
    assert evaluation["fold_widths"] == [235] * 7 + [234] * 3
    assert evaluation["fold_boundaries"] == [
        {"fold": 1, "start": "2016-03-18", "end": "2017-02-22"},
        {"fold": 2, "start": "2017-02-23", "end": "2018-01-29"},
        {"fold": 3, "start": "2018-01-30", "end": "2019-01-04"},
        {"fold": 4, "start": "2019-01-07", "end": "2019-12-10"},
        {"fold": 5, "start": "2019-12-11", "end": "2020-11-13"},
        {"fold": 6, "start": "2020-11-16", "end": "2021-10-21"},
        {"fold": 7, "start": "2021-10-22", "end": "2022-09-28"},
        {"fold": 8, "start": "2022-09-29", "end": "2023-09-05"},
        {"fold": 9, "start": "2023-09-06", "end": "2024-08-09"},
        {"fold": 10, "start": "2024-08-12", "end": "2025-07-18"},
    ]
    taxonomy = evaluation["data_quality_taxonomy"]
    assert taxonomy["numerator"] == "data_exclusions"
    assert taxonomy["denominator"] == "prepared_candidates+data_exclusions"
    assert taxonomy["inclusive_threshold"] is True
    assert "excluded from denominator" in taxonomy["warmup"]
    assert "excluded from denominator" in taxonomy[
        "development_or_fold_boundary"
    ]
    assert taxonomy["arm_rates"] == (
        "compute primary and base separately; partial shares primary candidates"
    )
    assert taxonomy["gate_rate"] == "max(primary_rate,base_rate)"


def test_indicator_conventions_and_pre_result_revision_are_frozen() -> None:
    contract = _contract()
    indicators = contract["indicator_conventions"]
    assert indicators["true_range"].startswith("max(high-low")
    assert indicators["atr"] == "simple arithmetic mean of true range"
    assert indicators["atr_pre_windows"] == (
        "ATR5 and ATR20 use only t-5:t-1 and t-20:t-1 true ranges"
    )
    assert "not-ready until 50 consecutive valid bars" in indicators["ema50"]
    assert "200 consecutive valid SPY closes" in indicators["spy_sma200"]
    assert "fixed t-20:t-1 bars" in indicators["volume_median20"]
    assert "all 20 volumes must be positive and non-missing" in indicators[
        "volume_median20"
    ]
    assert "never reach back before t-20" in indicators["volume_median20"]
    assert indicators["missing_or_invalid_daily_bar"].startswith("break segment")
    revision = contract["revision_history"][-1]
    assert revision["version"] == "1.2"
    assert "before signal simulator" in revision["timing"]
    assert revision["go_easier"] is False
