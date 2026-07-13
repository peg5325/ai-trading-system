"""SessionLock — namespace 당 single-writer 파일 lock.

같은 namespace 를 두 프로세스가 동시에 쓰는 것을 막는다(무결성 훼손 방지).
- 살아있는 프로세스가 보유한 lock → 두 번째 실행 거부(LockHeldError).
- 죽은 프로세스의 lock(stale) → 검증된 resume/restart 에서만 회수(allow_stale_takeover).
  FRESH 는 stale lock 을 회수하지 않고 StaleLockError 로 노출한다(계약: 재사용 금지).

획득은 O_CREAT|O_EXCL 로 원자적으로 시도한다 → 빈 namespace 를 향한 동시 실행 경합에서
정확히 한 프로세스만 lock 을 만든다(check-then-write TOCTOU 창 제거).

생사 판정은 주입 가능(is_process_alive)하여 결정론적으로 테스트한다. 기본 판정은
같은 호스트에서 os.kill(pid, 0). 다른 호스트의 lock 은 확인 불가 → 보수적으로 '살아있음'.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
from dataclasses import asdict, dataclass

from src.runtime.isolation.errors import LockHeldError, StaleLockError


@dataclass
class LockInfo:
    pid: int
    hostname: str
    run_id: str | None
    acquired_at: str | None
    last_seen: str | None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LockInfo":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


def default_is_alive(pid: int, hostname: str) -> bool:
    """같은 호스트면 os.kill(pid,0)로 생사 판정. 다른 호스트는 확인 불가 → 보수적으로 살아있음."""
    if hostname != socket.gethostname():
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                      # 존재하나 시그널 권한 없음 → 살아있음
    except (OSError, ValueError, TypeError):
        return False
    return True


class SessionLock:
    def __init__(self, path: str, info: LockInfo):
        self.path = str(path)
        self.info = info

    @classmethod
    def acquire(cls, path: str, identity, *, is_process_alive=default_is_alive,
                allow_stale_takeover: bool = False, now_iso=None) -> "SessionLock":
        path = str(path)
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        now = now_iso() if now_iso else None
        info = LockInfo(pid=os.getpid(), hostname=socket.gethostname(),
                        run_id=getattr(identity, "run_id", None),
                        acquired_at=now, last_seen=now)
        payload = json.dumps(info.to_dict(), ensure_ascii=False, indent=2)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            _reject_or_takeover(path, is_process_alive, allow_stale_takeover)
            _atomic_write(path, payload)          # stale 회수(원자적 교체)
            return cls(path, info)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)                  # 원자적 신규 획득(O_EXCL 승자)
        except Exception:
            with contextlib.suppress(OSError):    # 부분 기록된 lock 파일 제거
                os.remove(path)
            raise
        return cls(path, info)

    def release(self) -> None:
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass


def _reject_or_takeover(path: str, is_process_alive, allow_stale_takeover: bool) -> None:
    existing = read_lock(path)
    if existing is not None and is_process_alive(existing.pid, existing.hostname):
        raise LockHeldError(
            f"lock held by live process pid={existing.pid}@{existing.hostname} "
            f"(run_id={existing.run_id}); refusing second writer")
    if not allow_stale_takeover:
        holder = f"pid={existing.pid}@{existing.hostname}" if existing else "unreadable"
        raise StaleLockError(
            f"stale lock ({holder}); recover only via verified resume/restart")


def read_lock(path: str) -> LockInfo | None:
    path = str(path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return LockInfo.from_dict(json.load(f))
    except (json.JSONDecodeError, OSError, TypeError):
        return None                              # 손상/판독불가 → 없는 것으로(회수 정책이 판단)


def _atomic_write(path: str, payload: str) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):        # 쓰기 실패 시 .tmp 잔류 방지
            os.remove(tmp)
        raise
