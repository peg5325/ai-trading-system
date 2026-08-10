"""Tracked OOS unlock artifact 검증 로더.

이 모듈은 artifact를 생성하지 않고 blind OOS를 읽지도 않는다. 저장소·manifest·독립 리뷰의
증거 사슬을 fail-closed로 대조하고, 전부 통과한 경우에만 ``OOSUnlock``을 반환한다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.history.oos_guard import OOSLockError, OOSUnlock, blind_oos_window

UNLOCK_SCHEMA = "oos-unlock/v1"
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_CONTENT_SHA = re.compile(r"^[0-9a-f]{64}$")
_GITHUB_PULL_URL = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/"
    r"(?P<number>\d+)(?:#(?P<fragment>[-A-Za-z0-9_]+))?$"
)
_APPROVED_COMMENT_LINE = re.compile(
    r"^[ \t]*(?:VERDICT|RECOMMENDATION)[ \t]*:[ \t]*"
    r"(?:APPROVE|APPROVED)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_CANONICAL_MAIN_REF = "main"
_ALLOWED_SNAPSHOT_HASH_FIELDS = frozenset(
    {"content_sha256", "data_content_sha256", "raw_content_sha256"}
)


@dataclass(frozen=True)
class ReviewEvidence:
    """외부 독립 리뷰 검증기에 전달하는 artifact의 고정 리뷰 증거."""

    status: str
    url: str
    reviewer: str
    reviewed_commit: str


def _run_git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OOSLockError("OOS unlock git 증거 검증 실패") from exc
    if check and result.returncode != 0:
        raise OOSLockError(f"OOS unlock git 증거 불충족: {' '.join(args[:2])}")
    return result


def _repo_root(repo_root: str | Path | None) -> Path:
    if repo_root is not None:
        root = Path(repo_root).resolve()
    else:
        try:
            probe = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OOSLockError(
                "Git 저장소를 확인할 수 없어 OOS 잠금을 유지한다"
            ) from exc
        if probe.returncode != 0:
            raise OOSLockError("Git 저장소 밖에서는 OOS unlock을 검증할 수 없다")
        root = Path(probe.stdout.strip()).resolve()
    reported_root = Path(
        _run_git(root, "rev-parse", "--show-toplevel").stdout.strip()
    ).resolve()
    if not root.is_dir() or reported_root != root:
        raise OOSLockError("repo_root가 Git 최상위 경로가 아니다")
    return root


def _repo_file(repo: Path, value: str | Path, *, field: str) -> tuple[Path, str]:
    raw = Path(value)
    unresolved = raw if raw.is_absolute() else repo / raw
    normalized = Path(os.path.abspath(unresolved))
    try:
        lexical_relative = normalized.relative_to(repo)
    except ValueError as exc:
        raise OOSLockError(f"{field}가 저장소 밖을 가리킨다") from exc

    cursor = repo
    for part in lexical_relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise OOSLockError(f"{field} 경로에 symlink가 포함돼 있다")

    resolved = unresolved.resolve()
    try:
        relative = resolved.relative_to(repo).as_posix()
    except ValueError as exc:
        raise OOSLockError(f"{field}가 저장소 밖을 가리킨다") from exc
    if not resolved.is_file():
        raise OOSLockError(f"{field}가 일반 파일이 아니다")
    return resolved, relative


def _require_keys(value: Mapping[str, Any], expected: set[str], *, field: str) -> None:
    actual = set(value)
    if actual != expected:
        raise OOSLockError(
            f"{field} 키 불일치: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )


def _load_json(path: Path, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OOSLockError(f"{field} JSON을 읽을 수 없다") from exc
    if not isinstance(value, dict):
        raise OOSLockError(f"{field} 최상위는 object여야 한다")
    return value


def _full_commit(repo: Path, value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _FULL_SHA.fullmatch(value):
        raise OOSLockError(f"{field}는 full 40자리 lowercase commit SHA여야 한다")
    resolved = _run_git(
        repo, "rev-parse", "--verify", f"{value}^{{commit}}"
    ).stdout.strip()
    if resolved != value:
        raise OOSLockError(f"{field} commit이 실존하지 않거나 모호하다")
    return resolved


def _assert_ancestor(repo: Path, commit: str, main_ref: str, *, field: str) -> None:
    result = _run_git(
        repo, "merge-base", "--is-ancestor", commit, main_ref, check=False
    )
    if result.returncode != 0:
        raise OOSLockError(f"{field}가 {main_ref}에 병합되지 않았다")


def _assert_tracked_clean_on_main(
    repo: Path, relative: str, main_ref: str, *, field: str
) -> None:
    _run_git(repo, "ls-files", "--error-unmatch", "--", relative)
    for args in (
        ("diff", "--quiet", "HEAD", "--", relative),
        ("diff", "--cached", "--quiet", "HEAD", "--", relative),
    ):
        if _run_git(repo, *args, check=False).returncode != 0:
            raise OOSLockError(f"{field}에 commit되지 않은 변경이 있다")
    head_blob = _run_git(repo, "rev-parse", f"HEAD:{relative}").stdout.strip()
    main_blob = _run_git(repo, "rev-parse", f"{main_ref}:{relative}").stdout.strip()
    if head_blob != main_blob:
        raise OOSLockError(f"{field}의 현재 내용이 {main_ref}에 병합되지 않았다")


def _assert_append_only_artifact(repo: Path, relative: str, main_ref: str) -> str:
    commits = [
        line
        for line in _run_git(
            repo, "log", "--follow", "--format=%H", "--", relative
        ).stdout.splitlines()
        if line
    ]
    if len(commits) != 1:
        raise OOSLockError("unlock artifact는 최초 추가 후 수정할 수 없다(append-only)")
    artifact_commit = commits[0]
    status = _run_git(
        repo,
        "diff-tree",
        "--root",
        "--no-commit-id",
        "--name-status",
        "-r",
        artifact_commit,
        "--",
        relative,
    ).stdout.splitlines()
    if f"A\t{relative}" not in status:
        raise OOSLockError("unlock artifact 최초 Git 변경이 add가 아니다")
    _assert_ancestor(repo, artifact_commit, main_ref, field="artifact commit")
    return artifact_commit


def _lookup_hash(document: Mapping[str, Any], dotted_field: str) -> str:
    parts = dotted_field.split(".")
    if not parts or parts[-1] not in _ALLOWED_SNAPSHOT_HASH_FIELDS:
        raise OOSLockError("허용되지 않은 snapshot hash field")
    current: Any = document
    for part in parts:
        if not part or not isinstance(current, Mapping) or part not in current:
            raise OOSLockError("snapshot hash field가 manifest에 없다")
        current = current[part]
    if not isinstance(current, str) or not _CONTENT_SHA.fullmatch(current):
        raise OOSLockError("manifest snapshot hash가 64자리 lowercase SHA-256이 아니다")
    return current


def _artifact_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _gh_api(endpoint: str) -> Any:
    """GitHub API를 gh CLI로 조회한다. 인증/네트워크 실패는 항상 잠금 유지."""
    try:
        result = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OOSLockError("GitHub 독립 리뷰 증거 조회 실패") from exc
    if result.returncode != 0:
        raise OOSLockError("GitHub 독립 리뷰 증거 조회 실패")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OOSLockError("GitHub 독립 리뷰 응답이 JSON이 아니다") from exc


def _review_record_approved(
    record: Mapping[str, Any], evidence: ReviewEvidence
) -> bool:
    user = record.get("user")
    return (
        str(record.get("state", "")).upper() == "APPROVED"
        and isinstance(user, Mapping)
        and user.get("login") == evidence.reviewer
        and record.get("commit_id") == evidence.reviewed_commit
    )


def _reviewer_is_independent(
    owner: str, repo: str, number: str, evidence: ReviewEvidence
) -> bool:
    pull = _gh_api(f"repos/{owner}/{repo}/pulls/{number}")
    author = pull.get("user") if isinstance(pull, Mapping) else None
    return (
        isinstance(author, Mapping)
        and isinstance(author.get("login"), str)
        and author["login"] != evidence.reviewer
    )


def _verify_review_evidence(evidence: ReviewEvidence) -> bool:
    """GitHub PR review 또는 exact PR comment의 승인·commit 결속을 검증한다."""
    match = _GITHUB_PULL_URL.fullmatch(evidence.url)
    if match is None:
        return False
    owner = match.group("owner")
    repo = match.group("repo")
    number = match.group("number")
    fragment = match.group("fragment")
    if fragment is not None and not fragment.startswith(
        ("pullrequestreview-", "issuecomment-")
    ):
        return False
    if not _reviewer_is_independent(owner, repo, number, evidence):
        return False

    if fragment and fragment.startswith("pullrequestreview-"):
        review_id = fragment.removeprefix("pullrequestreview-")
        record = _gh_api(f"repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}")
        return isinstance(record, Mapping) and _review_record_approved(record, evidence)

    if fragment and fragment.startswith("issuecomment-"):
        comment_id = fragment.removeprefix("issuecomment-")
        record = _gh_api(f"repos/{owner}/{repo}/issues/comments/{comment_id}")
        if not isinstance(record, Mapping):
            return False
        user = record.get("user")
        body = record.get("body")
        return (
            record.get("html_url") == evidence.url
            and isinstance(user, Mapping)
            and user.get("login") == evidence.reviewer
            and isinstance(body, str)
            and _APPROVED_COMMENT_LINE.search(body) is not None
            and evidence.reviewed_commit in body
        )

    records = _gh_api(f"repos/{owner}/{repo}/pulls/{number}/reviews")
    return isinstance(records, list) and any(
        isinstance(record, Mapping) and _review_record_approved(record, evidence)
        for record in records
    )


def load_verified_oos_unlock(
    artifact_path: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> OOSUnlock:
    """Tracked artifact를 전수 대조해 검증된 ``OOSUnlock``을 반환한다.

    기록된 GitHub PR review/comment를 외부 원천과 직접 대조한다. 네트워크·인증·API 검증이
    실패하면 잠금을 유지하며, 호출자가 임의 검증기를 주입하는 우회 경로는 제공하지 않는다.
    """
    repo = _repo_root(repo_root)
    artifact, artifact_rel = _repo_file(repo, artifact_path, field="artifact_path")
    _assert_tracked_clean_on_main(
        repo, artifact_rel, _CANONICAL_MAIN_REF, field="unlock artifact"
    )
    _assert_append_only_artifact(repo, artifact_rel, _CANONICAL_MAIN_REF)
    payload = _load_json(artifact, field="unlock artifact")
    _require_keys(
        payload,
        {"schema", "track", "prereg", "implementation", "snapshot", "review"},
        field="unlock artifact",
    )
    if payload["schema"] != UNLOCK_SCHEMA:
        raise OOSLockError(f"지원하지 않는 unlock schema: {payload['schema']!r}")
    track = payload["track"]
    if not isinstance(track, str):
        raise OOSLockError("unlock track은 문자열이어야 한다")
    try:
        blind_oos_window(track)
    except ValueError as exc:
        raise OOSLockError("unlock track이 알려진 blind OOS track이 아니다") from exc

    prereg = payload["prereg"]
    implementation = payload["implementation"]
    snapshot = payload["snapshot"]
    review = payload["review"]
    for value, name in (
        (prereg, "prereg"),
        (implementation, "implementation"),
        (snapshot, "snapshot"),
        (review, "review"),
    ):
        if not isinstance(value, Mapping):
            raise OOSLockError(f"{name}은 object여야 한다")
    _require_keys(prereg, {"path", "merge_commit"}, field="prereg")
    _require_keys(implementation, {"commit"}, field="implementation")
    _require_keys(snapshot, {"manifest_path", "hash_field", "hash"}, field="snapshot")
    _require_keys(
        review, {"status", "url", "reviewer", "reviewed_commit"}, field="review"
    )

    prereg_commit = _full_commit(
        repo, prereg["merge_commit"], field="prereg.merge_commit"
    )
    implementation_commit = _full_commit(
        repo, implementation["commit"], field="implementation.commit"
    )
    _assert_ancestor(
        repo, prereg_commit, _CANONICAL_MAIN_REF, field="prereg merge commit"
    )
    _assert_ancestor(
        repo,
        implementation_commit,
        _CANONICAL_MAIN_REF,
        field="implementation commit",
    )

    prereg_path = prereg["path"]
    if not isinstance(prereg_path, str) or Path(prereg_path).is_absolute():
        raise OOSLockError("prereg.path는 저장소 상대경로 문자열이어야 한다")
    _, prereg_rel = _repo_file(repo, prereg_path, field="prereg.path")
    _assert_tracked_clean_on_main(repo, prereg_rel, _CANONICAL_MAIN_REF, field="prereg")

    manifest_path = snapshot["manifest_path"]
    hash_field = snapshot["hash_field"]
    declared_hash = snapshot["hash"]
    if (
        not isinstance(manifest_path, str)
        or Path(manifest_path).is_absolute()
        or not isinstance(hash_field, str)
    ):
        raise OOSLockError(
            "snapshot manifest_path는 저장소 상대경로, hash_field는 문자열이어야 한다"
        )
    if not isinstance(declared_hash, str) or not _CONTENT_SHA.fullmatch(declared_hash):
        raise OOSLockError("snapshot.hash는 64자리 lowercase SHA-256이어야 한다")
    manifest, manifest_rel = _repo_file(
        repo, manifest_path, field="snapshot.manifest_path"
    )
    _assert_tracked_clean_on_main(
        repo, manifest_rel, _CANONICAL_MAIN_REF, field="snapshot manifest"
    )
    if (
        _lookup_hash(_load_json(manifest, field="snapshot manifest"), hash_field)
        != declared_hash
    ):
        raise OOSLockError("snapshot hash가 manifest 정본과 일치하지 않는다")

    prereg_at_commit = _run_git(repo, "show", f"{prereg_commit}:{prereg_rel}").stdout
    if declared_hash not in prereg_at_commit:
        raise OOSLockError("snapshot hash가 prereg merge commit에 사전 고정되지 않았다")

    evidence = ReviewEvidence(
        status=review["status"],
        url=review["url"],
        reviewer=review["reviewer"],
        reviewed_commit=review["reviewed_commit"],
    )
    if evidence.status != "approved":
        raise OOSLockError("독립 리뷰 상태가 approved가 아니다")
    if not isinstance(evidence.url, str) or not _GITHUB_PULL_URL.fullmatch(
        evidence.url
    ):
        raise OOSLockError("review.url은 GitHub PR/review/comment URL이어야 한다")
    if not isinstance(evidence.reviewer, str) or not evidence.reviewer.strip():
        raise OOSLockError("review.reviewer가 비어 있다")
    if evidence.reviewed_commit != implementation_commit:
        raise OOSLockError("리뷰 대상 commit이 implementation commit과 다르다")
    if not _verify_review_evidence(evidence):
        raise OOSLockError("외부 독립 리뷰 승인 증거가 확인되지 않았다")

    return OOSUnlock._from_verified_artifact(
        track=track,
        prereg_merge_commit=prereg_commit,
        implementation_commit=implementation_commit,
        snapshot_hash=declared_hash,
        review_status=evidence.status,
        artifact_path=artifact_rel,
        artifact_sha256=_artifact_sha256(artifact),
    )
