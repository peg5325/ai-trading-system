"""Phase 6 승격조건 판정 로직 테스트(순수 로직, PR #38).

promotion.py 는 I/O 없는 순수 판정기 — SessionSignal(구조화 입력)만 소비한다.
실 Phase 6 데이터가 없어도 이 계약이 CI 로 강제된다(계약서 §"실행 가능한 테스트로 박을 것").
"""
from __future__ import annotations

import pytest

from src.shadow.promotion import (
    DEFER,
    FAIL,
    GATE_NOT_MET,
    GATE_PENDING,
    MIN_DISTINCT_REGIMES,
    MIN_FULL_SESSIONS_PER_MARKET,
    PASS,
    PromotionReport,
    SessionSignal,
    classify_session_regime,
    evaluate_promotion,
)


def _sig(session_id, *, market="US", completion="FULL", manual=0, regime="UP",
         avoided=0.02, missed=0.01, n_blocked=3, started_at=None):
    return SessionSignal(
        session_id=session_id, market=market, completion_status=completion,
        manual_flatten_count=manual, regime=regime, started_at=started_at,
        blocked_avoided_loss=avoided, blocked_missed_profit=missed, n_blocked=n_blocked)


def _crit(report: PromotionReport, key: str):
    for c in report.criteria:
        if c.key == key:
            return c
    raise AssertionError(f"criterion {key!r} not in report ({[c.key for c in report.criteria]})")


# ── 기본 5 FULL·다양 regime·양의 net·분산 → auto 전부 PASS, #5 만 DEFER ──
def _healthy_signals():
    regimes = ["UP", "DOWN", "FLAT", "UP", "DOWN"]
    return [
        _sig(f"s{i}", regime=regimes[i], avoided=0.02, missed=0.01,
             started_at=f"2026-08-0{i + 1}T13:30:00Z")
        for i in range(5)
    ]


def test_partial_and_manual_flatten_excluded_from_primary():
    signals = _healthy_signals() + [
        _sig("partial1", completion="PARTIAL"),
        _sig("manual1", manual=2),
        _sig("aborted1", completion="ABORTED"),
    ]
    report = evaluate_promotion(signals, policy_id="A", horizon_min=30, market="US")
    crit = _crit(report, "partial_excluded")
    excluded_ids = {e["session_id"] for e in crit.evidence["excluded"]}
    assert excluded_ids == {"partial1", "manual1", "aborted1"}
    # full_sessions 는 primary(FULL·manual0)만 카운트해야 한다
    assert _crit(report, "full_sessions").evidence["n_full"] == 5


def test_full_sessions_pass_at_threshold_and_fail_below():
    ok = evaluate_promotion(_healthy_signals(), policy_id="A", horizon_min=30, market="US")
    assert _crit(ok, "full_sessions").status == PASS
    assert _crit(ok, "full_sessions").evidence["required"] == MIN_FULL_SESSIONS_PER_MARKET

    four = evaluate_promotion(_healthy_signals()[:4], policy_id="A", horizon_min=30, market="US")
    assert _crit(four, "full_sessions").status == FAIL
    assert _crit(four, "full_sessions").evidence["n_full"] == 4


def test_regime_diversity_requires_two_distinct():
    same = [_sig(f"s{i}", regime="UP", started_at=f"2026-08-0{i + 1}T13:30:00Z")
            for i in range(5)]
    r_same = evaluate_promotion(same, policy_id="A", horizon_min=30, market="US")
    assert _crit(r_same, "regime_diversity").status == FAIL
    assert _crit(r_same, "regime_diversity").evidence["n_distinct"] == 1

    r_div = evaluate_promotion(_healthy_signals(), policy_id="A", horizon_min=30, market="US")
    d = _crit(r_div, "regime_diversity")
    assert d.status == PASS
    assert d.evidence["n_distinct"] >= MIN_DISTINCT_REGIMES


def test_regime_none_not_counted_as_distinct():
    sigs = [_sig(f"s{i}", regime=None, started_at=f"2026-08-0{i + 1}T13:30:00Z")
            for i in range(5)]
    r = evaluate_promotion(sigs, policy_id="A", horizon_min=30, market="US")
    assert _crit(r, "regime_diversity").evidence["n_distinct"] == 0
    assert _crit(r, "regime_diversity").status == FAIL


def test_avoided_gt_missed_repeated_pass_and_fail():
    ok = evaluate_promotion(_healthy_signals(), policy_id="A", horizon_min=30, market="US")
    c = _crit(ok, "avoided_gt_missed")
    assert c.status == PASS
    assert c.evidence["aggregate_net"] > 0

    # 총합은 음수 → FAIL
    neg = [_sig(f"s{i}", regime=["UP", "DOWN"][i % 2], avoided=0.01, missed=0.02,
                started_at=f"2026-08-0{i + 1}T13:30:00Z") for i in range(5)]
    r = evaluate_promotion(neg, policy_id="A", horizon_min=30, market="US")
    assert _crit(r, "avoided_gt_missed").status == FAIL


def test_avoided_gt_missed_fails_when_not_repeated():
    # 총합은 양수지만 한 세션만 강하게 양수, 나머지는 음수 → '반복적으로 큼' 불충족
    sigs = [
        _sig("big", regime="UP", avoided=0.50, missed=0.0,
             started_at="2026-08-01T13:30:00Z"),
        *[_sig(f"s{i}", regime=["DOWN", "FLAT"][i % 2], avoided=0.0, missed=0.02,
               started_at=f"2026-08-0{i + 2}T13:30:00Z") for i in range(4)],
    ]
    r = evaluate_promotion(sigs, policy_id="A", horizon_min=30, market="US")
    assert _crit(r, "avoided_gt_missed").status == FAIL


def test_avoided_gt_missed_denominator_is_all_primary_conservative():
    # 3 세션 강한 net>0(차단신호 있음), 2 세션 이 policy 미차단(중립 net=0, n_blocked=0).
    # 분모=전체 primary(중립 포함) → ratio=3/5=0.6 경계. 중립 세션이 희석하는 보수적 정의를 고정.
    strong = [_sig(f"p{i}", regime=["UP", "DOWN", "FLAT"][i % 3], avoided=0.03, missed=0.0,
                   n_blocked=2, started_at=f"2026-08-0{i + 1}T13:30:00Z") for i in range(3)]
    neutral = [_sig(f"n{i}", regime="UP", avoided=0.0, missed=0.0, n_blocked=0,
                    started_at=f"2026-08-0{i + 4}T13:30:00Z") for i in range(2)]
    r = evaluate_promotion(strong + neutral, policy_id="A", horizon_min=30, market="US")
    c = _crit(r, "avoided_gt_missed")
    assert c.evidence["n_primary"] == 5
    assert c.evidence["n_acting"] == 3        # 중립 2세션은 차단신호 없음
    assert c.evidence["n_neutral"] == 2
    assert c.evidence["positive_ratio"] == pytest.approx(0.6)
    assert c.status == PASS                    # 경계(0.6) 통과


def test_no_single_dominance_all_zero_nets_pass():
    # 모든 net==0 → Σ|net|==0 → div-by-zero 가드 → PASS(지배 없음, 신호 부재)
    sigs = [_sig(f"z{i}", regime=["UP", "DOWN"][i % 2], avoided=0.0, missed=0.0,
                 n_blocked=0, started_at=f"2026-08-0{i + 1}T13:30:00Z") for i in range(5)]
    r = evaluate_promotion(sigs, policy_id="A", horizon_min=30, market="US")
    dom = _crit(r, "no_single_dominance")
    assert dom.status == PASS
    assert dom.evidence["max_share"] == 0.0
    assert dom.evidence["dominant_session"] is None
    # 총 net 0 → avoided_gt_missed FAIL → 게이트는 NOT_MET(false-promotion 없음)
    assert _crit(r, "avoided_gt_missed").status == FAIL


def test_no_single_session_dominance():
    ok = evaluate_promotion(_healthy_signals(), policy_id="A", horizon_min=30, market="US")
    assert _crit(ok, "no_single_dominance").status == PASS

    dom = [
        _sig("big", regime="UP", avoided=1.0, missed=0.0,
             started_at="2026-08-01T13:30:00Z"),
        *[_sig(f"s{i}", regime=["DOWN", "FLAT"][i % 2], avoided=0.01, missed=0.0,
               started_at=f"2026-08-0{i + 2}T13:30:00Z") for i in range(4)],
    ]
    r = evaluate_promotion(dom, policy_id="A", horizon_min=30, market="US")
    c = _crit(r, "no_single_dominance")
    assert c.status == FAIL
    assert c.evidence["max_share"] > 0.5
    assert c.evidence["dominant_session"] == "big"


def test_direction_stability_pass_when_sign_consistent():
    ok = evaluate_promotion(_healthy_signals(), policy_id="A", horizon_min=30, market="US")
    c = _crit(ok, "direction_stability")
    assert c.status == PASS
    assert c.evidence["loso_stable"] is True
    assert c.evidence["chronological_stable"] is True


def test_direction_stability_fails_when_leave_one_out_flips_sign():
    # 한 세션이 부호를 좌우 → LOSO 로 부호 뒤집힘
    sigs = [
        _sig("flip", regime="UP", avoided=0.30, missed=0.0,
             started_at="2026-08-01T13:30:00Z"),
        *[_sig(f"s{i}", regime=["DOWN", "FLAT"][i % 2], avoided=0.0, missed=0.05,
               started_at=f"2026-08-0{i + 2}T13:30:00Z") for i in range(4)],
    ]
    r = evaluate_promotion(sigs, policy_id="A", horizon_min=30, market="US")
    assert _crit(r, "direction_stability").status == FAIL


def test_cost_mdd_is_always_defer():
    r = evaluate_promotion(_healthy_signals(), policy_id="A", horizon_min=30, market="US")
    c = _crit(r, "cost_mdd")
    assert c.status == DEFER


def test_gate_pending_when_only_defer_remains():
    r = evaluate_promotion(_healthy_signals(), policy_id="A", horizon_min=30, market="US")
    # auto 조건 전부 PASS + #5 DEFER → 게이트는 MET 이 아니라 PENDING(수동 판정 잔여)
    assert r.gate_status == GATE_PENDING
    assert not r.gate_passed


def test_gate_not_met_on_any_fail():
    r = evaluate_promotion(_healthy_signals()[:3], policy_id="A", horizon_min=30, market="US")
    assert r.gate_status == GATE_NOT_MET


def test_market_filter_isolates_signals():
    us = _healthy_signals()
    kr = [_sig(f"k{i}", market="KR", regime="UP", started_at=f"2026-08-0{i + 1}T05:30:00Z")
          for i in range(2)]
    r = evaluate_promotion(us + kr, policy_id="A", horizon_min=30, market="US")
    assert _crit(r, "full_sessions").evidence["n_full"] == 5


def test_determinism_same_input_same_report():
    a = evaluate_promotion(_healthy_signals(), policy_id="A", horizon_min=30, market="US")
    b = evaluate_promotion(_healthy_signals(), policy_id="A", horizon_min=30, market="US")
    assert a.to_dict() == b.to_dict()


def test_classify_session_regime_mode():
    assert classify_session_regime(["UP", "UP", "DOWN"]) == "UP"
    assert classify_session_regime([None, None]) is None
    assert classify_session_regime([]) is None
    # 결정론적 tie-break(사전순) — UP vs DOWN 동수면 사전순 앞선 DOWN
    assert classify_session_regime(["UP", "DOWN"]) == "DOWN"


def test_report_carries_policy_and_horizon():
    r = evaluate_promotion(_healthy_signals(), policy_id="B", horizon_min=60, market="US")
    assert r.policy_id == "B"
    assert r.horizon_min == 60
    assert r.market == "US"
