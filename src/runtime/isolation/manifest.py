"""SessionManifest — namespace 를 소유한 세션(run)의 정본 기록.

startup 게이트가 namespace 재사용/RESUME 정당성을 판정할 때 읽는 SSoT.
원자적 기록(tmp + os.replace, PaperRuntime.status.json 과 동일 관례)으로 부분쓰기 방지.

PR 1-A 범위: identity + lifecycle_state=CREATED + 타임스탬프.
lifecycle finalize(RUNNING/TERMINATED)·completion·coverage 는 PR 1-B 에서 확장한다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace

from src.runtime.isolation.errors import ManifestReadError
from src.runtime.isolation.identity import SessionIdentity

LIFECYCLE_CREATED = "CREATED"       # PR 1-B: RUNNING | TERMINATED 로 확장


@dataclass
class SessionManifest:
    identity: SessionIdentity
    lifecycle_state: str = LIFECYCLE_CREATED
    created_at: str | None = None
    updated_at: str | None = None

    @classmethod
    def create(cls, identity: SessionIdentity, now: str | None) -> "SessionManifest":
        return cls(identity=identity, lifecycle_state=LIFECYCLE_CREATED,
                   created_at=now, updated_at=now)

    def touch(self, now: str | None) -> "SessionManifest":
        """RESUME 등에서 updated_at 만 갱신한 사본 반환."""
        return replace(self, updated_at=now)

    def to_dict(self) -> dict:
        return {
            "identity": self.identity.to_dict(),
            "lifecycle_state": self.lifecycle_state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionManifest":
        return cls(
            identity=SessionIdentity.from_dict(d["identity"]),
            lifecycle_state=d.get("lifecycle_state", LIFECYCLE_CREATED),
            created_at=d.get("created_at"),
            updated_at=d.get("updated_at"),
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
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest.to_dict(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
