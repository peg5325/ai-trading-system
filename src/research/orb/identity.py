"""ORB P3 스냅샷 identity 검증 — 이슈 #91 (4-A).

사전등록 §2: **4개 hash 중 하나라도 불일치하면 분석을 실행하지 않는다(fail-closed)**.
raw 아티팩트가 실행 환경에 없으면 그 항목은 `NOT_RUN` 이며, 나머지 3개만 맞았다고 진행하지
않는다(§10 P3 미충족 = `preconditions_not_result_criteria` — 프로세스 게이트가 과학적
판정으로 위장하지 않는다).

검증 도구는 사전등록 §2 가 지정한 것을 그대로 쓴다:
  - snapshot file/content → `src.history.research_snapshot.file_sha256` / `content_sha256`
  - manifest data → tracked `docs/research/P0_US30_ORB_CLEAN_1M_MANIFEST.json` 의
    `data_content_sha256`
  - raw content → `src.history.raw_snapshot.RawSnapshotStore(root).manifest()["raw_content_sha256"]`
기대값은 전부 **계약 JSON 의 `data_identity`** 에서 읽는다(하드코딩 금지).
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.history.raw_snapshot import RawSnapshotStore
from src.history.research_snapshot import content_sha256, file_sha256
from src.research.orb.contract import _require

# IdentityResult.checks 의 고정 순서(사전등록 §2 나열 순).
CHECK_NAMES = ("snapshot_file", "snapshot_content", "manifest_data", "raw_content")


class IdentityMismatchError(RuntimeError):
    """`require_identity` 가 identity 불일치/미검증 시 던진다(fail-closed 강제)."""


@dataclass(frozen=True)
class IdentityCheck:
    name: str          # "snapshot_file" | "snapshot_content" | "manifest_data" | "raw_content"
    expected: str
    actual: str | None  # None = 아티팩트 부재
    status: str        # "match" | "mismatch" | "not_run"


@dataclass(frozen=True)
class IdentityResult:
    checks: tuple[IdentityCheck, ...]   # 항상 4개, CHECK_NAMES 순서 고정
    ok: bool                            # 4개 전부 "match" 일 때만 True
    verdict: str | None                 # not_run 이 하나라도 있으면 "NOT_RUN", 아니면 None


def _make_check(name: str, expected: str, actual: str | None) -> IdentityCheck:
    if actual is None:
        return IdentityCheck(name=name, expected=expected, actual=None, status="not_run")
    status = "match" if actual == expected else "mismatch"
    return IdentityCheck(name=name, expected=expected, actual=actual, status=status)


def _snapshot_file_actual(snapshot_path: str) -> str | None:
    """스냅샷 파일이 없으면 부재(None). 있는데 읽기 실패하면 예외를 그대로 전파한다."""
    if not os.path.isfile(snapshot_path):
        return None
    return file_sha256(snapshot_path)


def _snapshot_content_actual(snapshot_path: str) -> str | None:
    if not os.path.isfile(snapshot_path):
        return None
    con = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True)
    try:
        return content_sha256(con)
    finally:
        con.close()


def _manifest_data_actual(manifest_path: str) -> str | None:
    if not os.path.isfile(manifest_path):
        return None
    with open(manifest_path, encoding="utf-8") as f:
        data = json.load(f)
    return str(data["data_content_sha256"])


def _raw_content_actual(raw_root: str) -> str | None:
    """raw 스토어 root 가 없거나 완료본이 하나도 없으면 부재(None).

    `RawSnapshotStore.__init__` 는 root 디렉터리를 없으면 만들어 버리므로(그 자체는 read-only
    검증을 위한 부작용으로 부적절), 존재 여부를 먼저 확인한 뒤에만 생성자를 호출한다 — identity
    검증이 디렉터리를 새로 만드는 부작용을 내지 않게 한다.
    """
    if not os.path.isdir(raw_root):
        return None
    manifest = RawSnapshotStore(raw_root).manifest()
    if manifest["content_file_count"] == 0:
        return None
    return str(manifest["raw_content_sha256"])


def verify_snapshot_identity(*, contract: Mapping[str, Any], snapshot_path: str, raw_root: str,
                             manifest_path: str) -> IdentityResult:
    """사전등록 §2 의 4개 hash 를 실행 환경의 tracked artifact 와 대조한다(fail-closed).

    계약에서 값을 읽을 때 `_require`(contract.py 공용)를 쓴다 — `dict[...]` 직접 접근은 누락
    시 평범한 `KeyError` 를 내어 나머지 계약 위반(=`ContractIntegrityError`)과 오류 종류가
    갈렸었다(v1.1, 독립 리뷰 정리 지적).
    """
    ident = _require(contract, "data_identity")
    checks = (
        _make_check("snapshot_file", str(_require(ident, "snapshot_file_sha256")),
                    _snapshot_file_actual(snapshot_path)),
        _make_check("snapshot_content", str(_require(ident, "snapshot_content_sha256")),
                    _snapshot_content_actual(snapshot_path)),
        _make_check("manifest_data", str(_require(ident, "manifest_data_sha256")),
                    _manifest_data_actual(manifest_path)),
        _make_check("raw_content", str(_require(ident, "raw_content_sha256")),
                    _raw_content_actual(raw_root)),
    )
    ok = all(c.status == "match" for c in checks)
    verdict = "NOT_RUN" if any(c.status == "not_run" for c in checks) else None
    return IdentityResult(checks=checks, ok=ok, verdict=verdict)


def require_identity(result: IdentityResult) -> None:
    """시뮬레이션 진입점용 가드 — `result.ok` 가 False 면 예외(무시하고 진행 불가)."""
    if not result.ok:
        detail = "; ".join(f"{c.name}={c.status}" for c in result.checks)
        raise IdentityMismatchError(
            f"스냅샷 identity 검증 실패(fail-closed, verdict={result.verdict}): {detail}")
