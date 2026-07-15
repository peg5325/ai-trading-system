"""Shadow Framework (v2.1 Phase 4) — 가상 진입 정책 관측.

Online Shadow Decision(PR 4-A): 벤치마크 국면으로 정책 A/B 를 평가해 hypothetical decision 만
기록(실주문·권한 순서 무영향). Offline Outcome(PR 4-B): 세션 후 read-only batch 에서 forward
return 을 부착. 어떤 결정에도 되먹임되지 않는 관측 전용 계층.
"""
from src.shadow.decider import build_shadow_record, evaluate_shadow_policies
from src.shadow.models import (
    SHADOW_POLICY_VERSION,
    SHADOW_VERSION,
    ShadowConfig,
    ShadowDecision,
)

__all__ = [
    "ShadowConfig", "ShadowDecision", "evaluate_shadow_policies", "build_shadow_record",
    "SHADOW_VERSION", "SHADOW_POLICY_VERSION",
]
