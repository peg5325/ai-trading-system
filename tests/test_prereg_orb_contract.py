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
    assert g["D8_parameter_cliff"] == "no_sign_flip"


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
        "stale_exit", "eod_fallback"])
    assert not [x for x in e["numerator"] if "threshold" in x or "below_" in x]
    assert "trading_days" in e["denominator"]


def test_cost_official_level_and_rank_keys_are_pinned(contract):
    assert contract["costs"]["official_level"] == "official"
    keys = contract["portfolio"]["rank_keys"]
    assert [k["key"] for k in keys] == ["rvol", "abs_gap", "symbol"]
    assert [k["order"] for k in keys] == ["desc", "desc", "asc"]
    assert contract["portfolio"]["composite_score"] is None


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
def test_oos_gate_is_not_weaker_than_the_tracked_plan(contract):
    """정본 계획 §Phase4 최소 선택 게이트를 그대로 고정했는지 확인한다."""
    plan = _read(PLAN)
    g = contract["oos_gate"]
    assert "OOS profit factor >= 1.20" in plan and g["O2_profit_factor_gte"] == 1.20
    assert "OOS 거래일 >= 60, 거래 >= 100" in plan
    assert g["O3_min_trading_days_with_trade"] == 60 and g["O4_min_trades"] == 100
    assert "6개월 중 >= 4개월 양수" in plan and g["O5_min_positive_months"] == 4
    assert "OOS MDD 절대값 <= 10%" in plan and g["O6_abs_mdd_lte"] == 0.10
    assert "stress 비용 0.68%p에서 누적 손익 >= 0" in plan and g["O7_stress_cumulative_pnl_gte"] == 0
    assert "단일 종목이 총 net profit의 25% 초과 기여 금지" in plan
    assert g["O8_max_single_symbol_profit_share"] == 0.25
    assert "단일 거래일이 총 net profit의 20% 초과 기여 금지" in plan
    assert g["O9_max_single_day_profit_share"] == 0.20
    assert g["O1_net_expectancy_gt"] == 0
    assert g["O11_major_data_defects"] == 0


def test_risk_limits_match_live_risk_config(contract):
    """포트폴리오 한도를 사전등록이 임의로 키우지 않았는지 — 라이브 RiskConfig 와 대조."""
    from src.risk.risk_manager import RiskConfig
    cfg, p = RiskConfig(), contract["portfolio"]
    assert p["max_positions"] == cfg.max_positions
    assert p["max_portfolio_effective_pct"] == cfg.max_portfolio_effective_pct
    assert p["max_symbol_effective_pct"] == cfg.max_symbol_effective_pct
    assert p["max_symbol_notional_pct"] == cfg.max_symbol_notional_pct
    assert contract["risk"]["eod_flatten_minutes_before_close"] == 10
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
    # 산술 근거가 문서에 남아 있어야 한다(왜 차단이 불가능한지)
    assert "5%" in doc


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
