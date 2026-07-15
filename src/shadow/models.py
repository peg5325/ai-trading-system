"""Online Shadow Decision 모델 (v2.1 PR 4-A, 계약서 §2-4).

- 순수 데이터 컨테이너 + config. 매매/판단 로직에 되먹임되지 않는다(관측 전용).
- Shadow 는 벤치마크 국면(2-B BenchmarkContext)으로 '가상 진입 정책'을 평가할 뿐,
  실제 주문/권한 순서(AI→Risk→Order→Broker)에 아무 영향이 없다.
- 런타임 레코드에는 hypothetical decision 만 담는다. 미래 outcome(forward return 등)은
  세션 후 오프라인 batch(PR 4-B)에서만 부착한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 스키마/정책 버전(구성 변경 시 bump).
SHADOW_VERSION = "1"          # shadow 레코드 스키마 버전
SHADOW_POLICY_VERSION = "1"   # 정책 로직 버전(A/B 계산 규약)

# 정책 식별자.
POLICY_A = "A"   # benchmark close > EMA20
POLICY_B = "B"   # A ∧ EMA20 slope > 0

# reason 코드(would_allow 근거).
R_CLOSE_ABOVE_EMA = "CLOSE_ABOVE_EMA"
R_CLOSE_BELOW_EMA = "CLOSE_BELOW_EMA"
R_SLOPE_UP = "EMA_SLOPE_UP"
R_SLOPE_DOWN_FLAT = "EMA_SLOPE_DOWN_OR_FLAT"
R_NO_CONTEXT = "NO_CONTEXT"     # 국면 스냅샷 없음(관측 미가용)
R_EMA_WARMUP = "EMA_WARMUP"     # close/ema warm-up·결측(정책 판단 불가)
R_SLOPE_WARMUP = "SLOPE_WARMUP"  # ema slope warm-up·결측(Policy B 만 — 원인 구분)


@dataclass(frozen=True)
class ShadowConfig:
    """shadow 블록. 기본 비활성(opt-in) — 켜져도 매매를 바꾸지 않는다(관측 전용).

    shadow 는 market_context 관측(2-B)의 BenchmarkContext 를 소비한다. 의미있는 shadow
    결정을 남기려면 market_context.enabled=True 가 함께 필요하다(둘 다 opt-in; Phase 6
    baseline 은 'Market Context 와 Shadow 만 ON'). context 결측 시 would_allow=None 기록.
    """
    enabled: bool = False


@dataclass(frozen=True)
class ShadowDecision:
    """단일 정책의 가상 결정. would_allow=None 은 '판단 불가(결측/warm-up)' — 차단이 아니다."""
    policy_id: str
    policy_version: str
    would_allow: bool | None
    reason_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "would_allow": self.would_allow,
            "reason_codes": list(self.reason_codes),
        }
