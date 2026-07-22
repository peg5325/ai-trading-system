#!/usr/bin/env python3
"""market-history SQLite DB를 외부 저장소로 안전하게 복제하는 재사용 CLI (#73).

canonical `data/market_history.sqlite` 또는 검증된 백업을 외장 디스크/NAS/
검증 가능한 클라우드 remote 로 복제한다. 표준 라이브러리만 사용한다.

핵심 안전 원칙(fail-closed):
  - 원본 DB 는 절대 변경하지 않는다(read-only open + SQLite backup API).
  - 저장소 내부·원본 경로·동일 파일·동일 device 로의 복제를 기본 거부한다.
  - 임시 `<name>.partial` 로 먼저 쓰고 flush/fsync 후 atomic rename.
  - 기존 같은 이름 파일을 조용히 덮어쓰지 않는다.
  - SHA-256 + PRAGMA integrity_check + interval 행수/symbol/기간이 모두
    일치할 때에만 SUCCESS receipt 를 확정한다.
  - 외부 복제본을 임시 로컬 경로로 되읽는 restore smoke test 로 복원 가능성 증명.

시크릿·자격증명·환경변수 값은 읽지도 기록하지도 않는다.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

CANDLE_TABLE = "candle_history"
CHUNK = 1024 * 1024

# 종료 코드
EXIT_OK = 0
EXIT_SAFETY = 10       # 검증 전 안전/입력 거부 (복제 시작 안 함)
EXIT_VERIFY = 20       # 복제 후 무결성/의미 검증 실패
EXIT_ERROR = 30        # 예기치 못한 오류


class SafetyError(Exception):
    """복제를 시작하기 전 fail-closed 로 거부해야 하는 조건."""


class VerificationError(Exception):
    """복제본이 원본과 일치하지 않아 SUCCESS 로 확정할 수 없는 조건."""


# --------------------------------------------------------------------------- #
# 기본 유틸
# --------------------------------------------------------------------------- #
def compute_sha256(path: str) -> str:
    """파일 전체를 스트리밍으로 읽어 SHA-256 hex digest 반환."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def read_only_uri(path: str) -> str:
    """원본을 변경하지 않고 사이드카(-wal/-shm)도 만들지 않는 read-only URI.

    immutable=1 은 SQLite 가 파일을 불변으로 간주해 -wal/-shm 을 생성/사용하지
    않게 한다. 정적(체크포인트 완료)·DELETE 모드 DB 는 이렇게 열면 부작용 없이
    완전한 데이터를 읽는다. 미체크포인트 WAL 프레임이 있을 수 있는 경우에는
    이 URI 를 원본에 직접 쓰지 않고 ``consistent_source`` 의 복사 경로를 쓴다.
    """
    return f"file:{path}?mode=ro&immutable=1"


def _pending_wal(path: str) -> bool:
    """미체크포인트 WAL 프레임이 있을 수 있는가(= -wal 이 비어있지 않음).

    lone -shm 이나 0바이트 -wal 은 체크포인트 완료 상태이므로 pending 이 아니다.
    """
    wal = path + "-wal"
    return os.path.exists(wal) and os.path.getsize(wal) > 0


@contextlib.contextmanager
def consistent_source(path: str):
    """원본을 변경하지 않고 일관 상태로 읽을 수 있는 경로를 yield 한다.

    - 정적/체크포인트 완료(-wal 없음 또는 0바이트): 원본 경로를 그대로 yield.
      호출측은 immutable read-only 로 열어 사이드카를 만들지 않고 완전한
      데이터를 읽는다(복사 없음).
    - 미체크포인트 WAL 가능성(-wal 이 비어있지 않음): 원본과 -wal 을 임시
      디렉터리로 **복사**(shutil, sqlite open 아님)한 뒤 임시본만 열어
      checkpoint(TRUNCATE) 로 병합한다. 원본은 sqlite 로 열지 않으므로
      사이드카 생성/체크포인트/변경이 원본에 일어나지 않는다.
    """
    if not _pending_wal(path):
        yield path
        return
    tmpdir = tempfile.mkdtemp(prefix="repl_src_")
    try:
        work = os.path.join(tmpdir, os.path.basename(path))
        shutil.copyfile(path, work)
        wal = path + "-wal"
        if os.path.exists(wal):
            shutil.copyfile(wal, work + "-wal")  # -shm 은 open 시 재생성됨
        con = sqlite3.connect(work)
        try:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.commit()
        finally:
            con.close()
        yield work
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def db_inventory(path: str) -> dict[str, Any]:
    """DB 를 read-only 로 열어 무결성/의미 지문을 수집한다.

    반환: integrity, total_rows, distinct_symbols, intervals{iv:{rows,symbols,
    min_ts,max_ts}}. candle_history 가 없으면 의미 지표는 비운다(integrity 만).
    """
    con = sqlite3.connect(read_only_uri(path), uri=True)
    try:
        cur = con.cursor()
        integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
        has_table = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (CANDLE_TABLE,),
        ).fetchone()
        intervals: dict[str, Any] = {}
        total_rows = 0
        distinct_symbols = 0
        if has_table:
            for iv, rows, nsym, tmin, tmax in cur.execute(
                f"SELECT interval, COUNT(*), COUNT(DISTINCT symbol), "
                f"MIN(ts_utc), MAX(ts_utc) FROM {CANDLE_TABLE} "
                f"GROUP BY interval ORDER BY interval"
            ):
                intervals[iv] = {
                    "rows": rows,
                    "symbols": nsym,
                    "min_ts": tmin,
                    "max_ts": tmax,
                }
            total_rows = cur.execute(
                f"SELECT COUNT(*) FROM {CANDLE_TABLE}"
            ).fetchone()[0]
            distinct_symbols = cur.execute(
                f"SELECT COUNT(DISTINCT symbol) FROM {CANDLE_TABLE}"
            ).fetchone()[0]
        return {
            "integrity": integrity,
            "has_candle_table": bool(has_table),
            "total_rows": total_rows,
            "distinct_symbols": distinct_symbols,
            "intervals": intervals,
        }
    finally:
        con.close()


def snapshot_via_backup_api(src_path: str, dst_path: str) -> None:
    """SQLite backup API 로 원본을 일관되게 복제한다.

    원본은 read-only 로 열며, writer/WAL 이 있어도 커밋된 일관 상태만 복제된다.
    단순 cp 로 live DB 를 복사하지 않는다.
    """
    src = sqlite3.connect(read_only_uri(src_path), uri=True)
    try:
        dst = sqlite3.connect(dst_path)  # dst open 실패해도 src 는 닫힌다
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def fsync_path(path: str) -> None:
    """파일 내용을 디스크로 flush 한다."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(dir_path: str) -> None:
    """디렉터리 엔트리(rename)를 디스크로 flush 한다."""
    fd = os.open(dir_path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def git_head(work_dir: str) -> Optional[str]:
    """work_dir 이 속한 저장소의 HEAD 커밋을 조회(없으면 None)."""
    try:
        out = subprocess.run(
            ["git", "-C", work_dir, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


# --------------------------------------------------------------------------- #
# 안전 검사 (fail-closed) — 복제를 시작하기 전에 모두 통과해야 함
# --------------------------------------------------------------------------- #
def _resolve(path: str) -> str:
    return os.path.realpath(os.path.abspath(path))


def _is_within(child: str, parent: str) -> bool:
    """child 가 parent 와 같거나 그 하위 경로인지(realpath 기준)."""
    child = _resolve(child)
    parent = _resolve(parent)
    if child == parent:
        return True
    return child.startswith(parent + os.sep)


def detect_repo_root(source_db: str) -> Optional[str]:
    """원본이 속한 git 저장소 최상위(없으면 None)."""
    src_dir = os.path.dirname(_resolve(source_db))
    try:
        out = subprocess.run(
            ["git", "-C", src_dir, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return _resolve(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def validate_replica_name(name: str) -> None:
    """replica 파일명이 순수 basename 인지(경로/트래버설 금지) 확인."""
    if not name:
        raise SafetyError("replica 이름이 비어 있습니다.")
    if name != os.path.basename(name) or os.sep in name or (
        os.altsep and os.altsep in name
    ):
        raise SafetyError(f"replica 이름에 경로 구분자가 포함됨: {name!r}")
    if name in (".", "..") or ".." in name.split(os.sep):
        raise SafetyError(f"replica 이름에 상위 경로 참조가 포함됨: {name!r}")


def run_safety_checks(
    *,
    source_db: str,
    target_dir: str,
    replica_name: str,
    repo_root: Optional[str],
    allow_same_device: bool,
) -> str:
    """모든 fail-closed 안전 검사. 통과 시 최종 target 파일 절대경로 반환."""
    if not os.path.isfile(source_db):
        raise SafetyError(f"원본 DB 가 파일이 아닙니다: {source_db}")
    validate_replica_name(replica_name)

    src_real = _resolve(source_db)
    tgt_dir_real = _resolve(target_dir)

    if not os.path.isdir(tgt_dir_real):
        raise SafetyError(f"target 디렉터리가 존재하지 않습니다: {target_dir}")

    final_path = os.path.join(tgt_dir_real, replica_name)

    # 1) 동일 파일 거부
    if _resolve(final_path) == src_real:
        raise SafetyError("target 이 원본과 동일한 파일입니다.")
    # 2) target 디렉터리가 원본 디렉터리와 동일 → 거부
    if tgt_dir_real == os.path.dirname(src_real):
        raise SafetyError("target 디렉터리가 원본 DB 의 디렉터리와 동일합니다.")
    # 3) 저장소 내부 거부 (repo_root 아래) — data/·data/backups 포함
    if repo_root and _is_within(tgt_dir_real, repo_root):
        raise SafetyError(f"target 이 저장소 내부입니다: {tgt_dir_real}")
    # 4) 동일 device 거부 (외부 저장소가 아님) — 명시적 opt-in 시에만 허용
    src_dev = os.stat(src_real).st_dev
    tgt_dev = os.stat(tgt_dir_real).st_dev
    if src_dev == tgt_dev and not allow_same_device:
        raise SafetyError(
            "원본과 target 이 동일 device 입니다(외부 저장소 아님). "
            "검증된 클라우드 동기화 폴더 등 의도된 경우에만 "
            "--allow-same-device 로 명시하세요."
        )
    return final_path


# --------------------------------------------------------------------------- #
# 복제 결과 / receipt
# --------------------------------------------------------------------------- #
@dataclass
class ReplicationResult:
    status: str = "failed"                 # "success" | "failed" | "dry-run"
    reason: Optional[str] = None
    source_inventory: dict[str, Any] = field(default_factory=dict)
    replica_inventory: dict[str, Any] = field(default_factory=dict)
    restore_smoke: dict[str, Any] = field(default_factory=dict)
    source_file_sha256: Optional[str] = None
    snapshot_sha256: Optional[str] = None
    replica_sha256: Optional[str] = None
    source_file_size: Optional[int] = None
    replica_file_size: Optional[int] = None
    final_path: Optional[str] = None
    retention_deleted: list[str] = field(default_factory=list)


def semantic_match(a: dict[str, Any], b: dict[str, Any]) -> tuple[bool, str]:
    """두 inventory 의 의미 지표(행수/symbol/기간/integrity) 일치 여부."""
    if a.get("integrity") != "ok" or b.get("integrity") != "ok":
        return False, "integrity_check 가 ok 가 아닙니다."
    for key in ("total_rows", "distinct_symbols"):
        if a.get(key) != b.get(key):
            return False, f"{key} 불일치: {a.get(key)} != {b.get(key)}"
    if a.get("intervals") != b.get("intervals"):
        return False, "interval 별 행수/symbol/기간 불일치."
    return True, "ok"


def restore_smoke_test(
    replica_path: str,
    expected_sha: str,
    expected_inventory: dict[str, Any],
    smoke_dir: Optional[str],
) -> dict[str, Any]:
    """외부 복제본을 임시 로컬 경로로 복원해 read-only 검증한다.

    canonical DB 위치에는 절대 복원하지 않는다. 임시 파일은 검증 후 삭제한다.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix="restore_smoke_", suffix=".sqlite", dir=smoke_dir
    )
    os.close(tmp_fd)
    try:
        shutil.copyfile(replica_path, tmp_path)
        restored_sha = compute_sha256(tmp_path)
        inv = db_inventory(tmp_path)
        ok_sha = restored_sha == expected_sha
        ok_sem, sem_reason = semantic_match(expected_inventory, inv)
        return {
            "status": "ok" if (ok_sha and ok_sem) else "failed",
            "sha256_match": ok_sha,
            "restored_sha256": restored_sha,
            "integrity": inv.get("integrity"),
            "semantic_match": ok_sem,
            "reason": "ok" if (ok_sha and ok_sem) else sem_reason,
        }
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)


# --------------------------------------------------------------------------- #
# 핵심 복제 흐름
# --------------------------------------------------------------------------- #
def replicate(
    *,
    source_db: str,
    target_dir: str,
    replica_name: str,
    repo_root: Optional[str],
    allow_same_device: bool,
    dry_run: bool,
    smoke_dir: Optional[str],
) -> ReplicationResult:
    """복제 전체 절차. 안전 검사 → snapshot → 검증 → atomic rename →
    restore smoke. 실패 시 partial 을 성공본처럼 남기지 않는다."""
    result = ReplicationResult()

    # 1) 안전 검사 (복제 시작 전 fail-closed)
    final_path = run_safety_checks(
        source_db=source_db,
        target_dir=target_dir,
        replica_name=replica_name,
        repo_root=repo_root,
        allow_same_device=allow_same_device,
    )
    result.final_path = final_path

    # 기존 성공본 보호: 같은 이름이 이미 있으면 절대 덮어쓰지 않는다.
    if os.path.exists(final_path):
        raise SafetyError(
            f"동일 이름의 복제본이 이미 존재합니다(덮어쓰기 금지): {final_path}"
        )

    # 2) 원본 지문(실제 on-disk 파일) + 일관 상태 inventory
    result.source_file_size = os.path.getsize(source_db)
    result.source_file_sha256 = compute_sha256(source_db)

    with consistent_source(source_db) as work_src:
        try:
            src_inv = db_inventory(work_src)
        except sqlite3.Error as exc:
            # 아예 열 수 없는 손상 원본도 verify 실패로 분류(EXIT_VERIFY).
            raise VerificationError(f"원본 DB 를 열 수 없음: {exc}") from exc
        result.source_inventory = src_inv
        if src_inv.get("integrity") != "ok":
            raise VerificationError(
                f"원본 integrity_check 실패: {src_inv.get('integrity')}"
            )

        if dry_run:
            result.status = "dry-run"
            result.reason = "dry-run: 파일을 생성하지 않음"
            return result

        partial_path = final_path + ".partial"
        # 이전 실패 잔여 partial 정리(성공본은 위에서 이미 보호됨)
        with contextlib.suppress(OSError):
            os.remove(partial_path)

        renamed = False
        try:
            # 3) SQLite backup API 로 일관 snapshot → .partial
            snapshot_via_backup_api(work_src, partial_path)
            fsync_path(partial_path)

            # 4) partial 검증: integrity + 의미 + snapshot SHA
            snap_sha = compute_sha256(partial_path)
            result.snapshot_sha256 = snap_sha
            replica_inv = db_inventory(partial_path)
            result.replica_inventory = replica_inv
            ok_sem, sem_reason = semantic_match(src_inv, replica_inv)
            if not ok_sem:
                raise VerificationError(f"복제본 의미 검증 실패: {sem_reason}")

            # 5) 성공본 보호 재확인 후 atomic rename
            if os.path.exists(final_path):
                raise SafetyError(
                    f"rename 직전 동일 이름 복제본 발견(덮어쓰기 금지): "
                    f"{final_path}"
                )
            os.rename(partial_path, final_path)
            renamed = True
            fsync_dir(os.path.dirname(final_path))

            # 6) 최종 복제본 재검증: SHA 동일 + integrity
            result.replica_file_size = os.path.getsize(final_path)
            final_sha = compute_sha256(final_path)
            result.replica_sha256 = final_sha
            if final_sha != snap_sha:
                raise VerificationError(
                    "rename 후 SHA-256 이 snapshot 과 다릅니다."
                )
            final_inv = db_inventory(final_path)
            if final_inv.get("integrity") != "ok":
                raise VerificationError("최종 복제본 integrity_check 실패.")

            # 7) restore smoke: 임시 로컬 경로로 복원해 재검증
            smoke = restore_smoke_test(
                final_path, final_sha, src_inv, smoke_dir
            )
            result.restore_smoke = smoke
            if smoke.get("status") != "ok":
                raise VerificationError(
                    f"restore smoke 실패: {smoke.get('reason')}"
                )

            result.status = "success"
            result.reason = "ok"
            return result
        except Exception:
            # 실패 시 partial 을 성공본처럼 남기지 않는다.
            with contextlib.suppress(OSError):
                os.remove(partial_path)
            # rename 이후 검증 실패면 방금 만든 실패본(final)도 제거한다.
            # (rename 직전 final 부재를 확인했으므로 우리가 만든 파일만 지운다.)
            # 기존의 다른 성공 복제본은 이름이 달라 영향받지 않는다.
            if renamed:
                with contextlib.suppress(OSError):
                    os.remove(final_path)
            raise


def apply_retention(
    target_dir: str,
    source_stem: str,
    keep: int,
    protect: str,
) -> list[str]:
    """target_dir 에서 source_stem 으로 시작하는 복제본 중 최신 keep 개만 남기고
    삭제. 기본 OFF(keep is None). protect(방금 만든 복제본)는 절대 삭제 안 함.
    복제본 네이밍 패턴에 맞는 파일만 삭제해 무관 파일 훼손을 막는다."""
    deleted: list[str] = []
    if keep is None or keep < 1:
        return deleted
    protect_real = _resolve(protect)
    # 도구가 만든 복제본 네이밍(<stem>.replica.<UTC>.sqlite)만 대상으로 한다.
    # 무관한 <stem>*.sqlite (예: 수동 export)는 절대 삭제하지 않는다.
    pattern = re.compile(
        rf"^{re.escape(source_stem)}\.replica\.\d{{8}}T\d{{6}}Z\.sqlite$"
    )
    candidates = []
    for name in os.listdir(target_dir):
        if not pattern.match(name):
            continue
        full = os.path.join(target_dir, name)
        if not os.path.isfile(full) or _resolve(full) == protect_real:
            continue
        candidates.append(full)
    # 파일명(타임스탬프 내장) 역순 → 최신 우선. 방금 만든 protect 는 목록 밖.
    candidates.sort(reverse=True)
    # protect 를 최신 1개로 간주하므로 keep-1 개의 과거본을 유지
    for old in candidates[max(keep - 1, 0):]:
        with contextlib.suppress(OSError):
            os.remove(old)
            deleted.append(old)
    return deleted


# --------------------------------------------------------------------------- #
# receipt 생성 / sanitize
# --------------------------------------------------------------------------- #
def _reason_category(reason: Optional[str]) -> str:
    """sanitized receipt 용 경로 없는 사유 범주.

    reason 은 실패 시 예외 메시지(절대경로 포함 가능)를 담을 수 있으므로,
    ':' 앞 토큰만 취하고 화이트리스트로 제한해 어떤 절대경로/홈경로도
    sanitized 산출물로 새지 않게 한다.
    """
    cat = (reason or "").split(":", 1)[0].strip()
    return cat if cat in {
        "ok", "dry-run", "safety", "verify", "error"
    } else "failed"


def build_receipt(
    result: ReplicationResult,
    *,
    target_id: str,
    target_kind: str,
    source_db: str,
    target_dir: str,
    created_utc: str,
    source_commit: Optional[str],
    sanitized: bool,
) -> dict[str, Any]:
    """receipt(JSON) 을 구성한다. sanitized=True 면 절대경로/홈 경로를 제외한다.

    시크릿·자격증명·환경변수 값은 어느 계층에도 기록하지 않는다.
    """
    receipt: dict[str, Any] = {
        "schema": "history-external-replica/receipt/v1",
        "status": result.status,
        "target_id": target_id,
        "target_kind": target_kind,
        "created_utc": created_utc,
        "source_commit": source_commit,
        "source": {
            "basename": os.path.basename(source_db),
            "size_bytes": result.source_file_size,
            "sha256": result.source_file_sha256,
            "integrity": result.source_inventory.get("integrity"),
            "total_rows": result.source_inventory.get("total_rows"),
            "distinct_symbols": result.source_inventory.get("distinct_symbols"),
            "intervals": result.source_inventory.get("intervals"),
        },
        "replica": {
            "basename": (
                os.path.basename(result.final_path)
                if result.final_path else None
            ),
            "size_bytes": result.replica_file_size,
            "snapshot_sha256": result.snapshot_sha256,
            "replica_sha256": result.replica_sha256,
            "sha256_match": (
                result.snapshot_sha256 is not None
                and result.snapshot_sha256 == result.replica_sha256
            ),
            "integrity": result.replica_inventory.get("integrity"),
            "total_rows": result.replica_inventory.get("total_rows"),
            "distinct_symbols": result.replica_inventory.get("distinct_symbols"),
            "intervals": result.replica_inventory.get("intervals"),
        },
        "restore_smoke": result.restore_smoke,
    }
    if sanitized:
        # sanitized: 경로가 섞일 수 있는 free-form reason 대신 경로 없는 범주만
        # 노출한다(실패 사유 메시지에 절대경로가 포함될 수 있으므로).
        receipt["reason"] = _reason_category(result.reason)
    else:
        receipt["reason"] = result.reason
        receipt["source"]["abs_path"] = _resolve(source_db)
        receipt["replica"]["abs_path"] = result.final_path
        receipt["replica"]["target_dir"] = _resolve(target_dir)
        receipt["retention_deleted"] = result.retention_deleted
    return receipt


def _stable_json(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def write_json(path: str, obj: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(_resolve(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_stable_json(obj))
        fh.write("\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="market-history SQLite DB 를 외부 저장소로 안전 복제 (#73)"
    )
    p.add_argument("--source-db", required=True,
                   help="복제할 원본 DB(변경하지 않음)")
    p.add_argument("--target-dir", required=True,
                   help="외부 저장소 디렉터리(외장/NAS/클라우드 동기화 경로)")
    p.add_argument("--target-id", required=True,
                   help="target 식별 라벨(예: external-ssd-T7, nas-backup)")
    p.add_argument("--target-kind", default="other",
                   choices=["external_disk", "nas", "cloud_sync",
                            "cloud_object", "other"],
                   help="target 종류(provenance 기록용)")
    p.add_argument("--replica-name", default=None,
                   help="복제본 파일명(basename). 미지정 시 원본+UTC 타임스탬프")
    p.add_argument("--as-of-utc", default=None,
                   help="복제본명/receipt 에 쓸 UTC 시각(ISO8601). 결정성 옵션")
    p.add_argument("--repo-root", default=None,
                   help="저장소 최상위 override(미지정 시 원본 기준 자동탐지)")
    p.add_argument("--allow-same-device", action="store_true",
                   help="동일 device target 허용(검증된 클라우드 동기화 폴더 등)")
    p.add_argument("--dry-run", action="store_true",
                   help="read-only 검사만 수행하고 파일을 생성하지 않음")
    p.add_argument("--receipt", default=None,
                   help="상세 receipt JSON 경로(절대경로 포함, gitignore 권장)")
    p.add_argument("--sanitized-receipt", default=None,
                   help="sanitized receipt JSON 경로(절대경로/홈 경로 제외)")
    p.add_argument("--smoke-dir", default=None,
                   help="restore smoke 임시 복원 디렉터리(기본 시스템 temp)")
    p.add_argument("--retention-count", type=int, default=None,
                   help="target 에 남길 최신 복제본 수(기본 OFF, 삭제 안 함)")
    p.add_argument("--source-commit", default=None,
                   help="원본 커밋 override(미지정 시 자동탐지)")
    return p


def default_replica_name(source_db: str, as_of_utc: str) -> str:
    stem = os.path.basename(source_db)
    if stem.endswith(".sqlite"):
        stem = stem[: -len(".sqlite")]
    stamp = as_of_utc.replace(":", "").replace("-", "").replace(".", "")
    return f"{stem}.replica.{stamp}.sqlite"


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    as_of = args.as_of_utc or datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    replica_name = args.replica_name or default_replica_name(
        args.source_db, as_of
    )
    repo_root = (
        _resolve(args.repo_root) if args.repo_root
        else detect_repo_root(args.source_db)
    )
    source_commit = args.source_commit or git_head(
        os.path.dirname(_resolve(args.source_db))
    )
    source_stem = os.path.basename(args.source_db)
    if source_stem.endswith(".sqlite"):
        source_stem = source_stem[: -len(".sqlite")]

    result = ReplicationResult()
    exit_code = EXIT_OK
    try:
        result = replicate(
            source_db=args.source_db,
            target_dir=args.target_dir,
            replica_name=replica_name,
            repo_root=repo_root,
            allow_same_device=args.allow_same_device,
            dry_run=args.dry_run,
            smoke_dir=args.smoke_dir,
        )
        # retention 은 성공 후에만, 명시 옵션이 있을 때에만
        if result.status == "success" and args.retention_count is not None:
            result.retention_deleted = apply_retention(
                _resolve(args.target_dir),
                source_stem,
                args.retention_count,
                result.final_path,
            )
    except SafetyError as exc:
        result.status = "failed"
        result.reason = f"safety: {exc}"
        exit_code = EXIT_SAFETY
    except VerificationError as exc:
        result.status = "failed"
        result.reason = f"verify: {exc}"
        exit_code = EXIT_VERIFY
    except Exception as exc:  # noqa: BLE001 - CLI 경계에서 방어적 처리
        result.status = "failed"
        result.reason = f"error: {exc}"
        exit_code = EXIT_ERROR

    # receipt 는 성공/실패 모두 기록하되, status 는 실제 결과를 반영한다.
    # (실패 시 절대 status="success" 로 쓰지 않는다.)
    receipt_common = dict(
        target_id=args.target_id,
        target_kind=args.target_kind,
        source_db=args.source_db,
        target_dir=args.target_dir,
        created_utc=as_of,
        source_commit=source_commit,
    )
    if args.receipt:
        write_json(args.receipt, build_receipt(
            result, sanitized=False, **receipt_common))
    if args.sanitized_receipt:
        write_json(args.sanitized_receipt, build_receipt(
            result, sanitized=True, **receipt_common))

    # 사람이 읽을 요약
    summary = {
        "status": result.status,
        "reason": result.reason,
        "target_id": args.target_id,
        "target_kind": args.target_kind,
        "replica": os.path.basename(result.final_path) if result.final_path
        else None,
        "snapshot_sha256": result.snapshot_sha256,
        "replica_sha256": result.replica_sha256,
        "restore_smoke": result.restore_smoke.get("status")
        if result.restore_smoke else None,
    }
    print(_stable_json(summary))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
