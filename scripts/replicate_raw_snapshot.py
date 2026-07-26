#!/usr/bin/env python3
"""immutable raw snapshot tree를 외부 tar로 복제하고 restore smoke를 수행한다."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.raw_snapshot import RawSnapshotStore

CHUNK = 1024 * 1024


class RawReplicaError(RuntimeError):
    """복제 안전·검증 계약 위반."""


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_basename(name: str) -> str:
    if not name or name != os.path.basename(name) or name in {".", ".."}:
        raise RawReplicaError("archive_name은 경로 없는 안전한 basename이어야 한다")
    return name


def _is_within(child: str, parent: str) -> bool:
    child = os.path.realpath(child)
    parent = os.path.realpath(parent)
    return child == parent or child.startswith(parent + os.sep)


def _repo_root(source_root: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", source_root, "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=10,
    )
    return (
        os.path.realpath(result.stdout.strip())
        if result.returncode == 0 and result.stdout.strip()
        else None
    )


def verify_payload_files(root: str, rel_paths: list[str]) -> int:
    """gzip을 실제 해제해 payload SHA가 content-addressed 파일명과 같은지 검증."""
    verified = 0
    for rel in rel_paths:
        expected = os.path.basename(rel)[: -len(".json.gz")]
        digest = hashlib.sha256()
        with gzip.open(os.path.join(root, rel), "rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise RawReplicaError(f"payload SHA 불일치: {rel}")
        verified += 1
    return verified


def _create_deterministic_tar(source_root: str, output: str,
                              rel_paths: list[str]) -> None:
    root_name = os.path.basename(source_root.rstrip(os.sep))
    with tarfile.open(output, "x", format=tarfile.PAX_FORMAT) as archive:
        for rel in rel_paths:
            source = os.path.join(source_root, rel)
            info = archive.gettarinfo(source, arcname=f"{root_name}/{rel}")
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            with open(source, "rb") as fh:
                archive.addfile(info, fh)


def _validate_members(archive: tarfile.TarFile, root_name: str) -> None:
    prefix = root_name + "/"
    for member in archive.getmembers():
        name = member.name
        if member.issym() or member.islnk() or not member.isfile():
            raise RawReplicaError(f"restore 대상에 비정규 파일 존재: {name}")
        if not name.startswith(prefix) or ".." in Path(name).parts:
            raise RawReplicaError(f"restore path traversal 거부: {name}")


def replicate_raw_snapshot(
    *, source_root: str, target_dir: str, archive_name: str,
    target_id: str, target_kind: str, allow_same_device: bool = False,
) -> dict:
    source = os.path.realpath(source_root)
    target = os.path.realpath(target_dir)
    name = _safe_basename(archive_name)
    if not os.path.isdir(source):
        raise RawReplicaError("source raw root가 디렉터리가 아니다")
    if not os.path.isdir(target):
        raise RawReplicaError("target_dir가 디렉터리가 아니다")
    if source == target or target.startswith(source + os.sep):
        raise RawReplicaError("target_dir는 source raw tree 밖이어야 한다")
    repo = _repo_root(source)
    if repo and _is_within(target, repo):
        raise RawReplicaError("target_dir는 저장소 밖이어야 한다")
    if os.stat(source).st_dev == os.stat(target).st_dev and not allow_same_device:
        raise RawReplicaError(
            "source와 target이 동일 device다; 검증된 cloud sync는 "
            "--allow-same-device로 명시해야 한다")
    final = os.path.join(target, name)
    partial = final + ".partial"
    if os.path.exists(final) or os.path.exists(partial):
        raise RawReplicaError("기존 final/partial overwrite 거부")

    source_store = RawSnapshotStore(source)
    before = source_store.manifest()
    try:
        source_payloads_verified = verify_payload_files(
            source, source_store.list_content_files())
        _create_deterministic_tar(source, partial, source_store.list_content_files())
        with open(partial, "rb+") as fh:
            os.fsync(fh.fileno())
        archive_hash = file_sha256(partial)

        with tempfile.TemporaryDirectory(prefix="raw_restore_smoke_") as temp:
            with tarfile.open(partial, "r") as archive:
                _validate_members(archive, before["root_basename"])
                archive.extractall(temp)
            restored = RawSnapshotStore(
                os.path.join(temp, before["root_basename"])
            ).manifest()
            restored_payloads_verified = verify_payload_files(
                os.path.join(temp, before["root_basename"]),
                restored["files"] and [
                    entry["rel_path"] for entry in restored["files"]
                ],
            )
            restored_match = (
                restored["schema"] == before["schema"]
                and restored["content_file_count"] == before["content_file_count"]
                and restored["raw_content_sha256"] == before["raw_content_sha256"]
                and restored["files"] == before["files"]
            )
            if not restored_match:
                raise RawReplicaError("restore smoke raw manifest 불일치")

        after = source_store.manifest()
        if before != after:
            raise RawReplicaError("archive 생성 중 source raw tree 변경 감지")
        os.replace(partial, final)
        try:
            directory_fd = os.open(target, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # 일부 cloud sync 가상 디렉터리는 directory fsync를 지원하지 않는다.
            pass
        return {
            "schema": "raw-external-replica/receipt/v2",
            "status": "success",
            "target_id": target_id,
            "target_kind": target_kind,
            "archive_basename": name,
            "archive_size_bytes": os.path.getsize(final),
            "archive_sha256": archive_hash,
            "source_content_file_count": before["content_file_count"],
            "source_raw_content_sha256": before["raw_content_sha256"],
            "source_payload_files_verified": source_payloads_verified,
            "restored_content_file_count": restored["content_file_count"],
            "restored_raw_content_sha256": restored["raw_content_sha256"],
            "restored_payload_files_verified": restored_payloads_verified,
            "restore_content_match": True,
        }
    except Exception:
        if os.path.exists(partial):
            os.remove(partial)
        raise


def _write_json_atomic(path: str, payload: dict) -> None:
    output = os.path.abspath(path)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    partial = output + ".partial"
    if os.path.exists(partial):
        raise RawReplicaError("receipt stale partial 존재")
    with open(partial, "x", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(partial, output)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="raw snapshot 외부 복제 + restore smoke")
    ap.add_argument("--source-root", required=True)
    ap.add_argument("--target-dir", required=True)
    ap.add_argument("--archive-name", required=True)
    ap.add_argument("--target-id", required=True)
    ap.add_argument("--target-kind", required=True)
    ap.add_argument(
        "--allow-same-device", action="store_true",
        help="검증된 cloud sync 등 의도된 동일 device target만 명시적으로 허용")
    ap.add_argument("--receipt", required=True)
    ap.add_argument("--sanitized-receipt", required=True)
    args = ap.parse_args(argv)
    receipt = replicate_raw_snapshot(
        source_root=args.source_root, target_dir=args.target_dir,
        archive_name=args.archive_name, target_id=args.target_id,
        target_kind=args.target_kind, allow_same_device=args.allow_same_device,
    )
    _write_json_atomic(args.receipt, receipt)
    _write_json_atomic(args.sanitized_receipt, receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
