"""세션 lifecycle/completion 상태 정의 + 종료 분류.

`start_mode`(FRESH/RESUME/RESTART, identity)와 `completion_status`(아래)는 직교한다.
#006R = RESTART(시작방식) ∧ PARTIAL(완료상태) 처럼 동시에 표현할 수 있어야 한다.
"""
from __future__ import annotations

# lifecycle_state (manifest)
LIFECYCLE_CREATED = "CREATED"
LIFECYCLE_RUNNING = "RUNNING"
LIFECYCLE_TERMINATED = "TERMINATED"

# completion_status (종료 시 확정)
COMPLETION_FULL = "FULL"          # 세션말까지 도달 + 커버리지 완결
COMPLETION_PARTIAL = "PARTIAL"    # 조기 종료 또는 결측 존재
COMPLETION_ABORTED = "ABORTED"    # 외부 중단(Ctrl+C/stop)로 세션말 이전 종료
COMPLETION_FAILED = "FAILED"      # 오류로 정상 사이클 없이 종료

# termination_reason (자유 문자열; 자동 런타임 기본값)
REASON_SESSION_END = "SESSION_END"
REASON_STOPPED = "STOPPED"
REASON_INTERRUPTED = "INTERRUPTED"
REASON_ERROR = "ERROR"
REASON_ENDED = "ENDED"


def classify_completion(*, reached_session_end: bool, coverage_complete: bool,
                        aborted: bool = False, failed: bool = False) -> str:
    """종료 신호 → completion_status. 우선순위: FAILED > ABORTED > (FULL|PARTIAL)."""
    if failed:
        return COMPLETION_FAILED
    if aborted:
        return COMPLETION_ABORTED
    if reached_session_end and coverage_complete:
        return COMPLETION_FULL
    return COMPLETION_PARTIAL
