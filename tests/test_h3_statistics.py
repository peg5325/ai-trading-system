"""H3 판정에 쓰는 통계 — Mann-Whitney U 와 부호검정 (numpy 만, scipy 미사용).

이 두 함수가 H3 리포트의 판정 축이다. 특히 `sign_test` 는 **의사복제 교정**의 핵심이다:
국면 관측은 5분 간격 결정봉에 30분~3일 지평이라 창이 겹치고, 같은 날 12 종목이 같은 시장을
공유하며, 국면 라벨 자체가 하루 단위로 상수다. 관측 단위 검정은 이 중복을 독립으로 착각해
p 를 천문학적으로 과대추정한다(실측: 유효표본 16일인데 관측 단위 p=1.57e-45). 하루를
블록 1개로 묶어 부호검정을 쓰면 유효표본이 정직해진다.

기대값은 손계산으로 고정한다 — scipy 가 의존성이 아니라 참조 구현을 쓸 수 없다.
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.correlation import mann_whitney_u, normal_sf_two_sided, sign_test


# ---------------------------------------------------------------------------
# Mann-Whitney U
# ---------------------------------------------------------------------------

def test_u_statistic_matches_hand_calculation():
    """x=[1,3,5,7], y=[2,4,6,8] → R1=16, U1 = 16 − 4·5/2 = 6."""
    r = mann_whitney_u([1, 3, 5, 7], [2, 4, 6, 8])
    assert r.n1 == 4 and r.n2 == 4
    assert r.u == pytest.approx(6.0)
    # rank-biserial = 2U/(n1·n2) − 1 = 12/16 − 1 = −0.25
    assert r.rank_biserial == pytest.approx(-0.25)


def test_complete_separation_gives_extreme_rank_biserial():
    """x 가 전부 y 보다 작으면 U=0, rank-biserial = −1 (완전 분리)."""
    r = mann_whitney_u([1, 2, 3, 4, 5], [6, 7, 8, 9, 10])
    assert r.u == pytest.approx(0.0)
    assert r.rank_biserial == pytest.approx(-1.0)
    r2 = mann_whitney_u([6, 7, 8, 9, 10], [1, 2, 3, 4, 5])
    assert r2.rank_biserial == pytest.approx(1.0)


def test_identical_distributions_give_zero_effect():
    a = list(range(50))
    r = mann_whitney_u(a, list(a))
    assert r.rank_biserial == pytest.approx(0.0, abs=1e-12)
    assert r.p == pytest.approx(1.0, abs=0.05)


def test_ties_use_average_ranks_in_u():
    """x=[1,1,2], y=[1,2,2] → R1 = 2+2+5 = 9, U1 = 9 − 6 = 3."""
    r = mann_whitney_u([1, 1, 2], [1, 2, 2])
    assert r.u == pytest.approx(3.0)


def test_all_tied_returns_none_p_not_zero():
    """전부 같은 값이면 분포 차이를 말할 수 없다 — 0 이 아니라 None."""
    r = mann_whitney_u([5.0] * 10, [5.0] * 10)
    assert r.p is None
    assert r.rank_biserial == pytest.approx(0.0)


def _uncorrected_p(u: float, n1: int, n2: int) -> float:
    """동점 보정을 뺀 정규근사 p — 비교 기준."""
    var = n1 * n2 * (n1 + n2 + 1) / 12.0
    z = max((abs(u - n1 * n2 / 2.0) - 0.5) / math.sqrt(var), 0.0)
    return normal_sf_two_sided(z)


def test_tie_correction_restores_the_correct_null_variance():
    """동점은 U 의 귀무분산을 **줄인다** → 보정 p 는 무보정 p 보다 작거나 같다.

    보정을 빼면 분산을 과대평가해 과도하게 보수적인(큰) p 가 나온다. 수익률 0 같은
    동점이 흔한 이 데이터에서 그 손실은 무시할 수 없다.
    """
    x = [0.0] * 40 + [1.0] * 10
    y = [0.0] * 40 + [1.0] * 20
    r = mann_whitney_u(x, y)
    assert r.p <= _uncorrected_p(r.u, len(x), len(y)) + 1e-12
    assert r.p < _uncorrected_p(r.u, len(x), len(y)), "동점이 많은데 보정 효과가 없다"


def test_no_ties_means_correction_is_a_noop():
    """동점이 없으면 보정항이 0 이라 무보정과 같아야 한다."""
    x = [1.0, 3.0, 5.0, 7.0, 9.0]
    y = [2.0, 4.0, 6.0, 8.0, 10.0]
    r = mann_whitney_u(x, y)
    assert r.p == pytest.approx(_uncorrected_p(r.u, len(x), len(y)))


def test_empty_group_returns_none():
    r = mann_whitney_u([], [1, 2, 3])
    assert r.p is None and r.u is None and r.n1 == 0


def test_non_finite_values_are_dropped():
    r = mann_whitney_u([1.0, float("nan"), 3.0], [2.0, float("inf")])
    assert r.n1 == 2 and r.n2 == 1


# ---------------------------------------------------------------------------
# 부호검정 (하루 블록)
# ---------------------------------------------------------------------------

def test_sign_test_all_same_direction():
    """5일 전부 같은 부호 → p = 2·(1/2)^5 = 0.0625."""
    assert sign_test(5, 5) == pytest.approx(0.0625)
    assert sign_test(0, 5) == pytest.approx(0.0625), "반대 방향도 같은 양측 p"


def test_sign_test_ten_of_ten():
    assert sign_test(10, 10) == pytest.approx(2.0 / 1024.0)


def test_sign_test_even_split_is_p_one():
    """절반이면 방향 증거가 전혀 없다 — p 는 1 로 캡된다."""
    assert sign_test(5, 10) == pytest.approx(1.0)
    assert sign_test(15, 30) == pytest.approx(1.0)


def test_sign_test_is_symmetric():
    for k in range(0, 21):
        assert sign_test(k, 20) == pytest.approx(sign_test(20 - k, 20))


def test_sign_test_needs_many_days_to_reach_significance():
    """★ 유효표본이 작으면 부호가 완벽해도 유의해지지 않는다.

    이 성질이 H3 판정의 핵심이다 — 관측 5만 건이 아니라 '며칠이냐'가 검정력을 정한다.
    Bonferroni 임계(60검정, α=0.05 → 8.3e-4)를 넘으려면 완벽한 부호로도 12일이 필요하다.
    """
    thr = 0.05 / 60
    assert sign_test(8, 8) > thr, "8일 완벽 부호로는 보정 임계를 넘지 못한다"
    assert sign_test(12, 12) < thr, "12일 완벽 부호면 넘는다"


def test_sign_test_rejects_invalid_input():
    assert sign_test(3, 0) is None
    assert sign_test(5, 3) is None
    assert sign_test(-1, 5) is None


# ---------------------------------------------------------------------------
# 정규 꼬리확률
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("z, expected", [
    (0.0, 1.0),
    (1.0, 0.31731050786),
    (1.959963985, 0.05),
    (2.575829304, 0.01),
    (3.0, 0.00269979606),
])
def test_normal_sf_two_sided_reference_values(z, expected):
    assert normal_sf_two_sided(z) == pytest.approx(expected, rel=1e-6)


def test_normal_sf_is_symmetric():
    assert normal_sf_two_sided(-2.5) == pytest.approx(normal_sf_two_sided(2.5))
