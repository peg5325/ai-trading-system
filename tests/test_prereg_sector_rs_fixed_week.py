from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "docs/research/PREREG_P0_SECTOR_RS_FIXED_WEEK_V1.json"
MD_PATH = ROOT / "docs/research/PREREG_P0_SECTOR_RS_FIXED_WEEK_V1.md"


def _load() -> dict:
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


def test_prereg_is_frozen_exploratory_and_does_not_open_oos() -> None:
    contract = _load()
    assert contract["status"] == "FROZEN_BEFORE_THIS_STRATEGY_RESULTS"
    assert contract["adaptation_disclosure"] == {
        "prior_strategy_result_examined": True,
        "prior_result_path": (
            "docs/research/RESULT_P0_SECTOR_ETF_WEEKLY_MOMENTUM_DEVELOPMENT.json"
        ),
        "prior_result_verdict": "NO_SELECTION",
        "current_strategy_results_examined": False,
        "same_development_data_is_exploratory_only": True,
        "blind_oos_or_paper_promotion_allowed": False,
    }
    assert contract["discipline"]["implementation_or_result_in_this_pr"] is False
    assert contract["discipline"]["blind_oos_opened"] is False
    assert contract["discipline"]["oos_grant_allowed"] is False
    assert contract["discipline"]["paper_or_live_promotion_allowed"] is False


def test_signal_execution_and_portfolio_are_exactly_fixed() -> None:
    contract = _load()
    assert contract["data"]["symbols"] == [
        "SMH", "XLE", "XLF", "XLI", "XLK", "XLU", "XLV", "XLY"
    ]
    signal = contract["signal"]
    assert signal["primary_lookbacks"] == [63, 126]
    assert signal["skip_recent_trading_days"] == 5
    assert signal["primary_top_n"] == 3
    assert signal["sensitivity_top_n"] == 3
    execution = contract["execution"]
    assert execution["entry"] == "first trading day of next ISO week open"
    assert execution["max_holding_trading_days"] == 5
    assert execution["cross_iso_week_carry"] is False
    assert execution["stop"] is None and execution["target"] is None
    portfolio = contract["portfolio"]
    assert portfolio["primary_max_positions"] == 3
    assert portfolio["gross_target_notional_usd"] == 50_000
    assert portfolio["baseline_position_entry_notional_usd"] * 8 == 50_000


def test_data_and_evidence_hashes_are_current() -> None:
    contract = _load()
    for key, path_key in (
        ("ledger_sha256", "ledger_path"),
        ("manifest_file_sha256", "manifest_path"),
    ):
        container = (
            contract["evidence_boundary"]
            if key == "ledger_sha256"
            else contract["data"]
        )
        path = ROOT / container[path_key]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == container[key]
    assert contract["data"]["development_end_inclusive"] == "2025-07-18"
    assert contract["data"]["blind_oos_start"] == "2025-07-21"
    assert contract["data"]["live_hash_policy"] == "development-end-bounded-only"


def test_verdicts_are_exploratory_and_result_artifacts_do_not_exist() -> None:
    contract = _load()
    assert contract["evaluation"]["verdicts"] == {
        "data_gate_failure": "INCONCLUSIVE_DATA",
        "economic_gate_failure": "EXPLORATORY_NO_SELECTION",
        "all_pass": "EXPLORATORY_GO_REQUIRES_FRESH_VALIDATION",
    }
    assert contract["discipline"]["positive_result_requires"] == (
        "fresh user-approved preregistration on untouched forward data"
    )
    assert not (
        ROOT / "docs/research/RESULT_P0_SECTOR_RS_FIXED_WEEK_DEVELOPMENT.json"
    ).exists()
    assert not (
        ROOT / "docs/research/RESULT_P0_SECTOR_RS_FIXED_WEEK_DEVELOPMENT.md"
    ).exists()
    assert "blind OOS 개봉은 이 PR에서 하지 않는다" in MD_PATH.read_text(
        encoding="utf-8"
    )
