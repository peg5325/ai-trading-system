"""Phase 6 승격조건 판정 로직 — 순수(I/O 없음) 판정기 (PR #38).

plan §Phase 6 의 승격 최소조건을 세션 신호(SessionSignal)로부터 판정한다. 이 모듈은
**매매 로직·런타임에 일절 관여하지 않는** read-only 분석 산출물이며(behavior-change=false),
journal/candle/manifest 파싱은 CLI(scripts/phase6_promotion_report.py)가 담당한다. 여기서는
이미 조립된 per-session 신호만 소비하므로 실 데이터 없이도 계약이 단위테스트로 강제된다
(계약서 §"불변식을 산문이 아니라 실행 가능한 테스트로 박을 것").

승격 신호의 출처(중요):
- 각 SessionSignal 의 blocked_avoided_loss/missed_profit 은 CLI 가
  `aggregate_by_policy(dataset, horizon, entry_only=True)`(4-B)로 구한 **실제 진입(ORDERED)
  후보 + gate 차단(would_allow=False)** 집계다. 관측 유니버스 전체가 아니라 '국면 gate 가
  실제 진입을 막았을 때의 가치'만 센다(4-B docstring 의 분모 주의).
- price-direction shadow 이므로 실 체결 수량/비용/포트폴리오 MDD 가 아니다 → 조건 #5(비용
  순효과·MDD)는 이 배치가 판정 불가하여 항상 DEFER(조용한 PASS 금지).

판정 단위: (market, policy, horizon) 셀 하나. CLI 가 셀마다 evaluate_promotion 을 호출한다.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# ── 판정 상태 ──
PASS = "PASS"
FAIL = "FAIL"
DEFER = "DEFER"     # 이 배치가 판정 불가(별도 근거 필요) — PASS 로 세지 않는다

# ── 게이트 종합 상태 ──
GATE_MET = "MET"           # 모든 조건 PASS (FAIL/DEFER 없음)
GATE_NOT_MET = "NOT_MET"   # FAIL 이 하나라도
GATE_PENDING = "PENDING"   # FAIL 없음 + DEFER 잔여(자동조건 충족·수동판정 대기)

# ── 승격 임계값(plan §Phase 6) ──
MIN_FULL_SESSIONS_PER_MARKET = 5   # 시장별 FULL session 최소
MIN_DISTINCT_REGIMES = 2           # 서로 다른 regime 최소
DOMINANCE_MAX_SHARE = 0.5          # 한 세션의 |net| 기여 상한(초과 시 지배)
REPEAT_MIN_RATIO = 0.6             # net>0 세션 비율 하한('반복적으로 큼')

_EPS = 1e-12


def _sign(x: float) -> int:
    if x > _EPS:
        return 1
    if x < -_EPS:
        return -1
    return 0


@dataclass(frozen=True)
class SessionSignal:
    """한 세션의 (특정 policy·horizon) 판정 입력. 순수 로직은 이 컨테이너만 소비한다.

    blocked_avoided_loss/missed_profit 은 entry_only=True 집계(위 모듈 docstring 참조).
    started_at 은 chronological 정렬 키(없으면 session_id 로 대체).
    """
    session_id: str
    market: str
    completion_status: str          # FULL | PARTIAL | ABORTED | FAILED
    manual_flatten_count: int
    regime: str | None
    started_at: str | None
    blocked_avoided_loss: float
    blocked_missed_profit: float
    n_blocked: int

    @property
    def is_primary(self) -> bool:
        """primary 판정 대상 = 완전 세션(FULL) ∧ 수동청산 없음. 그 외는 제외(§Phase 6·§5)."""
        return self.completion_status == "FULL" and self.manual_flatten_count == 0

    @property
    def net(self) -> float:
        """세션 shadow net = avoided_loss − missed_profit(>0 이면 gate 차단이 유리했음)."""
        return self.blocked_avoided_loss - self.blocked_missed_profit

    def _order_key(self) -> str:
        return self.started_at or self.session_id


@dataclass(frozen=True)
class CriterionResult:
    key: str
    title: str
    status: str           # PASS | FAIL | DEFER
    detail: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "status": self.status,
                "detail": self.detail, "evidence": self.evidence}


@dataclass(frozen=True)
class PromotionReport:
    market: str | None
    policy_id: str
    horizon_min: int
    criteria: list[CriterionResult]

    @property
    def gate_status(self) -> str:
        if any(c.status == FAIL for c in self.criteria):
            return GATE_NOT_MET
        if any(c.status == DEFER for c in self.criteria):
            return GATE_PENDING
        return GATE_MET

    @property
    def gate_passed(self) -> bool:
        return self.gate_status == GATE_MET

    def to_dict(self) -> dict:
        return {"market": self.market, "policy_id": self.policy_id,
                "horizon_min": self.horizon_min, "gate_status": self.gate_status,
                "criteria": [c.to_dict() for c in self.criteria]}


def classify_session_regime(trends) -> str | None:
    """세션의 국면 라벨 = benchmark_trend 최빈값(None 제외). 동수면 사전순 앞선 값(결정론).

    trends 는 세션 내 각 사이클의 market_context_snapshot.benchmark_trend 시퀀스.
    전부 None/빈 입력이면 None(국면 미상 → regime 다양성에 미포함)."""
    labels = [t for t in trends if t]
    if not labels:
        return None
    counts = Counter(labels)
    top = max(counts.values())
    return sorted(k for k, v in counts.items() if v == top)[0]


def evaluate_promotion(signals: list[SessionSignal], *, policy_id: str,
                       horizon_min: int, market: str | None = None) -> PromotionReport:
    """한 (market, policy, horizon) 셀의 7개 승격조건 판정 → PromotionReport.

    market 이 주어지면 해당 시장 세션만 본다(시장별 FULL≥5 는 시장 격리 판정). 순서·값이
    같으면 리포트가 동일하다(결정론)."""
    scoped = [s for s in signals if market is None or s.market == market]
    primary = [s for s in scoped if s.is_primary]
    excluded = [s for s in scoped if not s.is_primary]

    criteria = [
        _c_full_sessions(primary),
        _c_regime_diversity(primary),
        _c_partial_excluded(excluded),
        _c_direction_stability(primary),
        _c_cost_mdd(),
        _c_avoided_gt_missed(primary),
        _c_no_single_dominance(primary),
    ]
    return PromotionReport(market=market, policy_id=policy_id,
                           horizon_min=horizon_min, criteria=criteria)


# ── 개별 조건 ──

def _c_full_sessions(primary: list[SessionSignal]) -> CriterionResult:
    n = len(primary)
    ok = n >= MIN_FULL_SESSIONS_PER_MARKET
    return CriterionResult(
        key="full_sessions", title="시장별 FULL session ≥ 5",
        status=PASS if ok else FAIL,
        detail=f"FULL(primary) 세션 {n}/{MIN_FULL_SESSIONS_PER_MARKET}",
        evidence={"n_full": n, "required": MIN_FULL_SESSIONS_PER_MARKET,
                  "session_ids": [s.session_id for s in primary]})


def _c_regime_diversity(primary: list[SessionSignal]) -> CriterionResult:
    regimes = sorted({s.regime for s in primary if s.regime})
    n = len(regimes)
    ok = n >= MIN_DISTINCT_REGIMES
    return CriterionResult(
        key="regime_diversity", title="서로 다른 regime ≥ 2",
        status=PASS if ok else FAIL,
        detail=f"distinct regime {n}/{MIN_DISTINCT_REGIMES}: {regimes or '없음'}",
        evidence={"n_distinct": n, "required": MIN_DISTINCT_REGIMES, "regimes": regimes})


def _c_partial_excluded(excluded: list[SessionSignal]) -> CriterionResult:
    """감사(audit) 조건: PARTIAL/MANUAL_FLATTEN/ABORTED/FAILED 세션이 primary 에서 제외됨을 명시.

    방법론적 불변식이라 항상 PASS 이되, 무엇이 왜 빠졌는지를 evidence 로 노출한다(§5 체크리스트
    'PARTIAL/MANUAL_FLATTEN 세션을 분석 리포트에서 필터링할 수 있다')."""
    items = [{"session_id": s.session_id, "completion_status": s.completion_status,
              "manual_flatten_count": s.manual_flatten_count,
              "reason": "MANUAL_FLATTEN" if s.completion_status == "FULL"
                        else s.completion_status}
             for s in excluded]
    return CriterionResult(
        key="partial_excluded", title="PARTIAL/MANUAL_FLATTEN primary 제외",
        status=PASS,
        detail=f"primary 에서 제외된 세션 {len(items)}개",
        evidence={"n_excluded": len(items), "excluded": items})


def _c_direction_stability(primary: list[SessionSignal]) -> CriterionResult:
    """LOSO(세션 leave-one-out)·chronological(전/후반) 에서 net 부호가 유지되는가.

    세션 단위 방향 안정성 검사(3-B evaluation 의 leave_one_session_out/chronological_split 을
    후보-캔들이 아닌 '세션 net' 수준으로 옮긴 것). 실 체결 OOS 가 아니라 shadow 신호의
    방향 견고성만 본다(계약 §3-7: 최종 OOS 는 chronological/embargo 우선)."""
    ordered = sorted(primary, key=lambda s: s._order_key())
    nets = [s.net for s in ordered]
    n = len(nets)
    if n < 2:
        return CriterionResult(
            key="direction_stability", title="LOSO·chronological 방향 유지",
            status=FAIL, detail=f"세션 {n}개 — 방향 안정성 판정에 부족(≥2 필요)",
            evidence={"n_sessions": n, "loso_stable": False,
                      "chronological_stable": False})

    agg = sum(nets)
    agg_sign = _sign(agg)
    loso_stable = agg_sign != 0 and all(_sign(agg - x) == agg_sign for x in nets)

    mid = n // 2
    early, late = nets[:mid], nets[mid:]
    chrono_stable = (agg_sign != 0 and bool(early) and bool(late)
                     and _sign(sum(early)) == agg_sign
                     and _sign(sum(late)) == agg_sign)

    ok = loso_stable and chrono_stable
    return CriterionResult(
        key="direction_stability", title="LOSO·chronological 방향 유지",
        status=PASS if ok else FAIL,
        detail=f"net 부호={agg_sign:+d} · LOSO={'유지' if loso_stable else '뒤집힘'} · "
               f"chronological={'유지' if chrono_stable else '뒤집힘'}",
        evidence={"n_sessions": n, "aggregate_net": agg, "aggregate_sign": agg_sign,
                  "loso_stable": loso_stable, "chronological_stable": chrono_stable})


def _c_cost_mdd() -> CriterionResult:
    """조건 #5 — 항상 DEFER. shadow 는 price-direction 프록시라 실 체결/비용/포트폴리오 MDD 가
    없으므로 이 배치가 판정할 수 없다. 조용한 PASS 대신 명시적 DEFER(no-fake-completion)."""
    return CriterionResult(
        key="cost_mdd", title="비용 반영 순효과 양수 · MDD 비악화",
        status=DEFER,
        detail="DEFER: price-direction shadow 로 판정 불가 — 실 체결 backtest + 비용모델 필요(별도 후속)",
        evidence={"reason": "shadow=price-direction proxy; no fills/cost/portfolio MDD"})


def _c_avoided_gt_missed(primary: list[SessionSignal]) -> CriterionResult:
    """avoided loss 가 missed profit 보다 '반복적으로' 큰가 = 총 net>0 ∧ net>0 세션 비율≥0.6.

    ★ 분모 = 전체 primary(해당 policy 가 차단 신호를 안 낸 중립 세션 net==0 포함). 이는 의도된
    **보수적** 정의다 — 중립 세션이 비율을 희석해 승격을 더 어렵게 만든다(승격 게이트는
    false-positive 를 피하는 쪽으로 편향돼야 자본 안전상 옳다). 투명성을 위해 실제 차단 신호를
    낸 세션 수(n_acting)를 evidence 에 함께 노출한다."""
    n = len(primary)
    agg_net = sum(s.net for s in primary)
    n_pos = sum(1 for s in primary if s.net > _EPS)
    n_acting = sum(1 for s in primary if s.n_blocked > 0)   # 이 policy 가 실제 차단한 세션
    ratio = (n_pos / n) if n else 0.0
    ok = n > 0 and agg_net > _EPS and ratio >= REPEAT_MIN_RATIO
    return CriterionResult(
        key="avoided_gt_missed", title="avoided loss > missed profit (반복)",
        status=PASS if ok else FAIL,
        detail=f"총 net={agg_net:+.4f} · net>0 세션 {n_pos}/{n}(비율 {ratio:.0%}, "
               f"임계 {REPEAT_MIN_RATIO:.0%}) · 차단신호 세션 {n_acting}",
        evidence={"aggregate_net": agg_net, "n_net_positive": n_pos, "n_primary": n,
                  "n_acting": n_acting, "n_neutral": n - n_acting,
                  "positive_ratio": ratio, "required_ratio": REPEAT_MIN_RATIO})


def _c_no_single_dominance(primary: list[SessionSignal]) -> CriterionResult:
    """한 세션이 결과(net 크기)를 지배하지 않는가 = max(|net_i|)/Σ|net_j| ≤ 0.5."""
    mags = [(s.session_id, abs(s.net)) for s in primary]
    total = sum(m for _, m in mags)
    if total <= _EPS:
        return CriterionResult(
            key="no_single_dominance", title="한 세션이 결과 지배 안 함",
            status=PASS, detail="집계 신호 0 — 지배 없음(신호 부재)",
            evidence={"max_share": 0.0, "dominant_session": None})
    dom_id, dom_mag = max(mags, key=lambda t: t[1])
    max_share = dom_mag / total
    ok = max_share <= DOMINANCE_MAX_SHARE
    return CriterionResult(
        key="no_single_dominance", title="한 세션이 결과 지배 안 함",
        status=PASS if ok else FAIL,
        detail=f"최대 기여 세션 {dom_id} share={max_share:.0%}(상한 {DOMINANCE_MAX_SHARE:.0%})",
        evidence={"max_share": max_share, "dominant_session": dom_id,
                  "threshold": DOMINANCE_MAX_SHARE})
