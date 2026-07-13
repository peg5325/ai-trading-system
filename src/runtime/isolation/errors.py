"""Session Isolation 예외 계층.

순환 import 방지를 위해 예외만 별도 모듈에 둔다(lock/guard 양쪽이 참조).
모든 startup 게이트 실패는 StartupGuardError 하위로 잡을 수 있다.
"""
from __future__ import annotations


class StartupGuardError(RuntimeError):
    """세션 시작 가드 실패의 공통 베이스."""


class NamespaceCollisionError(StartupGuardError):
    """FRESH/RESTART 인데 기존 manifest/DB 가 있는 namespace 재사용 시도."""


class IdentityMismatchError(StartupGuardError):
    """RESUME 인데 identity/provenance 필드가 기존 manifest 와 불일치."""


class MissingManifestError(StartupGuardError):
    """RESUME 인데 이어받을 manifest 가 없음."""


class ManifestReadError(StartupGuardError):
    """기존 manifest 가 손상/판독 불가 — namespace 를 신뢰할 수 없어 시작 차단."""


class LockHeldError(StartupGuardError):
    """살아있는 프로세스가 single-writer lock 보유 → 두 번째 실행 거부."""


class StaleLockError(StartupGuardError):
    """죽은 프로세스의 lock(stale) — 검증된 resume/restart 외에는 회수 금지."""
