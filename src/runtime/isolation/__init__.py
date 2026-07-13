"""Session Isolation (v2.1 Phase 1 · PR 1-A).

세션(run) 정체성 + 시작 게이트. 매매 로직과 완전히 분리된 startup 관심사.
새 세션이 과거 DB namespace 를 실수로 재사용하는 것을 시작 전에 fail-fast 로 막고,
single-writer lock 으로 동시 실행을 거부하며, manifest 로 재현 근거를 고정한다.
"""
from __future__ import annotations

from src.runtime.isolation.errors import (
    IdentityMismatchError,
    LockHeldError,
    ManifestReadError,
    MissingManifestError,
    NamespaceCollisionError,
    StaleLockError,
    StartupGuardError,
)
from src.runtime.isolation.guard import (
    GuardResult,
    resolve_session_paths,
    startup_guard,
)
from src.runtime.isolation.identity import (
    BROKER_PAPER,
    ID_VERSION,
    SESSION_SCHEMA_VERSION,
    START_FRESH,
    START_RESTART,
    START_RESUME,
    SessionIdentity,
    canonical_config_hash,
    capture_identity,
    infer_market_currency,
    producer_commit,
)
from src.runtime.isolation.lock import (
    LockInfo,
    SessionLock,
    default_is_alive,
    read_lock,
)
from src.runtime.isolation.manifest import (
    SessionManifest,
    read_manifest,
    write_manifest,
)

__all__ = [
    # identity
    "SessionIdentity", "capture_identity", "canonical_config_hash",
    "producer_commit", "infer_market_currency",
    "SESSION_SCHEMA_VERSION", "ID_VERSION", "BROKER_PAPER",
    "START_FRESH", "START_RESUME", "START_RESTART",
    # manifest
    "SessionManifest", "read_manifest", "write_manifest",
    # lock
    "SessionLock", "LockInfo", "read_lock", "default_is_alive",
    # guard
    "startup_guard", "GuardResult", "resolve_session_paths",
    # errors
    "StartupGuardError", "NamespaceCollisionError", "IdentityMismatchError",
    "MissingManifestError", "ManifestReadError", "LockHeldError", "StaleLockError",
]
