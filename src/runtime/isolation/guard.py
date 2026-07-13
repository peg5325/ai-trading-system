"""startup_guard — stateful store 를 열기 전에 실행하는 세션 시작 게이트.

이 함수는 어떤 store 도 생성/기록하지 않는다. 반드시 CandleStore/Journal/Metrics 생성
'이전에' 호출해야 한다(스토어는 open 만으로 파일을 만든다 → 실패 시 신규 쓰기 0 보장).

판정:
- FRESH/RESTART: 기존 manifest 또는 DB 파일이 있으면 NamespaceCollisionError(재사용 금지).
- RESUME: manifest 가 없으면 MissingManifestError; identity/provenance 불일치면
  IdentityMismatchError. 일치할 때만 진행.
- 어느 경우든 single-writer lock 획득(활성 lock → LockHeldError, stale → 회수).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from src.runtime.isolation.errors import (
    IdentityMismatchError,
    MissingManifestError,
    NamespaceCollisionError,
    StartupGuardError,
)
from src.runtime.isolation.identity import (
    START_FRESH,
    START_RESTART,
    START_RESUME,
    SessionIdentity,
)
from src.runtime.isolation.lock import SessionLock, default_is_alive
from src.runtime.isolation.manifest import (
    SessionManifest,
    read_manifest,
    write_manifest,
)


@dataclass
class GuardResult:
    manifest: SessionManifest
    lock: SessionLock


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_session_paths(cfg) -> tuple[str, str, list[str]]:
    """RuntimeConfig → (manifest_path, lock_path, db_paths).

    manifest/lock 은 세션별 journal_db 옆에 둔다(namespace 별 고유). 명시 override 우선.
    """
    manifest = cfg.session_manifest_path or (cfg.journal_db + ".manifest.json")
    lock = cfg.session_lock_path or (cfg.journal_db + ".lock")
    db_paths = [cfg.journal_db, cfg.candle_db, cfg.metrics_db]
    return manifest, lock, db_paths


def startup_guard(identity: SessionIdentity, *, manifest_path: str, lock_path: str,
                  db_paths, is_process_alive=default_is_alive,
                  now_iso=_utc_now_iso) -> GuardResult:
    mode = identity.start_mode
    existing = read_manifest(manifest_path)

    if mode in (START_FRESH, START_RESTART):
        if existing is not None:
            raise NamespaceCollisionError(
                f"namespace already initialized (manifest={manifest_path}); "
                f"start_mode={mode} requires a fresh namespace")
        occupied = [p for p in db_paths if p and os.path.exists(p)]
        if occupied:
            raise NamespaceCollisionError(
                f"existing DB files in namespace: {occupied}; "
                f"start_mode={mode} requires a fresh namespace")
        # FRESH 는 stale lock 을 회수하지 않는다(계약: 재사용 금지 → StaleLockError 로 노출).
        # RESTART 는 검증된 재시작이므로 orphan lock 회수 허용.
        lock = SessionLock.acquire(lock_path, identity, is_process_alive=is_process_alive,
                                   allow_stale_takeover=(mode == START_RESTART),
                                   now_iso=now_iso)
        manifest = SessionManifest.create(identity, now_iso())
        write_manifest(manifest_path, manifest)
        return GuardResult(manifest, lock)

    if mode == START_RESUME:
        if existing is None:
            raise MissingManifestError(
                f"no manifest at {manifest_path}; nothing to RESUME")
        mism = existing.identity.mismatched_fields(identity)
        if mism:
            raise IdentityMismatchError(
                f"RESUME identity mismatch on fields {mism}")
        lock = SessionLock.acquire(lock_path, identity, is_process_alive=is_process_alive,
                                   allow_stale_takeover=True, now_iso=now_iso)
        manifest = existing.touch(now_iso())
        write_manifest(manifest_path, manifest)
        return GuardResult(manifest, lock)

    raise StartupGuardError(f"unknown start_mode: {mode!r}")
