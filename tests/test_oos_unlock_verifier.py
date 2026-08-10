"""#78 tracked OOS unlock artifact 검증 로더 회귀 테스트."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

import src.history.oos_unlock as oos_unlock_module
from src.history.oos_guard import (
    TRACK_INTRADAY,
    OOSExecutionGrant,
    OOSLockError,
    OOSUnlock,
)
from src.history.oos_unlock import (
    OOSExecutionSpec,
    ReviewEvidence,
    bind_verified_oos_execution,
    load_verified_oos_unlock,
)

SNAPSHOT_HASH = "1" * 64
REAL_REVIEW_VERIFIER = oos_unlock_module._verify_review_evidence
CONTRACT = Path("docs/research/OOS_UNLOCK_VERIFICATION_CONTRACT.md")


@pytest.fixture(autouse=True)
def _verified_review_stub(monkeypatch):
    """Git 저장소 검증 테스트는 네트워크와 분리; GitHub 대조는 별도 단위 테스트."""
    monkeypatch.setattr(
        oos_unlock_module,
        "_verify_review_evidence",
        lambda evidence: True,
    )


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@dataclass(frozen=True)
class RepoFixture:
    root: Path
    artifact: Path
    payload: dict
    prereg_commit: str
    implementation_commit: str

    def spec(self, **overrides) -> OOSExecutionSpec:
        values = {
            "strategy_id": "p0-us30-orb",
            "prereg_path": "docs/research/PREREG.md",
            "config_path": "docs/research/PREREG.json",
            "implementation_paths": ("src/strategy.py",),
            "snapshot_manifest_path": "docs/research/SNAPSHOT.json",
            "snapshot_hash_field": "data_content_sha256",
        }
        values.update(overrides)
        return OOSExecutionSpec(**values)


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _make_repo(tmp_path: Path, *, prereg_contains_hash: bool = True) -> RepoFixture:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")

    manifest = repo / "docs" / "research" / "SNAPSHOT.json"
    _write_json(manifest, {"data_content_sha256": SNAPSHOT_HASH})
    prereg = repo / "docs" / "research" / "PREREG.md"
    prereg.parent.mkdir(parents=True, exist_ok=True)
    prereg.write_text(
        f"snapshot={SNAPSHOT_HASH}\n" if prereg_contains_hash else "snapshot=pending\n",
        encoding="utf-8",
    )
    config = repo / "docs" / "research" / "PREREG.json"
    config_bytes = b'{"opening_range_minutes":15}\n'
    config.write_bytes(config_bytes)
    config_sha256 = hashlib.sha256(config_bytes).hexdigest()
    prereg_commit = _commit(repo, "prereg")

    implementation = repo / "src" / "strategy.py"
    implementation.parent.mkdir(parents=True, exist_ok=True)
    implementation.write_text("VALUE = 1\n", encoding="utf-8")
    implementation_commit = _commit(repo, "implementation")

    payload = {
        "schema": "oos-unlock/v2",
        "track": "intraday",
        "execution": {"strategy_id": "p0-us30-orb"},
        "prereg": {
            "path": "docs/research/PREREG.md",
            "merge_commit": prereg_commit,
            "config_path": "docs/research/PREREG.json",
            "config_sha256": config_sha256,
        },
        "implementation": {
            "commit": implementation_commit,
            "paths": ["src/strategy.py"],
        },
        "snapshot": {
            "manifest_path": "docs/research/SNAPSHOT.json",
            "hash_field": "data_content_sha256",
            "hash": SNAPSHOT_HASH,
        },
        "review": {
            "status": "approved",
            "url": "https://github.com/acme/repo/pull/1#issuecomment-1",
            "reviewer": "independent-reviewer",
            "reviewed_commit": implementation_commit,
        },
    }
    artifact = repo / "docs" / "research" / "OOS_UNLOCK.json"
    _write_json(artifact, payload)
    _commit(repo, "unlock artifact")
    return RepoFixture(repo, artifact, payload, prereg_commit, implementation_commit)


def _rewrite_and_amend(fixture: RepoFixture, mutate) -> None:
    payload = json.loads(json.dumps(fixture.payload))
    mutate(payload)
    _write_json(fixture.artifact, payload)
    _git(fixture.root, "add", str(fixture.artifact))
    _git(fixture.root, "commit", "--amend", "--no-edit")


def _rewrite_and_commit(fixture: RepoFixture, mutate) -> None:
    payload = json.loads(json.dumps(fixture.payload))
    mutate(payload)
    _write_json(fixture.artifact, payload)
    _commit(fixture.root, "mutate artifact")


def test_verified_artifact_returns_unlock(tmp_path):
    fixture = _make_repo(tmp_path)
    unlock = load_verified_oos_unlock(
        fixture.artifact,
        repo_root=fixture.root,
    )
    assert unlock.is_valid_for(TRACK_INTRADAY) is True
    assert unlock.strategy_id == "p0-us30-orb"
    assert unlock.prereg_merge_commit == fixture.prereg_commit
    assert unlock.implementation_commit == fixture.implementation_commit
    assert unlock.snapshot_hash == SNAPSHOT_HASH
    assert unlock.artifact_path == "docs/research/OOS_UNLOCK.json"
    assert len(unlock.artifact_sha256) == 64


def test_verified_execution_binding_returns_grant(tmp_path):
    fixture = _make_repo(tmp_path)
    unlock = load_verified_oos_unlock(fixture.artifact, repo_root=fixture.root)
    grant = bind_verified_oos_execution(unlock, fixture.spec(), repo_root=fixture.root)
    assert grant.is_valid_for(TRACK_INTRADAY, "p0-us30-orb") is True
    assert grant.strategy_id == "p0-us30-orb"
    assert len(grant.execution_fingerprint) == 64


def test_direct_execution_grant_construction_is_rejected():
    with pytest.raises(OOSLockError, match="직접 생성 금지"):
        OOSExecutionGrant(
            track="intraday",
            strategy_id="p0-us30-orb",
            artifact_sha256="a" * 64,
            execution_fingerprint="b" * 64,
        )


def test_direct_unlock_construction_is_rejected():
    with pytest.raises(OOSLockError, match="직접 생성 금지"):
        OOSUnlock(
            track="intraday",
            prereg_merge_commit="a" * 40,
            implementation_commit="b" * 40,
            snapshot_hash="c" * 64,
            review_status="approved",
        )


def test_untracked_artifact_is_rejected(tmp_path):
    fixture = _make_repo(tmp_path)
    untracked = fixture.root / "OOS_UNLOCK_COPY.json"
    _write_json(untracked, fixture.payload)
    with pytest.raises(OOSLockError, match="git 증거 불충족"):
        load_verified_oos_unlock(
            untracked,
            repo_root=fixture.root,
        )


def test_dirty_artifact_is_rejected(tmp_path):
    fixture = _make_repo(tmp_path)
    fixture.artifact.write_text("{}\n", encoding="utf-8")
    with pytest.raises(OOSLockError, match="commit되지 않은 변경"):
        load_verified_oos_unlock(
            fixture.artifact,
            repo_root=fixture.root,
        )


def test_artifact_modified_after_initial_add_is_rejected(tmp_path):
    fixture = _make_repo(tmp_path)
    _rewrite_and_commit(
        fixture, lambda payload: payload["review"].update({"reviewer": "other"})
    )
    with pytest.raises(OOSLockError, match="append-only"):
        load_verified_oos_unlock(
            fixture.artifact,
            repo_root=fixture.root,
        )


def test_artifact_not_merged_to_main_is_rejected(tmp_path):
    fixture = _make_repo(tmp_path)
    _git(fixture.root, "branch", "topic-with-artifact", "HEAD")
    _git(fixture.root, "reset", "--hard", fixture.implementation_commit)
    _git(fixture.root, "switch", "topic-with-artifact")
    with pytest.raises(OOSLockError, match="git 증거 불충족"):
        load_verified_oos_unlock(
            fixture.artifact,
            repo_root=fixture.root,
        )


@pytest.mark.parametrize("field", ["prereg", "implementation"])
def test_unmerged_evidence_commit_is_rejected(tmp_path, field):
    fixture = _make_repo(tmp_path)
    _git(fixture.root, "switch", "-c", "unmerged")
    side = fixture.root / "side.txt"
    side.write_text("side\n", encoding="utf-8")
    side_commit = _commit(fixture.root, "side")
    _git(fixture.root, "switch", "main")

    def mutate(payload):
        if field == "prereg":
            payload["prereg"]["merge_commit"] = side_commit
        else:
            payload["implementation"]["commit"] = side_commit
            payload["review"]["reviewed_commit"] = side_commit

    _rewrite_and_amend(fixture, mutate)
    with pytest.raises(OOSLockError, match="병합되지 않았다"):
        load_verified_oos_unlock(
            fixture.artifact,
            repo_root=fixture.root,
        )


def test_missing_commit_is_rejected(tmp_path):
    fixture = _make_repo(tmp_path)
    _rewrite_and_amend(
        fixture,
        lambda payload: payload["implementation"].update({"commit": "f" * 40}),
    )
    with pytest.raises(OOSLockError, match="git 증거 불충족"):
        load_verified_oos_unlock(
            fixture.artifact,
            repo_root=fixture.root,
        )


def test_snapshot_hash_must_match_manifest(tmp_path):
    fixture = _make_repo(tmp_path)
    _rewrite_and_amend(
        fixture,
        lambda payload: payload["snapshot"].update({"hash": "2" * 64}),
    )
    with pytest.raises(OOSLockError, match="manifest 정본과 일치하지 않는다"):
        load_verified_oos_unlock(
            fixture.artifact,
            repo_root=fixture.root,
        )


def test_snapshot_hash_must_be_frozen_in_prereg_commit(tmp_path):
    fixture = _make_repo(tmp_path, prereg_contains_hash=False)
    with pytest.raises(OOSLockError, match="사전 고정되지 않았다"):
        load_verified_oos_unlock(
            fixture.artifact,
            repo_root=fixture.root,
        )


def test_snapshot_hash_field_is_allowlisted(tmp_path):
    fixture = _make_repo(tmp_path)
    _rewrite_and_amend(
        fixture,
        lambda payload: payload["snapshot"].update({"hash_field": "arbitrary_hash"}),
    )
    with pytest.raises(OOSLockError, match="허용되지 않은"):
        load_verified_oos_unlock(
            fixture.artifact,
            repo_root=fixture.root,
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update({"track": ["intraday"]}), "track은 문자열"),
        (
            lambda payload: payload["prereg"].update({"path": "/tmp/PREREG.md"}),
            "저장소 상대경로",
        ),
        (
            lambda payload: payload["snapshot"].update(
                {"manifest_path": "/tmp/SNAPSHOT.json"}
            ),
            "저장소 상대경로",
        ),
        (lambda payload: payload.update({"unexpected": True}), "키 불일치"),
    ],
)
def test_artifact_schema_is_fail_closed(tmp_path, mutate, message):
    fixture = _make_repo(tmp_path)
    _rewrite_and_amend(fixture, mutate)
    with pytest.raises(OOSLockError, match=message):
        load_verified_oos_unlock(
            fixture.artifact,
            repo_root=fixture.root,
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"strategy_id": "other-strategy"},
        {"prereg_path": "docs/research/OTHER.md"},
        {"config_path": "docs/research/OTHER.json"},
        {"implementation_paths": ("src/other.py",)},
        {"snapshot_manifest_path": "docs/research/OTHER.json"},
        {"snapshot_hash_field": "raw_content_sha256"},
    ],
)
def test_execution_subject_mismatch_is_fail_closed(tmp_path, overrides):
    fixture = _make_repo(tmp_path)
    unlock = load_verified_oos_unlock(fixture.artifact, repo_root=fixture.root)
    with pytest.raises(OOSLockError, match="실행 주체가 artifact와 불일치"):
        bind_verified_oos_execution(
            unlock, fixture.spec(**overrides), repo_root=fixture.root
        )


def test_execution_config_drift_after_artifact_is_rejected(tmp_path):
    fixture = _make_repo(tmp_path)
    unlock = load_verified_oos_unlock(fixture.artifact, repo_root=fixture.root)
    config = fixture.root / "docs" / "research" / "PREREG.json"
    config.write_text('{"opening_range_minutes":5}\n', encoding="utf-8")
    _commit(fixture.root, "drift config")
    with pytest.raises(OOSLockError, match="사전등록 config와 다르다"):
        bind_verified_oos_execution(unlock, fixture.spec(), repo_root=fixture.root)


def test_execution_artifact_drift_after_load_is_rejected(tmp_path):
    fixture = _make_repo(tmp_path)
    unlock = load_verified_oos_unlock(fixture.artifact, repo_root=fixture.root)
    fixture.artifact.write_text("{}\n", encoding="utf-8")
    with pytest.raises(OOSLockError, match="commit되지 않은 변경"):
        bind_verified_oos_execution(unlock, fixture.spec(), repo_root=fixture.root)


def test_execution_implementation_drift_after_artifact_is_rejected(tmp_path):
    fixture = _make_repo(tmp_path)
    unlock = load_verified_oos_unlock(fixture.artifact, repo_root=fixture.root)
    implementation = fixture.root / "src" / "strategy.py"
    implementation.write_text("VALUE = 2\n", encoding="utf-8")
    _commit(fixture.root, "drift implementation")
    with pytest.raises(OOSLockError, match="독립 리뷰 commit과 다르다"):
        bind_verified_oos_execution(unlock, fixture.spec(), repo_root=fixture.root)


def test_execution_snapshot_drift_after_artifact_is_rejected(tmp_path):
    fixture = _make_repo(tmp_path)
    unlock = load_verified_oos_unlock(fixture.artifact, repo_root=fixture.root)
    manifest = fixture.root / "docs" / "research" / "SNAPSHOT.json"
    _write_json(manifest, {"data_content_sha256": "2" * 64})
    _commit(fixture.root, "drift snapshot")
    with pytest.raises(
        OOSLockError, match="snapshot manifest가 unlock artifact와 다르다"
    ):
        bind_verified_oos_execution(unlock, fixture.spec(), repo_root=fixture.root)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["execution"].update({"strategy_id": ""}),
            "strategy_id",
        ),
        (
            lambda payload: payload["prereg"].update({"config_sha256": "bad"}),
            "config_sha256",
        ),
        (
            lambda payload: payload["implementation"].update({"paths": []}),
            "비어 있지 않은",
        ),
        (
            lambda payload: payload["implementation"].update(
                {"paths": ["docs/research/PREREG.md"]}
            ),
            "Python 파일",
        ),
    ],
)
def test_execution_identity_schema_is_fail_closed(tmp_path, mutate, message):
    fixture = _make_repo(tmp_path)
    _rewrite_and_amend(fixture, mutate)
    with pytest.raises(OOSLockError, match=message):
        load_verified_oos_unlock(fixture.artifact, repo_root=fixture.root)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload["review"].update({"status": "pending"}), "approved"),
        (
            lambda payload: payload["review"].update({"reviewed_commit": "a" * 40}),
            "implementation commit과 다르다",
        ),
        (
            lambda payload: payload["review"].update(
                {"url": "https://example.com/review"}
            ),
            "GitHub",
        ),
    ],
)
def test_review_metadata_is_fail_closed(tmp_path, mutate, message):
    fixture = _make_repo(tmp_path)
    _rewrite_and_amend(fixture, mutate)
    with pytest.raises(OOSLockError, match=message):
        load_verified_oos_unlock(
            fixture.artifact,
            repo_root=fixture.root,
        )


def test_external_review_verifier_must_approve(tmp_path, monkeypatch):
    fixture = _make_repo(tmp_path)
    monkeypatch.setattr(
        oos_unlock_module,
        "_verify_review_evidence",
        lambda evidence: False,
    )
    with pytest.raises(OOSLockError, match="확인되지 않았다"):
        load_verified_oos_unlock(
            fixture.artifact,
            repo_root=fixture.root,
        )


def test_external_review_verifier_error_is_fail_closed(tmp_path, monkeypatch):
    fixture = _make_repo(tmp_path)

    def broken(evidence):
        del evidence
        raise OOSLockError("GitHub 독립 리뷰 증거 조회 실패")

    monkeypatch.setattr(oos_unlock_module, "_verify_review_evidence", broken)
    with pytest.raises(OOSLockError, match="조회 실패"):
        load_verified_oos_unlock(
            fixture.artifact,
            repo_root=fixture.root,
        )


def test_artifact_path_cannot_escape_repository(tmp_path):
    fixture = _make_repo(tmp_path)
    outside = tmp_path / "outside.json"
    _write_json(outside, fixture.payload)
    with pytest.raises(OOSLockError, match="저장소 밖"):
        load_verified_oos_unlock(
            outside,
            repo_root=fixture.root,
        )


def test_artifact_symlink_is_rejected(tmp_path):
    fixture = _make_repo(tmp_path)
    alias = fixture.root / "OOS_UNLOCK_ALIAS.json"
    alias.symlink_to(fixture.artifact)
    with pytest.raises(OOSLockError, match="symlink"):
        load_verified_oos_unlock(
            alias,
            repo_root=fixture.root,
        )


def test_artifact_path_with_intermediate_symlink_is_rejected(tmp_path):
    fixture = _make_repo(tmp_path)
    alias_dir = fixture.root / "research-alias"
    alias_dir.symlink_to(fixture.artifact.parent, target_is_directory=True)
    with pytest.raises(OOSLockError, match="symlink"):
        load_verified_oos_unlock(
            alias_dir / fixture.artifact.name,
            repo_root=fixture.root,
        )


def test_repo_discovery_failure_is_fail_closed(monkeypatch):
    def broken(*args, **kwargs):
        del args, kwargs
        raise FileNotFoundError("git missing")

    monkeypatch.setattr(oos_unlock_module.subprocess, "run", broken)
    with pytest.raises(OOSLockError, match="잠금을 유지"):
        load_verified_oos_unlock("docs/research/OOS_UNLOCK.json")


@pytest.mark.parametrize(
    "record",
    [
        {
            "state": "CHANGES_REQUESTED",
            "user": {"login": "reviewer"},
            "commit_id": "b" * 40,
        },
        {"state": "APPROVED", "user": {"login": "other"}, "commit_id": "b" * 40},
        {"state": "APPROVED", "user": {"login": "reviewer"}, "commit_id": "c" * 40},
    ],
)
def test_github_formal_review_rejects_nonmatching_evidence(monkeypatch, record):
    evidence = ReviewEvidence(
        status="approved",
        url="https://github.com/acme/repo/pull/7#pullrequestreview-42",
        reviewer="reviewer",
        reviewed_commit="b" * 40,
    )

    def api(endpoint):
        if endpoint == "repos/acme/repo/pulls/7":
            return {"user": {"login": "author"}}
        return record

    monkeypatch.setattr(oos_unlock_module, "_gh_api", api)
    assert REAL_REVIEW_VERIFIER(evidence) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/repo/pull/7#unknown-42",
        "https://github.com/acme/repo/issues/7#issuecomment-42",
        "https://example.com/acme/repo/pull/7",
    ],
)
def test_github_review_rejects_unsupported_urls(monkeypatch, url):
    evidence = ReviewEvidence(
        status="approved",
        url=url,
        reviewer="reviewer",
        reviewed_commit="b" * 40,
    )
    monkeypatch.setattr(
        oos_unlock_module,
        "_gh_api",
        lambda endpoint: pytest.fail(f"unsupported URL made API call: {endpoint}"),
    )
    assert REAL_REVIEW_VERIFIER(evidence) is False


def test_github_issue_comment_binds_approval_to_exact_commit(monkeypatch):
    evidence = ReviewEvidence(
        status="approved",
        url="https://github.com/acme/repo/pull/7#issuecomment-99",
        reviewer="reviewer",
        reviewed_commit="a" * 40,
    )

    def api(endpoint):
        if endpoint == "repos/acme/repo/pulls/7":
            return {"user": {"login": "author"}}
        return {
            "html_url": evidence.url,
            "user": {"login": "reviewer"},
            "body": f"VERDICT: APPROVE\nreviewed_commit={evidence.reviewed_commit}",
        }

    monkeypatch.setattr(oos_unlock_module, "_gh_api", api)
    assert REAL_REVIEW_VERIFIER(evidence) is True


def test_github_issue_comment_without_commit_binding_is_rejected(monkeypatch):
    evidence = ReviewEvidence(
        status="approved",
        url="https://github.com/acme/repo/pull/7#issuecomment-99",
        reviewer="reviewer",
        reviewed_commit="a" * 40,
    )

    def api(endpoint):
        if endpoint == "repos/acme/repo/pulls/7":
            return {"user": {"login": "author"}}
        return {
            "html_url": evidence.url,
            "user": {"login": "reviewer"},
            "body": "VERDICT: APPROVE",
        }

    monkeypatch.setattr(oos_unlock_module, "_gh_api", api)
    assert REAL_REVIEW_VERIFIER(evidence) is False


def test_github_negated_issue_comment_approval_is_rejected(monkeypatch):
    evidence = ReviewEvidence(
        status="approved",
        url="https://github.com/acme/repo/pull/7#issuecomment-99",
        reviewer="reviewer",
        reviewed_commit="a" * 40,
    )

    def api(endpoint):
        if endpoint == "repos/acme/repo/pulls/7":
            return {"user": {"login": "author"}}
        return {
            "html_url": evidence.url,
            "user": {"login": "reviewer"},
            "body": f"VERDICT: NOT APPROVED\nreviewed_commit={evidence.reviewed_commit}",
        }

    monkeypatch.setattr(oos_unlock_module, "_gh_api", api)
    assert REAL_REVIEW_VERIFIER(evidence) is False


def test_github_review_by_pull_author_is_not_independent(monkeypatch):
    evidence = ReviewEvidence(
        status="approved",
        url="https://github.com/acme/repo/pull/7#issuecomment-99",
        reviewer="author",
        reviewed_commit="a" * 40,
    )
    monkeypatch.setattr(
        oos_unlock_module,
        "_gh_api",
        lambda endpoint: {"user": {"login": "author"}},
    )
    assert REAL_REVIEW_VERIFIER(evidence) is False


def test_github_formal_review_binds_approval_to_exact_commit(monkeypatch):
    evidence = ReviewEvidence(
        status="approved",
        url="https://github.com/acme/repo/pull/7#pullrequestreview-42",
        reviewer="reviewer",
        reviewed_commit="b" * 40,
    )

    def api(endpoint):
        if endpoint == "repos/acme/repo/pulls/7":
            return {"user": {"login": "author"}}
        return {
            "state": "APPROVED",
            "user": {"login": "reviewer"},
            "commit_id": evidence.reviewed_commit,
        }

    monkeypatch.setattr(oos_unlock_module, "_gh_api", api)
    assert REAL_REVIEW_VERIFIER(evidence) is True


def test_github_plain_pr_requires_matching_formal_review(monkeypatch):
    evidence = ReviewEvidence(
        status="approved",
        url="https://github.com/acme/repo/pull/7",
        reviewer="reviewer",
        reviewed_commit="c" * 40,
    )

    def api(endpoint):
        if endpoint == "repos/acme/repo/pulls/7":
            return {"user": {"login": "author"}}
        return [
            {
                "state": "APPROVED",
                "user": {"login": "reviewer"},
                "commit_id": evidence.reviewed_commit,
            }
        ]

    monkeypatch.setattr(oos_unlock_module, "_gh_api", api)
    assert REAL_REVIEW_VERIFIER(evidence) is True


def test_contract_documents_fail_closed_scope_without_creating_artifact():
    text = CONTRACT.read_text(encoding="utf-8")
    assert '"schema": "oos-unlock/v2"' in text
    assert "OOSExecutionGrant" in text
    assert "bind_verified_oos_execution" in text
    assert "GitHub API·인증·네트워크·JSON" in text
    assert "lambda True" in text
    assert "실 unlock artifact를 만들지 않으며 OOS도" in text
    assert not any(Path("docs/research").glob("OOS_UNLOCK_*.json"))
