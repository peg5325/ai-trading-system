"""Spearman/다중검정/부호안정성 수치 검증.

핵심: scipy 없이 구현한 p값이 **scipy 산출값과 일치**하는지를 고정한다.
기준점은 선행 리포트 `reports/research/h2-h4/h2_h3_preliminary_s001.md` §1 의 (rho, n) → p 로,
그 표는 scipy.stats.spearmanr 로 만들어졌다. 우리 구현이 같은 답을 내야
"scipy 없이도 같은 결론"이라고 말할 수 있다.
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.correlation import (
    binom_upper_tail,
    bonferroni_threshold,
    assert_official_method,
    loso_sign_stability,
    regularized_incomplete_beta,
    spearman,
    student_t_sf_two_sided,
)


def test_perfect_monotonic_relations():
    assert spearman([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]).rho == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4, 5], [50, 40, 30, 20, 10]).rho == pytest.approx(-1.0)


def test_spearman_uses_ranks_not_values():
    """비선형 단조 변환에 불변 — Pearson 과 구별되는 성질."""
    x = [1, 2, 3, 4, 5, 6]
    y = [1, 4, 9, 16, 25, 36]
    assert spearman(x, y).rho == pytest.approx(1.0)


def test_ties_use_average_ranks():
    r = spearman([1, 1, 2, 3], [1, 1, 2, 3])
    assert r.rho == pytest.approx(1.0)


def test_incomplete_beta_known_values():
    """I_x(a,b) 기준값 — 대칭성/경계."""
    assert regularized_incomplete_beta(0.5, 0.5, 0.0) == 0.0
    assert regularized_incomplete_beta(0.5, 0.5, 1.0) == 1.0
    # I_0.5(1,1) = 0.5 (균등분포 CDF)
    assert regularized_incomplete_beta(1.0, 1.0, 0.5) == pytest.approx(0.5, abs=1e-12)
    # I_x(a,b) = 1 - I_{1-x}(b,a)
    assert regularized_incomplete_beta(2.0, 3.0, 0.3) == pytest.approx(
        1.0 - regularized_incomplete_beta(3.0, 2.0, 0.7), abs=1e-12)


def test_student_t_two_sided_reference_values():
    """t 분포 양측 꼬리 — 알려진 임계값."""
    # df=∞ 근사가 아닌 실제 값: t=1.96, df=1e6 → p≈0.05
    assert student_t_sf_two_sided(1.959964, 1_000_000) == pytest.approx(0.05, abs=1e-4)
    # df=1 (Cauchy): P(|T|>1) = 0.5
    assert student_t_sf_two_sided(1.0, 1) == pytest.approx(0.5, abs=1e-9)
    # df=10, t=2.228 → p≈0.05 (t 표)
    assert student_t_sf_two_sided(2.228139, 10) == pytest.approx(0.05, abs=1e-5)


# 표본 수 주의: 선행 리포트는 30m 완결 156건 / 60m 완결 140건이다.
# 지평마다 n 이 다르므로 지평을 섞어 n 을 재사용하면 p 가 어긋난다.
@pytest.mark.parametrize("rho,n,expected_p", [
    # reports/research/h2-h4/h2_h3_preliminary_s001.md §1 (scipy 산출) 재현
    (-0.162, 156, 0.044),    # volume 30m
    (0.178, 156, 0.026),     # momentum 30m
    (-0.329, 156, 0.000),    # vwap_position 30m
    (-0.249, 140, 0.003),    # volume 60m
    (0.196, 140, 0.020),     # volatility 60m
    (-0.285, 140, 0.001),    # vwap_position 60m
])
def test_p_value_matches_scipy_reference(rho, n, expected_p):
    """선행 리포트의 scipy p값을 우리 t근사가 재현한다(반올림 자리수 이내)."""
    df = n - 2
    t = rho * math.sqrt(df / (1.0 - rho * rho))
    p = student_t_sf_two_sided(t, df)
    assert p == pytest.approx(expected_p, abs=0.001), f"rho={rho} n={n}: {p} vs {expected_p}"


def test_insufficient_or_constant_input_returns_none_not_zero():
    """표본 부족/상수열에서 rho=0 을 지어내지 않는다."""
    assert spearman([1, 2], [1, 2]).rho is None
    assert spearman([5, 5, 5, 5], [1, 2, 3, 4]).rho is None
    assert spearman([], []).n == 0


def test_non_finite_pairs_are_dropped():
    r = spearman([1, 2, 3, float("nan"), 5], [1, 2, 3, 4, 5])
    assert r.n == 4


def test_bonferroni_threshold():
    assert bonferroni_threshold(0.05, 14) == pytest.approx(0.05 / 14)
    assert bonferroni_threshold(0.05, 14) == pytest.approx(0.003571, abs=1e-6)
    with pytest.raises(ValueError):
        bonferroni_threshold(0.05, 0)


def test_loso_sign_stability_all_consistent():
    """모든 날에 같은 방향이면 일치율 1.0."""
    groups = {f"2026-07-{d:02d}": ([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) for d in range(1, 6)}
    st = loso_sign_stability(groups, "vwap_position", 30)
    assert st.folds == 5
    assert st.overall_sign == -1
    assert st.same_sign_ratio == 1.0


def test_loso_sign_stability_detects_instability():
    """한 날이 전체 부호를 뒤집을 만큼 지배적이면 일치율이 떨어진다."""
    groups = {
        "2026-07-01": ([1, 2, 3], [1, 2, 3]),
        "2026-07-02": ([1, 2, 3], [1, 2, 3]),
        # 반대 방향의 큰 표본
        "2026-07-03": (list(range(20)), list(range(20))[::-1]),
    }
    st = loso_sign_stability(groups, "f", 30)
    assert st.folds == 3
    assert st.same_sign_ratio < 1.0, "지배적인 날을 빼면 부호가 바뀌어야 한다"


def test_loso_requires_two_days():
    assert loso_sign_stability({"2026-07-01": ([1, 2, 3], [1, 2, 3])}, "f", 30) is None


def test_binom_upper_tail_matches_hand_computed_values():
    """P(X >= k) 정확검정 — 다중검정의 '유의 칸이 몇 개'를 재는 기준선."""
    # k=0 이면 전체 확률.
    assert binom_upper_tail(0, 10, 0.05) == pytest.approx(1.0)
    # k=n 이면 p^n.
    assert binom_upper_tail(3, 3, 0.5) == pytest.approx(0.125)
    # 전수합과 일치.
    total = sum(math.comb(10, i) * 0.05 ** i * 0.95 ** (10 - i) for i in range(2, 11))
    assert binom_upper_tail(2, 10, 0.05) == pytest.approx(total)
    # 단조감소 — k 가 클수록 꼬리는 얇아진다.
    tails = [binom_upper_tail(k, 42, 0.05) for k in range(0, 8)]
    assert all(a >= b for a, b in zip(tails, tails[1:]))


def test_binom_upper_tail_separates_impossible_from_undefined():
    """★ '불가능(0.0)'과 '판정 불가(None)'는 다른 답이다.

    `k > n` 은 P(X>=k)=0 인 불가능 사건이지 미정의가 아니다. 둘을 같은 None 으로 뭉개면
    호출부가 "절대 안 일어난다"와 "계산 못 했다"를 구분할 수 없다.
    """
    assert binom_upper_tail(5, 3, 0.05) == 0.0        # 불가능 사건
    assert binom_upper_tail(3, 0, 0.05) is None       # n=0 — 확률이 정의되지 않는다
    assert binom_upper_tail(1, 10, 1.5) is None       # p 범위 밖


def test_binom_upper_tail_explains_h3b_noise_level():
    """H3-b 실측(42칸 중 4칸 명목 유의)이 우연으로 설명되는지 — 판정에 직접 쓰인 값."""
    p = binom_upper_tail(4, 42, 0.05)
    assert p > 0.05, "4/42 는 귀무가설에서 흔한 값이라 '신호 있음'의 근거가 못 된다"
    # 반대로 유의 칸이 크게 늘면 우연으로 설명되지 않는다.
    assert binom_upper_tail(10, 42, 0.05) < 0.05


def test_random_cv_is_rejected():
    """공식 경로 강제 — random split 금지."""
    for m in ("chronological", "embargo", "loso"):
        assert assert_official_method(m) == m
    with pytest.raises(ValueError) as e:
        assert_official_method("random")
    assert "INVALID FOR MODEL SELECTION" in str(e.value)
