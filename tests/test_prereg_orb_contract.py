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
    """임계값이 문서와 contract 에서 갈리면 어느 쪽이 사전등록인지 모호해진다."""
    assert str(contract["candidate"]["rvol"]["threshold"]) in doc            # 1.5
    assert "0.30%" in doc and contract["candidate"]["gap"]["threshold_abs"] == 0.0030
    assert str(contract["risk"]["target_r_multiple"]) in doc                 # 2.0
    assert str(contract["portfolio"]["max_positions"]) in doc                # 2
    assert str(contract["signal"]["max_entry_minutes_before_close"]) in doc  # 90


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
    assert g["O3_min_trading_days"] == 60 and g["O4_min_trades"] == 100
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


def test_no_orb_result_artifacts_exist_yet():
    """ORB 성과·OOS·unlock 산출물이 하나라도 있으면 사전등록의 전제가 깨진다."""
    hits = []
    for name in sorted(os.listdir(RESEARCH)):
        if not _RESULT_PATTERNS.search(name):
            continue
        if "ORB" in name.upper() or "P0_US30_ORB" in name.upper():
            hits.append(name)
    assert hits == [], f"ORB 결과 산출물이 이미 존재한다: {hits}"


def test_no_orb_strategy_implementation_exists_yet():
    """3/6 은 문서만 만든다 — 구현이 있으면 단계 경계가 깨진 것이다."""
    found = []
    for base in ("src", "analysis"):
        root = os.path.join(REPO, base)
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn.endswith(".py") and "orb" in fn.lower():
                    found.append(os.path.relpath(os.path.join(dirpath, fn), REPO))
    assert found == [], f"ORB 구현 파일이 이미 존재한다: {found}"


def test_prereg_declares_what_it_did_not_do(doc):
    for phrase in ("성과 계산", "forward return", "blind OOS"):
        assert phrase in doc
    assert "§16" in doc or "이 문서가 하지 않은 것" in doc
