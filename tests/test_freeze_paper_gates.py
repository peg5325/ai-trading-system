from __future__ import annotations

import json
from datetime import date

import pytest

from src.freeze_paper.gates import (
    LEG_INVERSE,
    LEG_LONG,
    NO_TRADE,
    TRADE,
    evaluate_gates,
    freeze_paths,
)
from tests.freeze_paper_test_helpers import BINDING, BRIEF_DAY, watch_item, write_freeze


def _gate(reports):
    return evaluate_gates(BRIEF_DAY, reports, BINDING)


def test_planner_built_freeze_brief_passes_all_gates(tmp_path):
    write_freeze(tmp_path)
    result = _gate(tmp_path)
    assert result.decision == TRADE and result.reason_codes == ()
    assert result.leg == LEG_LONG
    assert [(c.rank, c.traded_symbol) for c in result.candidates] == [(0, "AAPL"), (1, "MSFT")]
    assert result.schedule["market_close_utc"] == "2026-03-09T20:00:00Z"
    assert result.contract_sha256 == BINDING.sha256


def test_missing_files_are_no_trade(tmp_path):
    assert _gate(tmp_path).reason_codes == ("MISSING_FREEZE_BRIEF",)
    write_freeze(tmp_path)
    freeze_paths(date.fromisoformat(BRIEF_DAY), tmp_path)[1].unlink()
    assert _gate(tmp_path).reason_codes == ("MISSING_FREEZE_RECEIPT",)


def test_brief_file_hash_must_equal_receipt(tmp_path):
    write_freeze(tmp_path, mutate_receipt=lambda r: r["artifact_file_sha256"].update(brief="0" * 64))
    result = _gate(tmp_path)
    assert result.decision == NO_TRADE
    assert "BRIEF_FILE_SHA256_MISMATCH" in result.reason_codes


def test_brief_bytes_changed_after_receipt_is_no_trade(tmp_path):
    write_freeze(tmp_path)
    brief_path = freeze_paths(date.fromisoformat(BRIEF_DAY), tmp_path)[0]
    brief_path.write_bytes(brief_path.read_bytes() + b"\n")
    assert "BRIEF_FILE_SHA256_MISMATCH" in _gate(tmp_path).reason_codes


def test_incomplete_receipt_is_no_trade(tmp_path):
    write_freeze(tmp_path, receipt_status="INCOMPLETE")
    assert "RECEIPT_NOT_COMPLETE" in _gate(tmp_path).reason_codes


def test_tampered_receipt_signature_is_no_trade(tmp_path):
    write_freeze(tmp_path)
    receipt_path = freeze_paths(date.fromisoformat(BRIEF_DAY), tmp_path)[1]
    receipt = json.loads(receipt_path.read_text())
    receipt["brief_status"] = "NO_TRADE"
    receipt_path.write_text(json.dumps(receipt))
    assert "RECEIPT_SHA256_MISMATCH" in _gate(tmp_path).reason_codes


def test_tampered_artifact_sha256_is_no_trade(tmp_path):
    write_freeze(tmp_path, mutate=lambda a: a["brief"]["watchlist"].reverse(), resign=False)
    result = _gate(tmp_path)
    assert result.decision == NO_TRADE
    assert "ARTIFACT_SHA256_MISMATCH" in result.reason_codes


@pytest.mark.parametrize(("mutation", "code"), [
    (lambda a: a.update(schema="premarket-daily-brief/v1"), "BRIEF_SCHEMA_MISMATCH"),
    (lambda a: a["brief"].update(trading_day="2026-03-10"), "BRIEF_TRADING_DAY_MISMATCH"),
    (lambda a: a["snapshot"]["payload"]["input"].update(stage="VERIFICATION"),
     "INPUT_STAGE_NOT_FREEZE"),
    (lambda a: a["snapshot"]["payload"]["input"]["provenance"].update(run_mode="REPLAY"),
     "RUN_MODE_NOT_LIVE_WINDOW"),
    (lambda a: a["snapshot"]["payload"]["input"]["sources"][0].update(status="ERROR"),
     "SOURCE_NOT_OK"),
    (lambda a: a["snapshot"]["payload"].update(quality_defects=["STALE_SOURCE"]),
     "QUALITY_DEFECTS_PRESENT"),
    (lambda a: a["brief"].update(status="NO_TRADE"), "BRIEF_NOT_SHADOW_READY"),
    (lambda a: a["brief"].update(executable=True), "BRIEF_INVARIANT_VIOLATED"),
    (lambda a: a["brief"].update(orders=[{"symbol": "AAPL"}]), "BRIEF_INVARIANT_VIOLATED"),
    (lambda a: a["brief"].update(promotion_allowed=True), "BRIEF_INVARIANT_VIOLATED"),
])
def test_every_json_path_gate_fails_closed(tmp_path, mutation, code):
    write_freeze(tmp_path, mutate=mutation)
    result = _gate(tmp_path)
    assert result.decision == NO_TRADE
    assert code in result.reason_codes
    assert result.candidates == ()


def test_uncertain_regime_is_no_trade_with_a_fixed_code(tmp_path):
    write_freeze(tmp_path, regime="UNCERTAIN")
    result = _gate(tmp_path)
    assert result.reason_codes == ("REGIME_NOT_TRADABLE",)
    assert result.regime == "UNCERTAIN"
    write_freeze(tmp_path / "odd", regime="RISK_ON; DROP")
    assert evaluate_gates(BRIEF_DAY, tmp_path / "odd", BINDING).reason_codes == (
        "REGIME_NOT_TRADABLE",)


def test_wrong_bias_for_regime_is_not_traded(tmp_path):
    write_freeze(tmp_path, regime="RISK_ON", watchlist=[watch_item("TQQQ", "SHORT_WATCH")])
    result = _gate(tmp_path)
    assert result.reason_codes == ("NO_ELIGIBLE_CANDIDATE",)
    assert result.observation_only[0]["reason"] == "BIAS_NOT_LONG_WATCH"
    write_freeze(tmp_path / "off", regime="RISK_OFF", watchlist=[watch_item("AAPL")])
    result = evaluate_gates(BRIEF_DAY, tmp_path / "off", BINDING)
    assert result.observation_only[0]["reason"] == "BIAS_NOT_SHORT_WATCH"


def test_unapproved_or_foreign_template_items_are_not_traded(tmp_path):
    write_freeze(tmp_path, watchlist=[
        watch_item("AAPL", approved=False),
        watch_item("MSFT", template="orb-v1"),
        watch_item("NVDA"),
    ])
    result = _gate(tmp_path)
    assert result.decision == TRADE
    assert [c.traded_symbol for c in result.candidates] == ["NVDA"]
    assert result.candidates[0].rank == 2
    assert [o["reason"] for o in result.observation_only] == [
        "TEMPLATE_NOT_APPROVED", "TEMPLATE_ID_MISMATCH"]


def test_more_than_three_watchlist_items_is_a_contract_defect(tmp_path):
    write_freeze(tmp_path, watchlist=[watch_item(s) for s in ("AAPL", "MSFT", "NVDA", "AMD")])
    result = _gate(tmp_path)
    assert result.decision == NO_TRADE
    assert result.reason_codes == ("WATCHLIST_EXCEEDS_MAX_CANDIDATES",)


def test_index_3x_etfs_trade_as_long_watch_but_unlisted_symbols_do_not(tmp_path):
    write_freeze(tmp_path, watchlist=[watch_item("TQQQ"), watch_item("NVDL"), watch_item("SOXL")])
    result = _gate(tmp_path)
    assert [c.traded_symbol for c in result.candidates] == ["TQQQ", "SOXL"]
    assert result.observation_only[0] == {"rank": 1, "symbol": "NVDL", "bias": "LONG_WATCH",
                                          "reason": "SYMBOL_NOT_IN_POLICY_UNIVERSE"}


def _extend_policy(*symbols):
    def mutate(artifact):
        policy = artifact["snapshot"]["payload"]["policy_config"]
        policy["allowed_symbols"] = sorted(set(policy["allowed_symbols"]) | set(symbols))
    return mutate


@pytest.mark.parametrize("symbol", ["NVDL", "SQQQ", "TSLL"])
def test_listed_leveraged_or_inverse_etf_is_still_not_a_long_leg(tmp_path, symbol):
    write_freeze(tmp_path, watchlist=[watch_item(symbol)], mutate=_extend_policy(symbol))
    assert _gate(tmp_path).observation_only[0]["reason"] == "LEVERAGED_ETF_NOT_ALLOWED"


def test_long_leg_requires_the_symbol_in_the_snapshot_policy_universe(tmp_path):
    write_freeze(tmp_path, watchlist=[watch_item("XYZ")])
    result = _gate(tmp_path)
    assert result.decision == NO_TRADE
    assert result.observation_only[0]["reason"] == "SYMBOL_NOT_IN_POLICY_UNIVERSE"
    write_freeze(tmp_path / "empty", mutate=lambda a: a["snapshot"]["payload"][
        "policy_config"].update(allowed_symbols=[]))
    assert evaluate_gates(BRIEF_DAY, tmp_path / "empty", BINDING).decision == NO_TRADE


def test_risk_off_maps_tqqq_and_soxl_short_watch_to_inverse_etfs(tmp_path):
    write_freeze(tmp_path, regime="RISK_OFF", watchlist=[
        watch_item("SOXL", "SHORT_WATCH"),
        watch_item("TSLA", "SHORT_WATCH"),
        watch_item("TQQQ", "SHORT_WATCH"),
    ])
    result = _gate(tmp_path)
    assert result.decision == TRADE and result.leg == LEG_INVERSE
    assert [(c.rank, c.signal_symbol, c.traded_symbol, c.leg) for c in result.candidates] == [
        (0, "SOXL", "SOXS", LEG_INVERSE), (2, "TQQQ", "SQQQ", LEG_INVERSE)]
    assert result.observation_only == ({"rank": 1, "symbol": "TSLA", "bias": "SHORT_WATCH",
                                        "reason": "SHORT_WATCH_OBSERVATION_ONLY"},)


def test_individual_stock_short_watch_alone_is_no_trade(tmp_path):
    write_freeze(tmp_path, regime="RISK_OFF", watchlist=[watch_item("TSLA", "SHORT_WATCH")])
    result = _gate(tmp_path)
    assert result.decision == NO_TRADE
    assert result.reason_codes == ("NO_ELIGIBLE_CANDIDATE",)


def test_inverse_mapping_requires_risk_off(tmp_path):
    write_freeze(tmp_path, regime="RISK_ON", watchlist=[watch_item("TQQQ", "SHORT_WATCH")])
    result = _gate(tmp_path)
    assert result.decision == NO_TRADE
    assert all(c.traded_symbol != "SQQQ" for c in result.candidates)


def test_inverse_leg_requires_approved_template(tmp_path):
    write_freeze(tmp_path, regime="RISK_OFF",
                 watchlist=[watch_item("TQQQ", "SHORT_WATCH", approved=False)])
    assert _gate(tmp_path).decision == NO_TRADE


def test_gate_result_is_json_serializable(tmp_path):
    write_freeze(tmp_path)
    json.dumps(_gate(tmp_path).to_dict())


@pytest.mark.parametrize("mutation", [
    lambda a: a["snapshot"]["payload"].update(policy_config=None),
    lambda a: a["snapshot"]["payload"]["policy_config"].update(allowed_symbols=None),
    lambda a: a["snapshot"]["payload"]["policy_config"].pop("allowed_symbols"),
])
def test_malformed_policy_config_is_structure_invalid(tmp_path, mutation):
    write_freeze(tmp_path, mutate=mutation)
    result = _gate(tmp_path)
    assert result.decision == NO_TRADE
    assert result.reason_codes == ("ARTIFACT_STRUCTURE_INVALID",)
