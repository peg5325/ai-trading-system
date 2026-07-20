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
from src.history.evaluation import OFFICIAL_SPLIT_METHODS, RANDOM_CV_WATERMARK


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


def sign_test(successes: int, n: int) -> float | None:
    """부호검정 양측 p — H0: 성공확률 0.5. n 이 작을 때(수십 일) 정확검정을 쓴다.

    왜 필요한가: 국면 관측은 5분 간격 결정봉에 30분~3일 지평이라 **심하게 중첩**되고,
    같은 날 같은 시장을 보는 12 종목이 동시에 들어간다. 관측 단위 p 값은 이 중복을
    독립 표본으로 착각해 천문학적으로 과대추정된다(실측: 하루 단위 국면에서 유효표본
    16일인데 관측 단위 p=1e-45). 하루를 **블록 1개**로 묶어 이 검정을 쓰면 유효표본이
    정직해진다.
    """
    if n <= 0 or not 0 <= successes <= n:
        return None
    k = max(successes, n - successes)
    tail = sum(math.comb(n, i) for i in range(k, n + 1)) / (2.0 ** n)
    return min(1.0, 2.0 * tail)


def normal_sf_two_sided(z: float) -> float:
    """P(|Z| > |z|) — 표준정규 양측 꼬리확률."""
    return math.erfc(abs(float(z)) / math.sqrt(2.0))


@dataclass(frozen=True)
class MannWhitneyResult:
    """두 국면 그룹(UP/DOWN)의 forward return 분포가 다른가.

    `rank_biserial` 을 함께 내는 이유: H3 판정은 **효과 크기**로 해야 하는데 p 는 n 이 크면
    무의미한 차이에도 0 에 붙는다. rank-biserial = 2*U/(n1*n2) - 1 은 "무작위로 뽑은 UP 관측이
    DOWN 관측보다 클 확률"을 -1..+1 로 재척도한 값이라 n 에 휘둘리지 않는다.
    """
    n1: int
    n2: int
    u: float | None
    p: float | None
    rank_biserial: float | None

    def to_dict(self) -> dict:
        return {"n1": self.n1, "n2": self.n2, "u": self.u, "p": self.p,
                "rank_biserial": self.rank_biserial}


def mann_whitney_u(x, y) -> MannWhitneyResult:
    """Mann-Whitney U (양측, 정규근사 + 동점 보정).

    scipy 를 쓰지 않는 이유는 `spearman` 과 같다 — 이 저장소는 scipy 를 의존성으로 선언하지
    않으며, 분석 재현이 환경에 인질로 잡히면 안 된다.

    정규근사를 쓰는 근거: H3 표본은 그룹당 수천~수만이라 정확검정이 불필요하다
    (근사 오차가 표본 중첩으로 인한 p 과대추정보다 훨씬 작다).

    동점 보정이 필요한 이유: 동점은 U 의 **귀무분포 분산을 줄인다**. 보정하지 않으면
    분산을 과대평가해 z 가 작아지고 p 가 필요 이상으로 커진다(과도하게 보수적 →
    검정력 손실). 보정항 `sum(t^3 - t)` 를 빼면 올바른 귀무분포가 복원된다.
    수익률 0 같은 동점이 흔한 이 데이터에서는 무시할 수 없다.
    """
    xa = np.asarray(x, dtype=float)
    ya = np.asarray(y, dtype=float)
    xa = xa[np.isfinite(xa)]
    ya = ya[np.isfinite(ya)]
    n1, n2 = int(xa.size), int(ya.size)
    if n1 == 0 or n2 == 0:
        return MannWhitneyResult(n1=n1, n2=n2, u=None, p=None, rank_biserial=None)

    combined = np.concatenate([xa, ya])
    ranks = _rankdata(combined)
    r1 = float(ranks[:n1].sum())
    u1 = r1 - n1 * (n1 + 1) / 2.0
    rank_biserial = 2.0 * u1 / (n1 * n2) - 1.0

    n = n1 + n2
    # 동점 보정항 sum(t^3 - t)
    _, counts = np.unique(combined, return_counts=True)
    tie = float(np.sum(counts.astype(float) ** 3 - counts.astype(float)))
    var = n1 * n2 / 12.0 * ((n + 1) - tie / (n * (n - 1))) if n > 1 else 0.0
    if var <= 0:
        # 전부 동점 — 분포 차이를 말할 수 없다.
        return MannWhitneyResult(n1=n1, n2=n2, u=u1, p=None,
                                 rank_biserial=rank_biserial)
    mu = n1 * n2 / 2.0
    # 연속성 보정(0.5) — 이산 U 를 연속 정규로 근사할 때의 표준 보정.
    z = (abs(u1 - mu) - 0.5) / math.sqrt(var)
    z = max(z, 0.0)
    return MannWhitneyResult(n1=n1, n2=n2, u=u1, p=float(normal_sf_two_sided(z)),
                             rank_biserial=rank_biserial)


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
