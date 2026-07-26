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
    # v1.7: 모집단이 다른 운영 실패는 [B] 로 분리했다(같은 분모에 섞으면 활동량이 데이터 게이트를 움직인다)
    assert sorted(e["numerator"]) == sorted([
        "incomplete_opening_range", "no_previous_regular_close",
        "insufficient_rvol_lookback", "opening_range_not_closed"])
    assert e["excludes_operational_reasons"] is True
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
    # 게이트 표의 D4/O2 행으로도 참이 되므로 §10.3 의 **정의 행** 고유 문구로 앵커한다.
    assert "gross profit / gross loss" in doc or "Σ(net PnL > 0)" in doc


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
    # `"O6" in doc` 은 §11 게이트 표 행으로도 참이 되어, **게이트가 쉬워졌다는 자기 공시 3곳을
    # 전부 지워도 통과**했다(적대적 리뷰 실증). 공시 문장 고유 문구로 앵커한다.
    assert "부분적으로 그렇다" in doc, "GO 를 쉽게 만든 항목의 공시가 지워졌다"
    assert "D5/O6(MDD ≤ 10%)은 통과하기 쉬워진다" in doc


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
                # 허용 목록은 **명시적**으로만 늘린다. 패턴을 느슨하게 하면 트립와이어가 죽는다.
                if rel == os.path.join("src", "bars", "resample.py"):
                    continue        # #76 의 opening_range 파생기(전략 구현 아님)
                if rel == os.path.join("scripts", "prereg_orb_coverage_probe.py"):
                    continue        # §5.5 커버리지 측정(봉 개수만 셈, 성과 계산 없음)
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
    assert set(contract["portfolio"]["skipped_reason_vocabulary"]) == {   # #15 / v1.7 N3
        "one_r_not_positive", "qty_lt_1", "family_cap", "slot_cap", "signal_expired"}
    assert contract["portfolio"]["skipped_reasons_are_not_defects"] is True


def test_coverage_measurement_scope_is_honest(contract):
    """§5.5 실측치가 무엇을 재지 않았는지 문서가 밝히는지 (#5)."""
    c = contract["candidate"]["opening_range_completeness_coverage_check"]
    assert c["measures_only"] == "incomplete_opening_range"
    assert c["lookback_validity_unmeasured"] is True
    # POST 측정은 표본 추정치다 — 모집단 값처럼 읽히지 않게 상한을 함께 고정
    assert c["eod_fallback_exposure_is_sample_estimate"] is True
    assert c["eod_fallback_rule_of_three_upper_bound_pct"] == 2.5


# --------------------------------------------------------------------------
# (g) **정본은 마크다운**인데 테스트는 JSON(사본)만 고정하고 있었다.
#     적대적 리뷰가 JSON 13곳 + 마크다운 11곳을 동시에 약화시켜도 전 스위트가 초록임을
#     실증했다 — 동결된 정본을 읽는 사람은 훨씬 쉬운 연구를 보게 된다.
#     아래가 정본 자체의 규범 문자열과 JSON 키를 전수 고정한다.
# --------------------------------------------------------------------------
_MD_GATE_ROWS = [
    # development D1~D9 (게이트 표의 기준 칸 고유 문구)
    "| D1 | 표본 — 거래 수 | **≥ 536**",
    "| D2 | 표본 — 거래 발생 거래일 수 | **≥ 322**",
    "| D3 | 순 기대값 | **> 0**",
    "| D4 | profit factor | **≥ 1.20** |",
    "| D5 | 최대낙폭 |",
    "| D6 | fold 안정성 | 10 folds 중 **순손익 양수 fold ≥ 7**",
    "| D7 | 비용 stress | **0.68%p 에서 누적 순손익 ≥ 0** |",
    # blind OOS O1~O11
    "| O1 | 공식 비용 후 net expectancy | **> 0** |",
    "| O2 | profit factor | **≥ 1.20** |",
    "| O4 | OOS 거래 수 | **≥ 100** |",
    "| O5 | 월별 일관성 | 6개월 중 **≥ 4개월 순손익 양수** |",
    "| O6 | 최대낙폭 | **|MDD| ≤ 10%** |",
    "| O7 | 비용 stress | **0.68%p 에서 누적 순손익 ≥ 0** |",
    "| O8 | 종목 집중도 |",
    "| O9 | 일자 집중도 |",
    "| O11 | 데이터 결함 | 누수·survivorship·corporate action **중대 결함 0** |",
]

_MD_NORMATIVE = [
    "**≥ 60** (창 길이 125일 자체가 아니라 **거래 발생일** 수)",      # O3
    "25% 초과 기여 금지",                                          # O8 수치
    "20% 초과 기여 금지",                                          # O9 수치
    "RVOL 임계 | 1.5 | 1.3, 1.7",                                # §10.1 이웃
    "gap 임계 | 0.30% | 0.20%, 0.40%",
    "target 배수 | 2.0R | 1.5R, 2.5R",
    "**5% 를 초과하면** 결함으로 승격",                              # §11.2 임계
    "분모 = 평가 구간의 (유니버스 30종목 × 거래일) 전체 종목-일",       # §11.2 분모
    "상대적으로 덜 나쁜 전략을 선택하지 않는다",                       # §12 결론 규칙
    "결과를 확인한 뒤에는 기준을 바꾸지 않는다",                       # §14.1
    "**\"성과가 나빠서\"는 무효화 사유가 아니다.**",                   # §14.2
    "규칙을 수정하지 않는다",                                       # §10.2
    "8개 파라미터화(primary + 6 이웃 + sensitivity) 전부를 보고서에 게재한다",  # §11.1
]


def test_authoritative_markdown_gate_tables_are_pinned(doc):
    """**정본 문서**의 게이트 표를 직접 고정한다 (적대적 리뷰 blocking).

    JSON 만 고정하면, 정본인 마크다운을 약화시켜도 스위트가 초록이다. 동결되는 것은 문서다.
    """
    missing = [row for row in _MD_GATE_ROWS if row not in doc]
    assert missing == [], f"정본 게이트 표가 변경/삭제됐다: {missing}"


def test_authoritative_markdown_normative_clauses_are_pinned(doc):
    """숫자 외에 **결론 규칙·개정 금지·무효화 사유**도 정본에서 지워질 수 있었다."""
    missing = [c for c in _MD_NORMATIVE if c not in doc]
    assert missing == [], f"정본 규범 문구가 사라졌다: {missing}"


_JSON_LITERALS = {
    ("development_gate", "D1_min_trades"): 536,
    ("development_gate", "D2_min_trading_days_with_trade"): 322,
    ("development_gate", "D4_profit_factor_gte"): 1.20,
    ("development_gate", "D5_abs_mdd_lte"): 0.10,
    ("development_gate", "D6_min_positive_folds"): 7,
    ("oos_gate", "O2_profit_factor_gte"): 1.20,
    ("oos_gate", "O3_min_trading_days_with_trade"): 60,
    ("oos_gate", "O4_min_trades"): 100,
    ("oos_gate", "O5_min_positive_months"): 4,
    ("oos_gate", "O6_abs_mdd_lte"): 0.10,
    ("oos_gate", "O8_max_single_symbol_profit_share"): 0.25,
    ("oos_gate", "O8_symbol_means_family_aggregated"): True,
    ("oos_gate", "O9_max_single_day_profit_share"): 0.20,
    ("oos_gate", "O11_major_data_defects"): 0,
    ("costs", "official_level"): "official",
    ("costs", "additional_fee_on_top"): False,
    ("signal", "signal_bar_fill"): False,
    ("signal", "touch_fill"): False,
    ("signal", "buffer"): 0.0,
    ("signal", "gap_through_entry"): "fill_at_next_bar_open_no_price_improvement",
    ("signal", "chase_cap"): None,
    ("risk", "gap_through_stop"): "fill_at_bar_open",
    ("risk", "gap_through_target"): "fill_at_bar_open",
    ("risk", "intrabar_touch_fill"): "at_level",
    ("risk", "same_bar_stop_and_target"): "stop_first_conservative",
    ("risk", "reject_if_one_r_not_positive"): True,
    ("risk", "trailing_stop"): False,
    ("risk", "partial_take_profit"): False,
    ("risk", "time_stop"): None,
    ("risk", "averaging_down"): False,
    ("risk", "scale_in_max"): 0,
    ("risk", "overnight"): False,
    ("risk", "target_r_multiple"): 2.0,
    ("portfolio", "max_positions_per_exposure_family"): 1,
    ("portfolio", "composite_score"): None,
    ("data_identity", "hash_mismatch_policy"): "fail_closed",
    ("verdict_definitions", "pick_least_bad"): False,
    ("blind_enforcement", "development_purpose"): "performance",
    ("blind_enforcement", "coverage_purpose_forbidden_in_performance_code"): True,
    ("blind_enforcement", "direct_load_candles_forbidden"): True,
    ("revision_policy", "invalidation_not_allowed_for"): "poor_performance",
    ("revision_policy", "post_merge_corrections"): "append_only_erratum",
    ("revision_policy", "invalidation_preserves_existing_results"): True,
}


def test_every_normative_json_value_is_pinned(contract):
    """게이트를 쉽게 만들 수 있는 JSON 키를 **전수** 리터럴 비교한다 (H2~H6)."""
    wrong = []
    for (section, key), want in _JSON_LITERALS.items():
        got = contract.get(section, {}).get(key, "<missing>")
        if got != want:
            wrong.append(f"{section}.{key}: {got!r} != {want!r}")
    assert wrong == [], "규범 값이 변경됐다:\n  " + "\n  ".join(wrong)


def test_defect_and_verdict_definitions_cannot_be_emptied(contract):
    """H3/H2 — 결함 목록·결론 규칙을 비우면 게이트가 공허해진다."""
    dp = contract["defect_policy"]
    assert set(dp["counted_as_defect"]) == {
        "ohlc_invariant_violation", "duplicate_ts_value_conflict",
        "unexplained_1m_price_jump_gt_50pct", "session_label_violation",
        "snapshot_hash_mismatch"}
    assert dp["escalate_to_defect_if_share_gt"] == dp["escalation_ratio"]["threshold"] == 0.05
    op = dp["operational_failure_ratio"]                 # 운영 실패 희석 방지
    assert op["denominator"] == "all_entered_trades_including_unclosable"
    assert op["threshold"] == 0.20
    assert "any development_gate or oos_gate item unmet" in contract[
        "verdict_definitions"]["NO_SELECTION"]
    fam = contract["universe"]["exposure_families"]
    assert set(fam) == {"QQQ_FAMILY", "SOX_FAMILY"}
    assert all(len(v) == 3 for v in fam.values())        # 단일 종목으로 축소 불가
    assert contract["revision_policy"]["forbidden_after_results"]


def test_publish_all_and_successor_lock_cannot_be_disabled(contract, doc):
    """H6 / §10.4 — 선택적 보고와 후속 우회를 한 불리언으로 열 수 없게."""
    sp = contract["oos_gate"]["single_pass"]
    assert sp["publish_all_parameterizations"] is True
    lock = contract["parameter_cliff_neighborhood"]["non_primary_permanently_ineligible"]
    assert lock["scope"] == "any ORB parameterization on this snapshot"
    assert set(lock["successor_must_use_post_as_of_for"]) == {"development", "oos"}
    assert "인접한 미평가 값" in doc


def test_coverage_probe_receipt_is_reproducible_evidence(contract):
    """§5.5 수치에 재현 경로가 있는지 — 없으면 근거가 아니라 주장이다(적대적 리뷰 지적)."""
    c = contract["candidate"]["opening_range_completeness_coverage_check"]
    assert c["reproduction_script"] == "scripts/prereg_orb_coverage_probe.py"
    assert os.path.exists(os.path.join(REPO, c["reproduction_script"]))
    receipt_path = os.path.join(REPO, c["receipt"])
    assert os.path.exists(receipt_path)
    r = _json(receipt_path)
    assert r["performance_computed"] is False
    assert r["window"]["blind_oos_days_included"] == 0
    assert r["result"]["complete_pct"] == c["complete_pct"]
    assert r["measures_only"] == c["measures_only"]


def test_conflict_baseline_is_adjudicated_before_results(contract):
    """N2 — 이미 0 이 아닌 conflict 기준선을 **결과 전에** 판정해 두었는가.

    기준선이 없으면 "1건 이상 = 중대 결함" 이 시작과 동시에 발동하고, 그 해석이 결과를 본 뒤에
    이뤄진다. tracked artifact 의 실제 값과 대조해 기준선이 지어낸 값이 아님을 확인한다.
    """
    b = contract["defect_policy"]["duplicate_ts_conflict_baseline"]
    man = _json(MANIFEST)
    actual = {x["symbol"]: x["conflict"] for x in man["symbols"] if x.get("conflict")}
    assert b["manifest_by_symbol"] == actual
    assert b["manifest_conflict_total"] == sum(actual.values()) == 28
    assert "not a contract violation" in b["adjudication"]
    assert b["operative_baseline"] == "manifest_conflict_total"   # 어느 행이 기준인가
    assert b["defect_is"] == "new conflicts beyond this baseline"


def test_operational_failures_use_a_trade_denominator(contract, doc):
    """N1 — 운영 실패가 종목-일 분모에 희석되지 않는가."""
    op = contract["defect_policy"]["operational_failure_ratio"]
    assert op["denominator"] == "all_entered_trades_including_unclosable"
    assert op["threshold"] == 0.20
    assert set(op["numerator"]) == {"stale_exit", "eod_fallback", "unclosable"}
    assert "[A] 데이터 품질 비율" in doc and "[B] 운영 실패 비율" in doc


def test_evidence_chain_obligation_is_pinned(contract, doc):
    """Q_A — 결과 보고가 unlock artifact 를 인용하지 않으면 판정으로 인정하지 않는다."""
    sp = contract["oos_gate"]["single_pass"]
    assert sp["report_must_cite_unlock_artifact"] is True
    assert sp["unlock_artifact_must_be_git_tracked"] is True
    assert "판정으로 인정하지 않는다" in doc
