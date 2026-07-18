"""Spearman 상관 + 다중검정 + 부호 안정성 — **numpy 만** 사용(신규 의존성 0).

왜 직접 구현하나: 이 저장소는 의존성을 최소로 유지하고(httpx/pandas/PyYAML), scipy 는
선언된 의존성이 아니다. 분석 리포트가 "scipy 가 설치된 환경에서만 재현 가능"해지면
재현성이 환경에 인질로 잡힌다. Spearman 은 순위 Pearson 이고 p값은 t 근사라
표준 수치 알고리즘으로 충분히 정확하게 계산할 수 있다.

p값은 scipy.stats.spearmanr 기본 경로와 동일한 **t 근사**를 쓴다:
    t = rho * sqrt((n-2) / (1 - rho^2)),  df = n-2
    p(two-sided) = I_{df/(df+t^2)}(df/2, 1/2)        (정칙화 불완전 베타)
검증: 선행 리포트(scipy 산출)의 (rho, n) → p 를 재현하는지 테스트로 고정한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# 이 저장소의 공식 검증 경로(random CV 금지)와 정합.
from src.history.evaluation import OFFICIAL_SPLIT_METHODS, RANDOM_CV_WATERMARK  # noqa: F401


@dataclass(frozen=True)
class CorrelationResult:
    n: int
    rho: float | None
    p: float | None

    def to_dict(self) -> dict:
        return {"n": self.n, "rho": self.rho, "p": self.p}


def _rankdata(a: np.ndarray) -> np.ndarray:
    """평균 순위(동점은 평균) — scipy.stats.rankdata(method='average') 와 동일."""
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a), dtype=float)
    ranks[order] = np.arange(1, len(a) + 1, dtype=float)
    # 동점 처리: 같은 값 구간의 순위를 평균으로 덮어쓴다.
    sorted_a = a[order]
    i = 0
    n = len(a)
    while i < n:
        j = i
        while j + 1 < n and sorted_a[j + 1] == sorted_a[i]:
            j += 1
        if j > i:
            avg = (i + j + 2) / 2.0        # 1-based 순위 평균
            ranks[order[i:j + 1]] = avg
        i = j + 1
    return ranks


def _betacf(a: float, b: float, x: float, *, itmax: int = 300,
            eps: float = 3e-16, fpmin: float = 1e-300) -> float:
    """정칙화 불완전 베타의 연분수(Lentz 알고리즘, Numerical Recipes)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """I_x(a, b). 0<=x<=1."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_sf_two_sided(t: float, df: float) -> float:
    """P(|T| > |t|) — Student t 양측 꼬리확률."""
    if df <= 0:
        return float("nan")
    t = abs(float(t))
    if t == 0.0:
        return 1.0
    if not math.isfinite(t):
        return 0.0
    return regularized_incomplete_beta(df / 2.0, 0.5, df / (df + t * t))


def spearman(x, y) -> CorrelationResult:
    """Spearman rho + 양측 p. 표본 부족/상수열이면 rho/p 는 None."""
    xa = np.asarray(x, dtype=float)
    ya = np.asarray(y, dtype=float)
    if xa.shape != ya.shape:
        raise ValueError("x, y 길이가 다르다")
    mask = np.isfinite(xa) & np.isfinite(ya)
    xa, ya = xa[mask], ya[mask]
    n = int(xa.size)
    if n < 3:
        return CorrelationResult(n=n, rho=None, p=None)
    rx, ry = _rankdata(xa), _rankdata(ya)
    sx, sy = rx.std(), ry.std()
    if sx == 0.0 or sy == 0.0:
        # 상수열(예: 모든 후보의 컴포넌트 점수가 동일) — 상관 정의 불가.
        return CorrelationResult(n=n, rho=None, p=None)
    rho = float(np.corrcoef(rx, ry)[0, 1])
    rho = max(-1.0, min(1.0, rho))
    if abs(rho) >= 1.0:
        return CorrelationResult(n=n, rho=rho, p=0.0)
    df = n - 2
    t = rho * math.sqrt(df / (1.0 - rho * rho))
    return CorrelationResult(n=n, rho=rho, p=float(student_t_sf_two_sided(t, df)))


def bonferroni_threshold(alpha: float, tests: int) -> float:
    if tests <= 0:
        raise ValueError("tests 는 1 이상")
    return alpha / tests


@dataclass(frozen=True)
class SignStability:
    """LOSO(leave-one-day-out) fold 별 rho 부호 일치율.

    단일 p값보다 이 지표가 재현성의 실질 근거다: 하루가 통째로 빠져도 방향이 유지되는가.
    """
    feature: str
    horizon_min: int
    method: str
    folds: int
    same_sign_folds: int
    same_sign_ratio: float
    overall_sign: int
    fold_rhos: list[float]

    def to_dict(self) -> dict:
        return {"feature": self.feature, "horizon_min": self.horizon_min,
                "method": self.method, "folds": self.folds,
                "same_sign_folds": self.same_sign_folds,
                "same_sign_ratio": self.same_sign_ratio,
                "overall_sign": self.overall_sign,
                "fold_rhos": [round(r, 6) for r in self.fold_rhos]}


def loso_sign_stability(groups: dict, feature: str, horizon_min: int) -> SignStability | None:
    """groups: day -> (xs, ys). 각 fold = 그 날을 **뺀** 나머지로 rho 계산.

    leave-one-day-out 이므로 fold rho 는 '그 날 없이도 방향이 유지되는가'를 본다.
    """
    days = sorted(groups)
    if len(days) < 2:
        return None
    all_x = [v for d in days for v in groups[d][0]]
    all_y = [v for d in days for v in groups[d][1]]
    overall = spearman(all_x, all_y)
    if overall.rho is None:
        return None
    overall_sign = 1 if overall.rho > 0 else (-1 if overall.rho < 0 else 0)
    fold_rhos: list[float] = []
    for held in days:
        xs = [v for d in days if d != held for v in groups[d][0]]
        ys = [v for d in days if d != held for v in groups[d][1]]
        r = spearman(xs, ys)
        if r.rho is not None:
            fold_rhos.append(r.rho)
    if not fold_rhos:
        return None
    same = sum(1 for r in fold_rhos
               if (1 if r > 0 else (-1 if r < 0 else 0)) == overall_sign)
    return SignStability(
        feature=feature, horizon_min=horizon_min, method="loso_by_day",
        folds=len(fold_rhos), same_sign_folds=same,
        same_sign_ratio=same / len(fold_rhos), overall_sign=overall_sign,
        fold_rhos=fold_rhos)


def assert_official_method(method: str) -> str:
    """공식 검증 경로 강제 — random CV 는 거부한다."""
    if method not in OFFICIAL_SPLIT_METHODS:
        raise ValueError(
            f"'{method}' 는 공식 검증 방법이 아니다. 허용: {OFFICIAL_SPLIT_METHODS}. "
            f"(random split 은 시계열 누수 — {RANDOM_CV_WATERMARK})")
    return method
