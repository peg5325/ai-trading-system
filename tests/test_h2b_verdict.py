"""H2-b 판정 로직 검증 — 리뷰에서 지적된 결함들을 회귀로 고정한다.

여기서 막는 것:
- 효과 크기 하한이 **부호에 따라 비대칭**으로 적용되던 문제
  (|rho| 가 같은데 부호만으로 "적극적 반증" vs "사실상 0" 으로 갈렸다)
- 레버리지 의존성이 **구조적으로 항상 yes** 이던 문제
  (그룹 키만 잡아 한 그룹 안의 feature 간 부호차를 불일치로 셌다)
- 기준선 없는 지평에서 크래시/오분류
- 일별 재현율이 LOSO 포화(하루 빼도 rho 가 안 움직임)를 대체하는지
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.h2b_backfill_analysis import (
    MIN_PRACTICAL_RHO,
    S001_BASELINE,
    daily_reproduction,
    verdict,
)


def _overall(rows):
    """rows: [(feature, horizon, rho, p)] → h2b['overall'] 형태."""
    return [{"feature": f, "horizon_min": h, "n": 70000, "rho": rho, "p": p,
             "bonferroni_pass": p is not None and p < 0.00357}
            for f, h, rho, p in rows]


def _h2b(overall, by_leverage=None, by_date=None, sign_stability=None):
    return {
        "overall": overall,
        "by_tier": [], "by_symbol": [],
        "by_leverage": by_leverage or [],
        "by_date": by_date or [],
        "sign_stability": sign_stability or [],
        "multiple_comparison": {"tests_run": 14, "alpha": 0.05,
                                "bonferroni_threshold": 0.00357, "survivors": []},
    }


def _coverage(n_symbols=8, candidate_days=40):
    return {"requested_symbols": [], "fetched_symbols": [], "failed_symbols": [],
            "per_symbol": [{"symbol": f"S{i}", "tier": "T1", "leverage_group": "1x",
                            "bars": 40000, "trading_days": candidate_days,
                            "candidate_days": candidate_days, "candidates": 5000}
                           for i in range(n_symbols)]}


_PARITY_OK = {"status": "PASS", "notes": "", "live_candidates": 171,
              "reconstructed_candidates": 171}


def test_effect_size_floor_applies_symmetrically():
    """★ 핵심 회귀: |rho| 가 같으면 부호가 달라도 같은 등급이어야 한다.

    이전 구현은 `reversed` 분기에만 크기 검사가 빠져 있어, |rho|=0.023 이
    부호에 따라 '적극적 반증'과 '사실상 0'으로 갈렸다.
    """
    tiny = MIN_PRACTICAL_RHO / 2
    # s001 기준선은 둘 다 음수 → +tiny 는 역방향, -tiny 는 같은 방향
    v_pos = verdict(_h2b(_overall([("vwap_position", 30, +tiny, 1e-9),
                                   ("vwap_position", 60, +tiny, 1e-9),
                                   ("volume", 30, +tiny, 1e-9),
                                   ("volume", 60, +tiny, 1e-9)])),
                    _coverage(), _PARITY_OK, (30, 60), 20, 6)
    v_neg = verdict(_h2b(_overall([("vwap_position", 30, -tiny, 1e-9),
                                   ("vwap_position", 60, -tiny, 1e-9),
                                   ("volume", 30, -tiny, 1e-9),
                                   ("volume", 60, -tiny, 1e-9)])),
                    _coverage(), _PARITY_OK, (30, 60), 20, 6)
    assert set(v_pos["classes"].values()) == {"negligible"}, \
        "크기가 하한 미만이면 방향과 무관하게 negligible 이어야 한다"
    assert set(v_neg["classes"].values()) == {"negligible"}
    assert v_pos["h2b_replicated"] == v_neg["h2b_replicated"] == "no"


def test_large_reversal_is_still_refutation():
    """크기가 충분하고 유의하며 방향이 반대면 여전히 reversed(적극적 반증)."""
    v = verdict(_h2b(_overall([("vwap_position", 30, +0.30, 1e-20),
                               ("vwap_position", 60, +0.28, 1e-20),
                               ("volume", 30, +0.20, 1e-20),
                               ("volume", 60, +0.25, 1e-20)])),
                _coverage(), _PARITY_OK, (30, 60), 20, 6)
    assert set(v["classes"].values()) == {"reversed"}
    assert v["h2b_replicated"] == "no"


def test_replication_requires_size_sign_and_significance():
    v = verdict(_h2b(_overall([("vwap_position", 30, -0.30, 1e-20),
                               ("vwap_position", 60, -0.28, 1e-20),
                               ("volume", 30, -0.20, 1e-20),
                               ("volume", 60, -0.25, 1e-20)]),
                     by_date=[{"feature": f, "horizon_min": h, "rho": -0.3, "p": 0.01}
                              for f in ("vwap_position", "volume") for h in (30, 60)
                              for _ in range(10)]),
                _coverage(), _PARITY_OK, (30, 60), 20, 6)
    assert set(v["classes"].values()) == {"replicated"}
    assert v["h2b_replicated"] == "yes"


def test_confidence_never_strong_due_to_overlapping_samples():
    """중첩 표본으로 p 가 과대추정이라 strong 을 쓰지 않는다."""
    v = verdict(_h2b(_overall([("vwap_position", 30, -0.30, 1e-30),
                               ("vwap_position", 60, -0.28, 1e-30),
                               ("volume", 30, -0.20, 1e-30),
                               ("volume", 60, -0.25, 1e-30)])),
                _coverage(), _PARITY_OK, (30, 60), 20, 6)
    assert v["confidence"] != "strong"
    assert any("중첩" in x or "iid" in x for x in v["limitations"]), \
        "중첩 표본 한계가 리포트에 명시돼야 한다"


def _lev(rows):
    return [{"group": g, "feature": f, "horizon_min": h, "n": 5000, "rho": rho, "p": 0.01}
            for g, f, h, rho in rows]


def test_leverage_dependency_compares_within_same_feature_and_horizon():
    """★ 핵심 회귀: 그룹 간 부호가 같으면 feature 끼리 달라도 'no' 여야 한다.

    이전 구현은 group 만으로 키를 잡아, 한 그룹 안에서 vwap(+)/volume(−) 이
    다르기만 하면 불일치로 세어 **레버리지와 무관하게 항상 yes** 였다.
    """
    rows = _lev([("1x", "vwap_position", 30, +0.023), ("1x", "volume", 30, -0.080),
                 ("3x_long", "vwap_position", 30, +0.021), ("3x_long", "volume", 30, -0.078)])
    v = verdict(_h2b(_overall([("vwap_position", 30, +0.02, 0.5),
                               ("volume", 30, -0.08, 0.5)]), by_leverage=rows),
                _coverage(), _PARITY_OK, (30,), 20, 6)
    assert v["leverage_dependent"] == "no", \
        "같은 feature 안에서 그룹 부호가 같으면 레버리지 의존이 아니다"


def test_leverage_dependency_detects_real_split():
    """같은 (feature, 지평)에서 그룹 간 부호가 갈리면 yes."""
    rows = _lev([("1x", "vwap_position", 30, +0.05),
                 ("3x_inverse", "vwap_position", 30, -0.05)])
    v = verdict(_h2b(_overall([("vwap_position", 30, +0.02, 0.5)]), by_leverage=rows),
                _coverage(), _PARITY_OK, (30,), 20, 6)
    assert v["leverage_dependent"] == "yes"


def test_unsupported_horizon_is_excluded_not_misclassified():
    """s001 기준선이 없는 지평은 크래시하지도, reversed 로 오분류되지도 않는다."""
    assert ("vwap_position", 45) not in S001_BASELINE
    v = verdict(_h2b(_overall([("vwap_position", 45, +0.30, 1e-20),
                               ("volume", 45, +0.30, 1e-20)])),
                _coverage(), _PARITY_OK, (45,), 20, 6)
    assert set(v["classes"].values()) == {"no_baseline"}
    assert v["h2b_replicated"] != "no", "기준선 없는 지평으로 반증 판정을 내리면 안 된다"


def test_parity_failure_blocks_verdict():
    v = verdict(_h2b(_overall([("vwap_position", 30, -0.30, 1e-20)])),
                _coverage(), {"status": "FAIL", "notes": "재구성 불일치"}, (30,), 20, 6)
    assert v["h2b_replicated"] == "inconclusive"
    assert v["confidence"] == "none"


def test_coverage_gate_uses_candidate_days_median_not_max():
    """한 종목만 길어도 통과하던 문제 — 중앙값 + 후보발생일 기준."""
    cov = _coverage(n_symbols=6, candidate_days=3)
    cov["per_symbol"][0]["candidate_days"] = 105        # 한 종목만 길다
    v = verdict(_h2b(_overall([("vwap_position", 30, -0.30, 1e-20)])),
                cov, _PARITY_OK, (30,), 20, 6)
    assert v["h2b_replicated"] == "inconclusive"
    assert any("커버리지 부족" in x for x in v["limitations"])


def test_daily_reproduction_detects_what_loso_misses():
    """LOSO 포화 반례: 모든 날이 반대 부호인데 LOSO 는 1.0 을 준다.

    일별 재현율은 그 상황을 0.0 으로 정확히 드러내야 한다.
    """
    idx = {("vwap_position", 30): {"feature": "vwap_position", "horizon_min": 30,
                                   "rho": +0.5, "p": 1e-9, "n": 1000}}
    by_date = [{"feature": "vwap_position", "horizon_min": 30, "date": f"2026-07-{d:02d}",
                "rho": -1.0, "p": 0.01, "n": 20} for d in range(1, 11)]
    dr = daily_reproduction({"by_date": by_date}, idx, (30,))
    cell = dr["vwap_position@30"]
    assert cell["days"] == 10
    assert cell["same_sign_days"] == 0
    assert cell["ratio"] == 0.0, "pooled 는 +인데 모든 날이 −면 재현율 0 이어야 한다"
