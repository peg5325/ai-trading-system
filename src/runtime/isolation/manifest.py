"""SessionManifest — namespace 를 소유한 세션(run)의 정본 기록.

startup 게이트가 namespace 재사용/RESUME 정당성을 판정할 때 읽는 SSoT.
원자적 기록(tmp + os.replace, PaperRuntime.status.json 과 동일 관례)으로 부분쓰기 방지.

- PR 1-A: identity + lifecycle_state=CREATED + 타임스탬프.
- PR 1-B: lifecycle RUNNING/TERMINATED · completion_status · termination_reason ·
  manual_flatten_count · coverage(bar 커버리지). start_mode ⟂ completion_status.
비정상 종료 시 manifest 는 RUNNING 으로 남아 stale lock 과 함께 crash-resume 신호가 된다.
"""
from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, replace

from src.runtime.isolation.errors import ManifestReadError
from src.runtime.isolation.identity import SessionIdentity
from src.runtime.isolation.lifecycle import (
    LIFECYCLE_CREATED,
    LIFECYCLE_RUNNING,
    LIFECYCLE_TERMINATED,
)


@dataclass
class SessionManifest:
    identity: SessionIdentity
    lifecycle_state: str = LIFECYCLE_CREATED
    created_at: str | None = None
    updated_at: str | None = None
    # PR 1-B: 종료 상태 + 커버리지 (TERMINATED 시 확정)
    completion_status: str | None = None        # FULL | PARTIAL | ABORTED | FAILED
    termination_reason: str | None = None
    manual_flatten_count: int = 0
    coverage: dict | None = None                # SessionCoverage.to_dict()
    terminated_at: str | None = None

    @classmethod
    def create(cls, identity: SessionIdentity, now: str | None) -> "SessionManifest":
        return cls(identity=identity, lifecycle_state=LIFECYCLE_CREATED,
                   created_at=now, updated_at=now)

    def touch(self, now: str | None) -> "SessionManifest":
        """RESUME 등에서 updated_at 만 갱신한 사본 반환."""
        return replace(self, updated_at=now)

    def mark_running(self, now: str | None) -> "SessionManifest":
        """세션 실행 시작 → RUNNING. 비정상 종료 시 이 상태로 남아 crash-resume 신호가 된다."""
        return replace(self, lifecycle_state=LIFECYCLE_RUNNING, updated_at=now)

    def finalize(self, now: str | None, *, completion_status: str,
                 termination_reason: str | None = None, coverage: dict | None = None,
                 manual_flatten_count: int = 0) -> "SessionManifest":
        """정상 종료 → TERMINATED + completion/coverage 확정."""
        return replace(self, lifecycle_state=LIFECYCLE_TERMINATED,
                       completion_status=completion_status,
                       termination_reason=termination_reason,
                       coverage=coverage, manual_flatten_count=manual_flatten_count,
                       updated_at=now, terminated_at=now)

    def to_dict(self) -> dict:
        return {
            "identity": self.identity.to_dict(),
            "lifecycle_state": self.lifecycle_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completion_status": self.completion_status,
            "termination_reason": self.termination_reason,
            "manual_flatten_count": self.manual_flatten_count,
            "coverage": self.coverage,
            "terminated_at": self.terminated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionManifest":
        return cls(
            identity=SessionIdentity.from_dict(d["identity"]),
            lifecycle_state=d.get("lifecycle_state", LIFECYCLE_CREATED),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
            completion_status=d.get("completion_status"),
            termination_reason=d.get("termination_reason"),
            manual_flatten_count=d.get("manual_flatten_count", 0),
            coverage=d.get("coverage"),
            terminated_at=d.get("terminated_at"),
        )


def read_manifest(path: str) -> SessionManifest | None:
    """manifest 를 읽는다. 없으면 None. 손상/판독불가면 ManifestReadError(시작 차단).

    손상 manifest 를 '없음'으로 취급하면 점유된 namespace 를 덮어쓸 수 있으므로
    None 대신 예외로 노출한다(read_lock 과 달리 fail-safe 하게 차단)."""
    path = str(path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return SessionManifest.from_dict(json.load(f))
    except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
        raise ManifestReadError(f"corrupt/unreadable manifest at {path}: {e}") from e


def write_manifest(path: str, manifest: SessionManifest) -> None:
    """원자적 기록: tmp 파일에 쓰고 os.replace 로 교체(부분쓰기 방지)."""
    path = str(path)
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):    # 쓰기 실패 시 .tmp 잔류 방지
            os.remove(tmp)
        raise
