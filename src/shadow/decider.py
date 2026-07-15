"""Shadow decider — 벤치마크 국면 스냅샷으로 가상 진입 정책 A/B 를 평가하는 순수 함수(PR 4-A).

원칙:
- 순수·결정론적: 같은 입력 → 같은 출력(now()/난수 미사용). 어떤 결정에도 되먹임 없음.
- 국면 스냅샷은 2-B `build_benchmark_context` 산출(완료봉만·look-ahead 안전) 재사용 →
  Paper=Backtest 국면 패리티.
- 관측 전용: context 결측/warm-up → would_allow=None(매매를 막지 않음).
- **런타임 레코드에 미래 outcome(forward return/avoided loss 등) 미기록** — 4-B 오프라인 전용.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.shadow.models import (
    POLICY_A,
    POLICY_B,
    R_CLOSE_ABOVE_EMA,
    R_CLOSE_BELOW_EMA,
    R_EMA_WARMUP,
    R_NO_CONTEXT,
    R_SLOPE_DOWN_FLAT,
    R_SLOPE_UP,
    R_SLOPE_WARMUP,
    SHADOW_POLICY_VERSION,
    SHADOW_VERSION,
    ShadowDecision,
)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decision(policy_id: str, would_allow: bool | None, reasons: list[str]) -> ShadowDecision:
    return ShadowDecision(policy_id=policy_id, policy_version=SHADOW_POLICY_VERSION,
                          would_allow=would_allow, reason_codes=reasons)


def evaluate_shadow_policies(context: dict | None) -> list[ShadowDecision]:
    """국면 스냅샷(dict) → [Policy A, Policy B] 가상 결정. context None → 둘 다 NO_CONTEXT(None)."""
    if context is None:
        return [_decision(POLICY_A, None, [R_NO_CONTEXT]),
                _decision(POLICY_B, None, [R_NO_CONTEXT])]

    close = context.get("benchmark_close")
    ema = context.get("benchmark_ema20")
    slope = context.get("benchmark_ema20_slope")

    # Policy A: close > EMA20
    if close is None or ema is None:
        a = _decision(POLICY_A, None, [R_EMA_WARMUP])
    elif close > ema:
        a = _decision(POLICY_A, True, [R_CLOSE_ABOVE_EMA])
    else:
        a = _decision(POLICY_A, False, [R_CLOSE_BELOW_EMA])

    # Policy B: A ∧ slope > 0. warm-up 원인 구분(ema vs slope)으로 오프라인 진단 정밀도 확보.
    if close is None or ema is None:
        b = _decision(POLICY_B, None, [R_EMA_WARMUP])
    elif slope is None:
        b = _decision(POLICY_B, None, [R_SLOPE_WARMUP])
    else:
        above = close > ema
        up = slope > 0
        reasons = [R_CLOSE_ABOVE_EMA if above else R_CLOSE_BELOW_EMA,
                   R_SLOPE_UP if up else R_SLOPE_DOWN_FLAT]
        b = _decision(POLICY_B, above and up, reasons)

    return [a, b]


def build_shadow_record(context: dict | None, *, decision_time: datetime,
                        candidate_ids: list[str]) -> dict:
    """런타임 shadow 레코드(hypothetical decision 만). 미래 outcome 필드 없음(4-B 부착).

    ★ 조인 규약(계약 §2-4 candidate_id → 구현 candidate_ids 리스트): 정책 결정은 벤치마크
      국면 단위(사이클당 A/B 1쌍)라 사이클 내 모든 후보에 동일하게 적용된다. 따라서 레코드는
      사이클 단위로 policies 1쌍 + candidate_ids 리스트를 담고, **4-B 는 리스트를 per-candidate
      로 fan-out 해 각 candidate_id 에 동일 policy 결정을 부여**한다(§2-4 단수 candidate_id 를
      리스트로 일반화 — 국면-단위 결정을 후보마다 복제하지 않아 컨텍스트 중복 회피).
    candidate_ids: 이 사이클의 진입 후보 candidate_id 목록(오프라인 조인 키). 결측/빈 값은 제외.
    market_context_snapshot: 2-B 국면 스냅샷 dict(이미 look-ahead 안전) 또는 None. shadow 레코드를
      자기완결로 만들기 위해 의도적으로 포함(§2-4 필드) — 저널 benchmark_context 와 동일 dict 를
      가리키나 read-only·독립 JSON 직렬화라 aliasing 위험 없음.
    """
    return {
        "shadow_version": SHADOW_VERSION,
        "policy_version": SHADOW_POLICY_VERSION,
        "decision_time": _iso_z(decision_time),
        "market_context_snapshot": context,
        "policies": [d.to_dict() for d in evaluate_shadow_policies(context)],
        "candidate_ids": list(candidate_ids),
    }
