"""ORB 사전등록 계약 검증 (이슈 #89 — 안전 게이트 3/6).

사전등록은 **문서**지만, 문서만으로는 다음이 보장되지 않는다:
  · 문서의 숫자와 machine-readable contract 가 서로 다를 수 있다
  · 고정했다는 snapshot hash 가 실제 tracked receipt 와 다를 수 있다
  · fold 경계가 손으로 적혀 재현되지 않을 수 있다
  · 정본 계획의 최소 게이트가 **조용히 완화**될 수 있다
  · "결과를 안 봤다"는 주장이 검증되지 않는다

이 테스트가 그 다섯 가지를 기계적으로 강제한다. 성과·가격 데이터는 일절 읽지 않는다
(캘린더 계산과 tracked 문서 조회만).
"""
from __future__ import annotations

import json
import os
import re
from datetime import date

import pytest

from src.history.exchange_calendar import early_close_days, trading_days
from src.history.oos_guard import TRACK_INTRADAY, blind_oos_window

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESEARCH = os.path.join(REPO, "docs", "research")
PREREG_MD = os.path.join(RESEARCH, "PREREG_P0_US30_ORB_V1.md")
PREREG_JSON = os.path.join(RESEARCH, "PREREG_P0_US30_ORB_V1.json")
SNAPSHOT_RECEIPT = os.path.join(RESEARCH, "P0_US30_ORB_CLEAN_SNAPSHOT_RECEIPT.json")
MANIFEST = os.path.join(RESEARCH, "P0_US30_ORB_CLEAN_1M_MANIFEST.json")
PLAN = os.path.join(RESEARCH, "PLAN_ONE_MONTH_STRATEGY_SELECTION_AI_SHADOW.md")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def contract() -> dict:
    return _json(PREREG_JSON)


@pytest.fixture(scope="module")
def doc() -> str:
    return _read(PREREG_MD)


# --------------------------------------------------------------------------
# (a) 문서 ↔ machine-readable contract 일치
# --------------------------------------------------------------------------
def test_document_and_contract_exist_and_declare_no_results(contract, doc):
    assert contract["schema"] == "prereg-orb/contract/v1"
    assert contract["results_examined"] is False
    assert "결과 미열람" in doc


def test_hashes_appear_identically_in_document_and_contract(contract, doc):
    ident = contract["data_identity"]
    for key in ("snapshot_file_sha256", "snapshot_content_sha256",
                "manifest_data_sha256", "raw_content_sha256"):
        assert ident[key] in doc, f"{key} 가 문서에 없다"
    assert ident["snapshot_basename"] in doc
    assert ident["collection_run_id"] in doc


def test_thresholds_appear_in_document(contract, doc):
    """임계값이 문서와 contract 에서 갈리면 어느 쪽이 사전등록인지 모호해진다.

    **부분문자열 포함 검사는 쓰지 않는다** — `str(1.0) in doc` 은 "1.0%" 나 "2023" 같은 무관한
    텍스트로도 참이 되어 거짓 확신을 준다(독립 리뷰 지적). 정확한 문구로 대조한다.
    """
    assert "RVOL ≥ 1.5" in doc and contract["candidate"]["rvol"]["threshold"] == 1.5
    assert "|gap| ≥ 0.30%" in doc and contract["candidate"]["gap"]["threshold_abs"] == 0.0030
    assert "entry + 2.0 × 1R" in doc and contract["risk"]["target_r_multiple"] == 2.0
    assert "| 동시 보유 최대 종목 수 | **2** |" in doc
    assert contract["portfolio"]["max_positions"] == 2
    assert "- 90분" in doc and contract["signal"]["max_entry_minutes_before_close"] == 90


# --------------------------------------------------------------------------
# (a2) 사전등록 고유 값 전량 고정
#      독립 리뷰 mutation 스윕에서 19개 값이 **테스트 없이 통과**했다. 원인은 명확했다:
#      외부 tracked artifact 와 대조되는 값(계획·RiskConfig·receipt)만 보호되고, 이 문서에서만
#      정해지는 값은 하나도 보호되지 않았다. 아래가 그 구멍을 덮는다.
# --------------------------------------------------------------------------
def test_leakage_invariant_is_pinned(contract):
    """가장 중요한 불변식 — development 는 OOS 봉을 쓸 수 없다.

    이 값이 조용히 true 로 뒤집혀도 어떤 테스트도 실패하지 않았다(리뷰 지적 중 최악).
    """
    f = contract["folds"]
    assert f["development_may_use_oos_bars"] is False
    assert f["oos_warmup_may_use_pre_oos_bars"] is True
    b = contract["blind_enforcement"]
    assert b["loader"] == "src.history.oos_guard.research_load"
    assert b["direct_load_candles_forbidden"] is True
    assert b["bypass_is_invalidation_grade"] is True
    assert b["unlock_verifier_required_before_oos"] == "#78"


def test_development_gate_values_are_pinned(contract):
    g = contract["development_gate"]
    assert g["D1_min_trades"] == 536
    assert g["D2_min_trading_days_with_trade"] == 322
    assert g["D3_net_expectancy_gt"] == 0
    assert g["D4_profit_factor_gte"] == 1.20
    assert g["D5_abs_mdd_lte"] == 0.10
    assert g["D6_min_positive_folds"] == 7 and g["D6_total_folds"] == 10
    assert g["D7_stress_cumulative_pnl_gte"] == 0
    assert g["D8_parameter_cliff"] == "opposite_sign_variants_lte_1"


def test_development_sample_gates_are_rate_consistent_with_oos(contract):
    """dev 가 OOS 보다 느슨하면, dev 를 통과한 전략이 OOS 표본 수에서 자동 탈락한다."""
    g, o, w = contract["development_gate"], contract["oos_gate"], contract["windows"]
    scale = w["development_eval_trading_days"] / w["blind_oos_trading_days"]
    assert g["D1_min_trades"] >= o["O4_min_trades"] * scale
    assert g["D2_min_trading_days_with_trade"] >= o["O3_min_trading_days_with_trade"] * scale


def test_candidate_and_signal_values_are_pinned(contract):
    c, s, r = contract["candidate"], contract["signal"], contract["risk"]
    assert c["rvol"]["lookback_trading_days"] == 20
    assert c["rvol"]["min_valid_lookback_days"] == 15
    assert c["uses_premarket"] is False
    assert sorted(c["exclusion_reasons"]) == sorted([
        "incomplete_opening_range", "no_previous_regular_close",
        "insufficient_rvol_lookback", "opening_range_not_closed"])
    assert s["buffer"] == 0.0 and s["touch_fill"] is False
    assert s["extra_confirmation_bars"] == 0
    assert s["max_entry_minutes_before_close"] == 90
    assert r["stale_bars_to_force_exit"] == 3
    assert r["risk_budget_pct_of_equity"] == 0.01
    assert r["one_r"] == "entry - OR_low" and r["stop"] == "OR_low"
    assert contract["spec"]["decision_bar_minutes"] == 5
    assert contract["spec"]["primary"]["opening_range_minutes"] == 15
    assert contract["spec"]["sensitivity"]["opening_range_minutes"] == 5


def test_account_model_is_pinned(contract, doc):
    a = contract["account_model"]
    assert a["initial_capital"] == 100000.0
    assert a["currency"] == "USD"
    assert a["sizing_base"] == "fixed_initial_capital"
    assert a["sizing_base_uses_running_equity"] is False
    assert a["oos_curve_restarts_at_initial_capital"] is True
    assert "100,000 USD" in doc


def test_parameter_cliff_neighborhood_is_pinned(contract):
    n = contract["parameter_cliff_neighborhood"]
    assert n["cliff_if_opposite_sign_variants_gte"] == 2
    axes = {v["axis"]: v for v in n["variants"]}
    assert set(axes) == {"rvol_threshold", "gap_threshold_abs", "target_r_multiple"}
    assert axes["rvol_threshold"]["neighbors"] == [1.3, 1.7]
    assert axes["gap_threshold_abs"]["neighbors"] == [0.0020, 0.0040]
    assert axes["target_r_multiple"]["neighbors"] == [1.5, 2.5]
    assert sum(len(v["neighbors"]) for v in n["variants"]) == 6
    lock = n["non_primary_permanently_ineligible"]      # 후속 연구 cherry-pick 차단
    assert lock["scope_oos_window"] == "2026-01-21..2026-07-21"
    assert lock["successor_must_use_data_after"] == "2026-07-21"


def test_defect_escalation_ratio_is_pinned(contract):
    e = contract["defect_policy"]["escalation_ratio"]
    assert e["threshold"] == 0.05
    assert e["numerator_excludes_strategy_filters"] is True
    # 분자는 **데이터 사유 4종 + 운영 2종** 뿐이다. gap/RVOL 임계 미달(전략이 의도한 필터링)이
    # 들어가면 비율이 구조적으로 70~80% 가 되어 결함 게이트가 항상 발동한다.
    assert sorted(e["numerator"]) == sorted([
        "incomplete_opening_range", "no_previous_regular_close",
        "insufficient_rvol_lookback", "opening_range_not_closed",
        "stale_exit", "eod_fallback", "unclosable"])
    assert e["unit"] == "symbol_day_deduplicated"      # 중복 계상은 비율을 2배까지 흔든다
    assert not [x for x in e["numerator"] if "threshold" in x or "below_" in x]
    assert "trading_days" in e["denominator"]


def test_cost_official_level_and_rank_keys_are_pinned(contract):
    assert contract["costs"]["official_level"] == "official"
    keys = contract["portfolio"]["rank_keys"]
    assert [k["key"] for k in keys] == ["rvol", "abs_gap", "symbol"]
    assert [k["order"] for k in keys] == ["desc", "desc", "asc"]
    assert contract["portfolio"]["composite_score"] is None


def test_exposure_basis_and_metric_denominators_are_pinned(contract, doc):
    """경로 의존을 되살리는 값들 — 되돌리면 반드시 깨져야 한다(통계 리뷰 최종 지적)."""
    assert contract["risk"]["current_effective_exposure_basis"] == "entry_fill_cost"
    assert "진입 체결 원가" in doc
    mdd = contract["evaluation_definitions"]["mdd"]
    assert "peak" in mdd and "official cost" in mdd
    assert "(peak - trough) / peak" in doc
    assert "official cost" in contract["evaluation_definitions"]["concentration_share"]
    assert contract["portfolio"]["slot_allocation_is_arrival_time_priority"] is True
    assert "도착 시각 우선" in doc


def test_process_preconditions_are_separate_from_result_criteria(contract):
    p = contract["preconditions_not_result_criteria"]
    assert p["unmet_verdict"] == "NOT_RUN"
    assert "D10_independent_review" not in contract["development_gate"]


def test_cost_levels_match_contract_and_document(contract, doc):
    costs = contract["costs"]["round_trip_pct_points"]
    assert costs == {"low": 0.14, "official": 0.34, "stress": 0.68}
    for v in ("0.14", "0.34", "0.68"):
        assert v in doc
    assert contract["costs"]["allocation"] == {"entry": 0.5, "exit": 0.5}
    assert contract["costs"]["additional_fee_on_top"] is False


# --------------------------------------------------------------------------
# (b) 고정 hash ↔ tracked artifact 일치 (fail-closed 의 근거)
# --------------------------------------------------------------------------
def test_fixed_hashes_match_tracked_snapshot_receipt(contract):
    rec = _json(SNAPSHOT_RECEIPT)
    ident = contract["data_identity"]
    assert ident["snapshot_basename"] == rec["snapshot"]["basename"]
    assert ident["snapshot_file_sha256"] == rec["snapshot"]["file_sha256"]
    assert ident["snapshot_content_sha256"] == rec["snapshot"]["content_sha256"]
    assert ident["raw_content_sha256"] == rec["raw_content_sha256"]
    assert ident["as_of"] == rec["contract"]["as_of"]
    assert ident["collection_run_id"] == rec["contract"]["collection_run_id"]
    assert sorted(ident["allowed_sessions"]) == sorted(rec["contract"]["allowed_sessions"])


def test_fixed_manifest_hash_matches_tracked_manifest(contract):
    man = _json(MANIFEST)
    assert contract["data_identity"]["manifest_data_sha256"] == man["data_content_sha256"]
    assert contract["data_identity"]["source_interval"] == man["interval"]


def test_universe_matches_tracked_snapshot_receipt(contract):
    rec = _json(SNAPSHOT_RECEIPT)
    u = contract["universe"]
    listed = sorted(u["market_etf"] + u["sector_etf"] + u["large_cap"] + u["leveraged_inverse"])
    assert listed == sorted(rec["contract"]["symbols"])
    assert len(listed) == 30
    assert len(set(listed)) == 30
    # family 구성원은 유니버스 안에 있어야 한다
    for members in u["exposure_families"].values():
        assert set(members) <= set(listed)


def test_blind_oos_window_matches_code_gate(contract):
    lo, hi = blind_oos_window(TRACK_INTRADAY)
    w = contract["windows"]
    assert w["blind_oos_start"] == lo.isoformat()
    assert w["blind_oos_end"] == hi.isoformat()


# --------------------------------------------------------------------------
# (c) fold·구간이 캘린더에서 실제로 재현되는가 (손으로 적은 표가 아님을 증명)
# --------------------------------------------------------------------------
def _eval_days(contract) -> list[date]:
    w = contract["windows"]
    dev = trading_days(date.fromisoformat(w["development_data_start"]),
                       date.fromisoformat(w["development_data_end"]))
    return dev[w["warmup_trading_days"]:]


def test_window_trading_day_counts_are_reproducible(contract):
    w = contract["windows"]
    dev = trading_days(date.fromisoformat(w["development_data_start"]),
                       date.fromisoformat(w["development_data_end"]))
    assert len(dev) == w["development_data_trading_days"]
    ev = _eval_days(contract)
    assert len(ev) == w["development_eval_trading_days"]
    assert ev[0].isoformat() == w["development_eval_start"]
    assert ev[-1].isoformat() == w["development_eval_end"]
    oos = trading_days(date.fromisoformat(w["blind_oos_start"]),
                       date.fromisoformat(w["blind_oos_end"]))
    assert len(oos) == w["blind_oos_trading_days"]


def test_development_ends_strictly_before_blind_oos(contract):
    w = contract["windows"]
    assert date.fromisoformat(w["development_eval_end"]) < date.fromisoformat(w["blind_oos_start"])
    assert date.fromisoformat(w["development_data_end"]) < date.fromisoformat(w["blind_oos_start"])


def test_fold_boundaries_are_reproducible_from_the_calendar(contract, doc):
    """문서·contract 의 10개 fold 경계를 캘린더에서 다시 계산해 대조한다."""
    f = contract["folds"]
    ev = _eval_days(contract)
    blk = f["block_trading_days"]
    n = len(ev)
    k = n // blk
    head = n - k * blk
    rebuilt = [ev[0:head + blk]] + [ev[head + blk + i * blk: head + blk + (i + 1) * blk]
                                    for i in range(k - 1)]
    assert len(rebuilt) == len(f["boundaries"])
    for got, want in zip(rebuilt, f["boundaries"]):
        assert got[0].isoformat() == want["start"], want
        assert got[-1].isoformat() == want["end"], want
        assert len(got) == want["trading_days"], want
        assert want["start"] in doc and want["end"] in doc      # 문서 표와도 일치
    assert sum(len(x) for x in rebuilt) == n                    # 누락·중복 없음
    assert all(len(x) >= blk for x in rebuilt)


def test_folds_are_contiguous_and_non_overlapping(contract):
    b = contract["folds"]["boundaries"]
    for prev, nxt in zip(b, b[1:]):
        assert date.fromisoformat(prev["end"]) < date.fromisoformat(nxt["start"])
        gap = trading_days(date.fromisoformat(prev["end"]),
                           date.fromisoformat(nxt["start"]))
        assert len(gap) == 2, "fold 사이에 평가되지 않는 거래일이 있다"


def test_early_close_days_match_the_calendar(contract):
    w, f = contract["windows"], contract["folds"]
    dev = early_close_days(date.fromisoformat(w["development_eval_start"]),
                           date.fromisoformat(w["development_eval_end"]))
    assert [d.isoformat() for d in dev] == f["development_early_close_days"]
    oos = early_close_days(date.fromisoformat(w["blind_oos_start"]),
                           date.fromisoformat(w["blind_oos_end"]))
    assert [d.isoformat() for d in oos] == f["blind_oos_early_close_days"]


def test_windows_are_inside_calendar_validity(contract):
    w = contract["windows"]
    assert (date.fromisoformat(w["calendar_valid_start"])
            <= date.fromisoformat(w["development_data_start"]))
    assert date.fromisoformat(w["blind_oos_end"]) <= date.fromisoformat(w["calendar_valid_end"])


# --------------------------------------------------------------------------
# (d) 정본 계획의 최소 게이트가 완화되지 않았는가
# --------------------------------------------------------------------------
def test_oos_gate_is_not_weaker_than_the_frozen_plan_minimums(contract):
    """정본 계획의 최소 게이트를 **숫자로** 동결해 두고 O-게이트가 그보다 약하지 않은지 본다.

    이전 판은 계획 문서의 **한국어 산문을 부분문자열로** 대조했다. 계획은 개정 이력을 가진
    가변 문서라, 정당한 표현 수정만으로 동결된 사전등록 테스트가 깨진다(고칠 방법도 없다).
    실제로 원하는 성질은 "숫자가 약해지지 않았다" 이므로 숫자로 비교한다(독립 리뷰 지적).
    """
    m, g = contract["plan_minimum_gate"], contract["oos_gate"]
    assert m["source_commit"], "계획 최소 게이트의 출처 commit 이 없다"
    assert g["O1_net_expectancy_gt"] >= m["net_expectancy_gt"]
    assert g["O2_profit_factor_gte"] >= m["profit_factor_gte"]
    assert g["O3_min_trading_days_with_trade"] >= m["intraday_min_trading_days"]
    assert g["O4_min_trades"] >= m["intraday_min_trades"]
    assert g["O5_min_positive_months"] >= m["intraday_min_positive_months"]
    assert g["O5_total_months"] == m["intraday_total_months"]
    assert g["O6_abs_mdd_lte"] <= m["abs_mdd_lte"]                  # 작을수록 엄격
    assert g["O7_stress_cumulative_pnl_gte"] >= m["stress_cumulative_pnl_gte"]
    assert g["O8_max_single_symbol_profit_share"] <= m["max_single_symbol_profit_share"]
    assert g["O9_max_single_day_profit_share"] <= m["max_single_day_profit_share"]
    assert g["O11_major_data_defects"] <= m["major_data_defects"]


def test_frozen_plan_minimums_cannot_be_lowered(contract):
    """**동결한 하한 자체**를 낮추는 우회를 막는다.

    O-게이트를 계획 하한과 비교하는 것만으로는 부족하다 — 하한을 같이 낮추면 비교는 통과한다.
    그래서 하한 숫자를 정본 계획 원문과 직접 대조한다. (첫 판은
    `... or "1.20" in plan` 이라 어떤 값이든 통과하는 거짓 가드였다 — 내가 고치던 바로 그 유형.)
    """
    plan = _read(PLAN)
    m = contract["plan_minimum_gate"]
    assert m["profit_factor_gte"] == 1.20
    assert m["intraday_min_trading_days"] == 60
    assert m["intraday_min_trades"] == 100
    assert m["intraday_min_positive_months"] == 4
    assert m["intraday_total_months"] == 6
    assert m["abs_mdd_lte"] == 0.10
    assert m["max_single_symbol_profit_share"] == 0.25
    assert m["max_single_day_profit_share"] == 0.20
    assert m["net_expectancy_gt"] == 0 and m["major_data_defects"] == 0
    # 각 숫자가 정본 계획 원문에 실제로 존재하는지(하한을 지어내지 않았음)
    for token in ("1.20", "60", "100", "4개월", "10%", "25%", "20%"):
        assert token in plan, f"계획 원문에 {token} 이 없다 — 하한 출처 확인 필요"


def test_risk_limits_match_live_risk_config(contract):
    """포트폴리오 한도를 사전등록이 임의로 키우지 않았는지 — 라이브 RiskConfig 와 대조."""
    from src.risk.risk_manager import RiskConfig
    cfg, p = RiskConfig(), contract["portfolio"]
    assert p["max_positions"] == cfg.max_positions
    assert p["max_portfolio_effective_pct"] == cfg.max_portfolio_effective_pct
    assert p["max_symbol_effective_pct"] == cfg.max_symbol_effective_pct
    assert p["max_symbol_notional_pct"] == cfg.max_symbol_notional_pct
    assert contract["risk"]["eod_flatten_minutes_before_close"] == 10
    # 코드 기본값을 따라다니면 코드 변경만으로 연구 계약이 바뀐다(R18)
    assert "prereg-fixed" in contract["risk"]["eod_flatten_minutes_before_close_owner"]
    assert "ExitConfig" in contract["risk"]["eod_flatten_minutes_before_close_owner"]
    assert contract["risk"]["scale_in_max"] == 0


def test_single_pass_and_authority_rules(contract):
    s, o = contract["spec"], contract["oos_gate"]["single_pass"]
    assert s["primary"]["authority"] == "decides_go_nogo"
    assert s["sensitivity"]["authority"] == "diagnostic_only"
    assert s["sensitivity_may_rescue_primary"] is False
    assert o["load_once"] and o["compute_once"] and o["rerun_on_bad_result"] is False
    assert set(o["same_pass_includes"]) == {"primary", "parameter_cliff_neighborhood", "sensitivity"}
    n = contract["parameter_cliff_neighborhood"]
    assert n["is_search"] is False and n["can_change_primary"] is False and n["one_axis_only"]


def test_safety_invariants_are_fixed(contract):
    sig, r = contract["signal"], contract["risk"]
    assert sig["signal_bar_fill"] is False
    assert sig["fill"] == "open_of_next_completed_decision_bar"
    assert sig["direction"] == "long_only"
    assert sig["naked_short_individual_stocks"] is False
    assert sig["max_trades_per_symbol_per_day"] == 1
    assert sig["reentry_same_day"] is False
    assert r["averaging_down"] is False and r["overnight"] is False
    assert r["same_bar_stop_and_target"] == "stop_first_conservative"
    assert r["tradable_sessions"] == ["REGULAR"]
    assert r["session_end_bucket_policy"] == "post_inclusive_load"
    assert contract["development_gate"]["on_failure"] == {
        "open_blind_oos": False, "modify_rules": False, "verdict": "NO_SELECTION"}
    assert contract["verdict_definitions"]["pick_least_bad"] is False


def test_sizing_caps_instead_of_blocking(contract, doc):
    """v1.1 핵심 교정 — 한도를 '차단'으로 쓰면 명목 한도가 거의 모든 거래를 없앤다.

    1R/entry >= risk_pct/notional_cap = 5% 여야 한도가 binding 하지 않는데, 15분 opening range
    폭은 통상 0.5~2% 다. 차단 규칙이면 거래가 사실상 발생하지 않아 표본 게이트가 엣지와 무관하게
    미달한다. 이 테스트가 그 회귀를 막는다.
    """
    r = contract["risk"]
    assert r["limits_applied_as"] == "cap_not_block"
    assert r["skip_if_qty_lt"] == 1
    assert r["equity_base"] == "fixed_initial_capital"
    assert "min(" in r["sizing"]
    assert contract["portfolio"]["insufficient_capacity"].startswith("cap_quantity")
    assert "축소" in doc and "차단이 아니라" in doc
    # 산술 근거가 문서에 남아 있어야 한다(왜 차단이 불가능한지).
    # `"5%" in doc` 은 §11.2 의 무관한 "5%" 로도 참이 되는 **거짓 가드**였다(리뷰 지적) —
    # 이 근거 문단 고유의 문자열로 앵커한다.
    assert "1% / 20% = 5%" in doc
    assert contract["portfolio"]["insufficient_capacity"] == "cap_quantity_then_skip_only_if_qty_lt_1"


def test_exit_priority_is_conservative_and_total(contract, doc):
    assert contract["risk"]["exit_priority"] == ["stop", "eod", "target"]
    assert "stop > EOD > target" in doc


def test_oos_month_buckets_tile_the_window_exactly(contract):
    """'6개월 중 4개월 양수' 의 월 경계가 OOS 창을 빈틈·중복 없이 덮는지."""
    w = contract["windows"]
    months = contract["oos_gate"]["O5_month_boundaries"]
    assert len(months) == contract["oos_gate"]["O5_total_months"] == 6
    assert months[0]["start"] == w["blind_oos_start"]
    assert months[-1]["end"] == w["blind_oos_end"]
    for prev, nxt in zip(months, months[1:]):
        assert (date.fromisoformat(nxt["start"]) - date.fromisoformat(prev["end"])).days == 1
    covered = sum(len(trading_days(date.fromisoformat(m["start"]),
                                   date.fromisoformat(m["end"]))) for m in months)
    assert covered == w["blind_oos_trading_days"], "월 구간 합이 OOS 거래일 수와 다르다"


def test_evaluation_statistics_are_defined(contract, doc):
    ev = contract["evaluation_definitions"]
    for key in ("net_pnl", "net_expectancy", "profit_factor", "daily_equity_curve",
                "mdd", "trading_days_counted", "concentration_share"):
        assert ev.get(key), key
    assert "auto-fail" in ev["concentration_share"]      # 분모 <= 0 이면 자동 미달
    assert "profit factor" in doc.lower() or "profit_factor" in doc


def test_discretion_removing_clauses_are_present(contract):
    s, p = contract["signal"], contract["portfolio"]
    assert s["max_entry_cutoff_applies_to"] == "fill_bar_start"
    assert s["capacity_skipped_signal_requeued"] is False
    assert s["trading_day_basis"] == "ET"
    assert p["family_cap_includes_pending_same_bar_picks"] is True


def test_revision_history_records_the_go_easier_audit(contract, doc):
    """개정이 있었다면 '이 변경이 GO 를 쉽게 만드는가' 감사가 문서에 있어야 한다."""
    rev = contract["revision"]
    assert rev["results_examined_at_revision"] is False
    assert contract["version"] == rev["version"]
    assert f"| v{contract['version']} |" in doc, "개정 이력 표에 현재 버전이 없다"
    assert "GO 를 쉽게 만드는가" in doc
    assert "O6" in doc, "게이트 난이도에 영향을 준 항목의 방향이 명시돼야 한다"


def test_opening_range_completeness_evidence_is_recorded(contract, doc):
    c = contract["candidate"]["opening_range_completeness_coverage_check"]
    assert c["complete_pct"] >= 95.0
    assert "coverage" in c["scope"] and "development" in c["scope"]
    assert str(c["complete_pct"]) in doc


def test_document_avoids_rot_prone_references(doc):
    """동결 문서에 **줄번호 참조·테스트 개수**를 넣지 않는다(A2 에서 세 번 어긋난 실패 유형)."""
    import re as _re
    assert not _re.findall(r"\.py[`:]*:\d+", doc), "소스 줄번호 참조는 코드가 자라면 썩는다"
    assert not _re.findall(r"\d+\s*(?:개\s*)?(?:tests?|테스트)\b", doc), "테스트 개수는 썩는다"
    assert "freeze 위험 분류" in doc


def test_referenced_paths_exist(contract, doc):
    for rel in ("docs/research/DATA_CONTRACT_4STRATEGY_V1.md",
                "docs/research/RESAMPLER_PARITY_CONTRACT.md",
                "docs/research/PLAN_ONE_MONTH_STRATEGY_SELECTION_AI_SHADOW.md",
                "docs/research/DECISION_DAYTRADING_ORB_PRIORITY.md",
                "src/history/exchange_calendar.py",
                "src/bars/resample.py"):
        assert os.path.exists(os.path.join(REPO, rel)), rel
        assert os.path.basename(rel) in doc or rel in doc


# --------------------------------------------------------------------------
# (e) 결과 부재 감사 — "결과를 보기 전에 썼다"를 검증 가능하게
# --------------------------------------------------------------------------
_RESULT_PATTERNS = re.compile(
    r"(RESULT|OOS_RESULT|WALKFORWARD|WALK_FORWARD|LEADERBOARD|UNLOCK|EXPECTANCY)", re.I)


_ORB_NAME = re.compile(r"(orb|opening[_-]?range)", re.I)


def test_no_orb_result_artifacts_exist_yet():
    """ORB 성과·OOS·unlock 산출물이 하나라도 있으면 사전등록의 전제가 깨진다.

    최상위 `docs/research/` 만 보던 것을 **재귀 + 다중 디렉터리 + `opening_range` 별칭**까지
    넓혔다(독립 리뷰 지적: `reports/` 나 하위 폴더의 결과가 보이지 않았다).
    """
    hits = []
    for base in ("docs", "reports", "analysis", "data"):
        root = os.path.join(REPO, base)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if _RESULT_PATTERNS.search(name) and _ORB_NAME.search(name):
                    # 1/6 스냅샷·복제 receipt 는 데이터 provenance 이지 성과가 아니다
                    if "CLEAN" in name.upper() and "RECEIPT" in name.upper():
                        continue
                    hits.append(os.path.relpath(os.path.join(dirpath, name), REPO))
    assert hits == [], f"ORB 결과 산출물이 이미 존재한다: {hits}"


def test_no_orb_strategy_implementation_exists_yet():
    """3/6 은 문서만 만든다 — 구현이 있으면 단계 경계가 깨진 것이다.

    파일명이 'orb' 를 포함하는 경우만 보던 것을 `opening_range` 별칭과 `scripts/` 까지 넓혔다.
    이 테스트는 **트립와이어**이지 증명이 아니다(§13 에 그 한계를 명시).
    """
    found = []
    for base in ("src", "analysis", "scripts"):
        root = os.path.join(REPO, base)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".py") or not _ORB_NAME.search(fn):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), REPO)
                if rel == os.path.join("src", "bars", "resample.py"):
                    continue        # #76 의 opening_range 파생기(전략 구현 아님)
                found.append(rel)
    assert found == [], f"ORB 구현 파일이 이미 존재한다: {found}"


def test_prereg_declares_what_it_did_not_do(doc):
    for phrase in ("성과 계산", "forward return", "blind OOS"):
        assert phrase in doc
    assert "§16" in doc or "이 문서가 하지 않은 것" in doc

def test_v14_discretion_closures_are_pinned(contract, doc):
    """v1.4 에서 없앤 재량들 — 되돌리면 깨져야 한다(독립 아키텍처 리뷰)."""
    r, p, sg = contract["risk"], contract["portfolio"], contract["signal"]
    assert r["exit_evaluation_timeframe_minutes"] == 5           # B1: +2R/-1R 을 가르는 값
    assert r["eod_must_use_exchange_calendar"] is True
    assert r["do_not_reuse_in_eod_flatten_window"] is True       # B8: 19:50 2차 EOD 방지
    assert set(r["risk_config_items_disabled"]) == {             # B5: 경로 의존 필터 비활성
        "daily_loss_limit_pct", "max_drawdown_pct_entry_block",
        "max_consecutive_losses", "reentry_cooldown", "block_all_entries_in_eod_window"}
    assert p["same_bar_fill_order"] == ["exits", "entries"]      # B6
    assert p["gate_population"] == "full_portfolio_including_leveraged"   # B3
    assert contract["candidate"]["rvol"]["prior_days_require_full_window"] is True  # B12a
    assert sg["max_entry_boundary_inclusive"] is True            # B12b
    assert sg["missing_next_scheduled_bar"] == "discard_signal"  # B12c
    assert contract["development_gate"]["D6_zero_trade_fold_counts_as_not_positive"] is True  # B11
    assert contract["md_is_authoritative_on_conflict"] is True   # B14
    assert "5분 결정봉" in doc


def test_cliff_criterion_is_internally_consistent(contract):
    """D8/O10 문안과 §10.1 판정 기준이 모순되면 어느 쪽이 게이트인지 알 수 없다(B4)."""
    n = contract["parameter_cliff_neighborhood"]
    assert n["cliff_if_opposite_sign_variants_gte"] == 2
    for key in ("D8_parameter_cliff",):
        assert contract["development_gate"][key] == "opposite_sign_variants_lte_1"
    assert contract["oos_gate"]["O10_parameter_cliff"] == "opposite_sign_variants_lte_1"


def test_verdict_set_is_complete(contract, doc):
    """NOT_RUN·INCONCLUSIVE_DATA 가 최종 상태표에 없으면 결과 작성자가 즉석에서 정한다(A2/B7)."""
    v = contract["verdict_definitions"]
    for state in ("SELECT_ONE", "EXTEND_PAPER", "NO_SELECTION", "NOT_RUN", "INCONCLUSIVE_DATA"):
        assert state in v, state
        assert state in doc, state
    assert v["pick_least_bad"] is False


def test_stage5_unlock_fields_are_designated(contract, doc):
    """5/6 unlock 은 hash 하나를 지정받아야 한다 — 4개 중 무엇인지 문서가 정해야 한다."""
    u = contract["unlock_artifact_reference"]
    assert u["track"] == "intraday"
    assert u["snapshot_hash_field_uses"] == "snapshot_content_sha256"
    assert u["snapshot_hash_value"] == contract["data_identity"]["snapshot_content_sha256"]
    assert "review_status" in doc and "implementation_commit" in doc


def test_audit_statement_matches_what_was_actually_run(doc):
    """§13 은 §5.5 의 coverage 측정을 포함해야 한다 — 아니면 문서가 스스로를 반증한다(A3)."""
    assert "research_load" in doc and "coverage" in doc
    assert "봉 개수만" in doc or "개수만 센" in doc
    assert "캘린더 계산과 tracked manifest 조회**뿐이며" not in doc


def test_mapping_checklist_covers_new_normative_sections(doc):
    """§15 체크리스트가 v1.1~v1.4 신설 절을 빠뜨리면 구현 강제 장치가 새 조항을 놓친다(B13)."""
    block = doc.split("## 15.")[1] if "## 15." in doc else doc
    for sec in ("§3.5", "§8.0", "§10.3", "§10.4", "§11.0", "§11.1"):
        assert sec in block, f"{sec} 매핑 행 누락"

# --------------------------------------------------------------------------
# (f) 동결 후 썩을 수 있는 서술을 **기계 사실**로 바꾼다 (freeze 위험 R-시리즈)
#     "NFLX 가 마지막 상장" 같은 문장은 사람이 확인한 주장일 뿐이다. 매니페스트에서
#     직접 계산해 대조하면 그 문장이 참인 동안에만 테스트가 통과한다.
# --------------------------------------------------------------------------
def test_development_start_is_the_last_symbol_to_become_available(contract):
    """§3.1 의 경계 근거(30종목 공통 가용 시작)를 manifest 에서 직접 재계산한다 (R6)."""
    man = _json(MANIFEST)
    firsts = {s["symbol"]: s["allowed_earliest_trading_day"] for s in man["symbols"]}
    assert len(firsts) == 30
    latest_symbol = max(firsts, key=lambda k: firsts[k])
    assert firsts[latest_symbol] == contract["windows"]["development_data_start"], (
        f"development 시작이 최후 가용 종목({latest_symbol})의 첫 거래일과 다르다")
    # 그 날짜 이전에는 30종목이 동시에 존재하지 않는다
    assert sum(1 for v in firsts.values()
               if v > contract["windows"]["development_data_start"]) == 0


def test_row_count_and_sessions_match_tracked_receipt(contract, doc):
    """§2 의 행수·세션 서술이 receipt 와 어긋나면 실패한다 (R1/R4)."""
    rec = _json(SNAPSHOT_RECEIPT)
    assert f"{rec['snapshot']['row_count']:,}" in doc
    assert rec["snapshot"]["disallowed_sessions"] == []
    assert rec["snapshot"]["bars_beyond_as_of"] == 0
    assert "OVERNIGHT` 0행" in doc or "OVERNIGHT 0행" in doc


def test_leverage_factors_match_the_code(contract):
    """§7 의 레버리지 3배 서술을 코드에서 확인한다 (R11)."""
    from src.universe.screener import leverage_for
    for sym in contract["universe"]["leveraged_inverse"]:
        assert leverage_for(sym) == 3, sym
    for sym in contract["universe"]["market_etf"] + contract["universe"]["sector_etf"]:
        assert leverage_for(sym) == 1, sym


def test_session_end_bar_count_is_derived_not_copied(doc):
    """§8.7 의 '닫힌 5m 77개' 는 다른 문서에서 베낀 수치였다 (R2).

    390분 정규장에서 마지막 버킷이 닫히지 않으므로 390//5 - 1 = 77 — 유도 근거를 문서에
    남겨 두면, 그 수치가 바뀌어도 어디서 왔는지 추적할 수 있다.
    """
    assert (390 // 5) - 1 == 77
    assert "390 ÷ 5 − 1" in doc or "390//5" in doc or "390 분" in doc


def test_v15_discretion_closures_are_pinned(contract, doc):
    """v1.5 에서 없앤 재량 — 되돌리면 깨져야 한다(독립 구현가능성 리뷰)."""
    r, sp = contract["risk"], contract["spec"]
    # #1 같은 봉 2종목이 각각 30% 를 잡아 합 60% 로 50% 한도를 넘던 구멍
    assert r["current_effective_exposure_includes_same_bar_accepted"] is True
    assert "이번 결정봉에서 이미 채택된 후보" in doc
    assert sp["decision_bar_min_bars"] == 1                       # #2
    f = r["eod_fallback"]                                          # #3
    assert "last existing REGULAR decision bar" in f["price"]
    assert "unclosable" in f["if_none"]
    dl = contract["data_load"]                                     # #4
    assert "per ET trading day" in dl["extent"]
    assert "regular_close_minute" in dl["decision_bars_bounded_by"]
    assert contract["account_model"]["curve_includes_initial_capital_at_t0"] is True   # #10
    assert contract["defect_policy"]["computed_on"] == "primary"   # #9
    assert contract["parameter_cliff_neighborhood"][
        "zero_trade_variant_counts_as_opposite_sign"] is False     # #8
    assert contract["candidate"]["rvol"]["median_even_sample"] == "midpoint average"   # #16
    assert set(contract["portfolio"]["skipped_reason_vocabulary"]) == {   # #15
        "one_r_not_positive", "qty_lt_1", "family_cap", "slot_cap",
        "portfolio_exposure_cap", "signal_expired"}
    assert contract["portfolio"]["skipped_reasons_are_not_defects"] is True


def test_coverage_measurement_scope_is_honest(contract):
    """§5.5 실측치가 무엇을 재지 않았는지 문서가 밝히는지 (#5)."""
    c = contract["candidate"]["opening_range_completeness_coverage_check"]
    assert c["measures_only"] == "incomplete_opening_range"
    assert c["lookback_validity_unmeasured"] is True
