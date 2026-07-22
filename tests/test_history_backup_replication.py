"""scripts/replicate_history_backup.py 회귀 테스트 (#73).

외부 저장소 복제 CLI 의 무결성/안전 불변식을 고정한다:
정상 snapshot, WAL 일관성, atomic rename, overwrite 거부, 동일/저장소내부/
동일device 거부, 손상 DB 시 SUCCESS 금지, 실패 시 성공본 보존, restore smoke,
receipt 결정성·시크릿 비노출, dry-run 무생성.

모두 tmp_path 위에서 동작하므로 동일 device 다. 정상 경로 테스트는
allow_same_device=True 로 그 축을 우회하고, 동일 device 거부는 별도로 검증한다.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

import scripts.replicate_history_backup as rep


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
_SCHEMA = (
    "CREATE TABLE candle_history ("
    " symbol TEXT NOT NULL, market TEXT NOT NULL, interval TEXT NOT NULL,"
    " ts_utc TEXT NOT NULL, close REAL NOT NULL,"
    " PRIMARY KEY (symbol, interval, ts_utc))"
)


def _rows():
    """1d(2 심볼)·1m(1 심볼) 소규모 캔들 집합."""
    rows = []
    for sym in ("AAA", "BBB"):
        for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
            rows.append((sym, "US", "1d", f"{day}T05:00:00+00:00", 1.5))
    for i in range(4):
        rows.append(("AAA", "US", "1m",
                     f"2026-01-01T09:{i:02d}:00+00:00", 2.0 + i))
    return rows


def _make_db(path, rows, journal_mode="delete"):
    con = sqlite3.connect(path)
    try:
        con.execute(f"PRAGMA journal_mode={journal_mode}")
        con.execute(_SCHEMA)
        con.executemany(
            "INSERT INTO candle_history VALUES (?,?,?,?,?)", rows
        )
        con.commit()
    finally:
        con.close()


def _mk_source(tmp_path, name="market_history.sqlite", journal_mode="delete"):
    src = str(tmp_path / name)
    _make_db(src, _rows(), journal_mode=journal_mode)
    return src


def _replicate(src, target_dir, **over):
    kw = dict(
        source_db=src,
        target_dir=str(target_dir),
        replica_name="replica.sqlite",
        repo_root=None,
        allow_same_device=True,
        dry_run=False,
        smoke_dir=str(target_dir),
    )
    kw.update(over)
    return rep.replicate(**kw)


# --------------------------------------------------------------------------- #
# 1) 정상 snapshot 복제: SHA-256 · integrity · 행수 일치
# --------------------------------------------------------------------------- #
def test_normal_replication_matches(tmp_path):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()

    result = _replicate(src, tgt)

    assert result.status == "success"
    final = os.path.join(str(tgt), "replica.sqlite")
    assert os.path.isfile(final)
    # snapshot(.partial) SHA == 최종 복제본 SHA
    assert result.snapshot_sha256 == result.replica_sha256
    assert result.snapshot_sha256 == rep.compute_sha256(final)
    # 의미 일치(행수/symbol/기간/integrity)
    ok, reason = rep.semantic_match(
        result.source_inventory, result.replica_inventory)
    assert ok, reason
    assert result.source_inventory["integrity"] == "ok"
    assert result.replica_inventory["integrity"] == "ok"
    assert result.source_inventory["total_rows"] == 10
    assert result.source_inventory["distinct_symbols"] == 2
    assert result.source_inventory["intervals"]["1d"]["rows"] == 6
    assert result.source_inventory["intervals"]["1m"]["rows"] == 4
    # restore smoke 통과
    assert result.restore_smoke["status"] == "ok"
    # partial 잔여 없음
    assert not os.path.exists(final + ".partial")


# --------------------------------------------------------------------------- #
# 2) WAL 상태 fixture 에서도 일관 snapshot (원본 미변경)
# --------------------------------------------------------------------------- #
def test_wal_source_consistent_snapshot(tmp_path):
    src = str(tmp_path / "wal_history.sqlite")
    # 라이브 writer 연결을 유지해 -wal 사이드카(미체크포인트 커밋)를 남긴다.
    # (마지막 연결을 닫으면 close 시 auto-checkpoint 로 -wal 이 병합/제거된다.)
    con = sqlite3.connect(src)
    con.execute("PRAGMA journal_mode=wal")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute(_SCHEMA)
    con.executemany("INSERT INTO candle_history VALUES (?,?,?,?,?)", _rows())
    con.commit()
    con.execute("INSERT INTO candle_history VALUES (?,?,?,?,?)",
                ("CCC", "US", "1d", "2026-02-01T05:00:00+00:00", 9.9))
    con.commit()
    try:
        before_sha = rep.compute_sha256(src)
        assert os.path.exists(src + "-wal")  # 사이드카 존재 = WAL 경로 강제
        assert os.path.getsize(src + "-wal") > 0

        tgt = tmp_path / "ext"
        tgt.mkdir()
        result = _replicate(src, tgt)

        assert result.status == "success"
        # WAL 의 CCC 행이 복제본에 포함되어야 한다(단순 cp 라면 누락).
        assert result.replica_inventory["distinct_symbols"] == 3
        assert result.replica_inventory["intervals"]["1d"]["rows"] == 7
        ok, reason = rep.semantic_match(
            result.source_inventory, result.replica_inventory)
        assert ok, reason
        # 원본 파일은 절대 변경되지 않았다(checkpoint/쓰기 없음).
        assert rep.compute_sha256(src) == before_sha
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# 3) target 임시파일 → atomic rename (rename 이 마지막 승격 단계)
# --------------------------------------------------------------------------- #
def test_atomic_rename_partial(tmp_path, monkeypatch):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()
    final = os.path.join(str(tgt), "replica.sqlite")

    # os.rename 이 실패하면 최종본은 생기지 않고 partial 도 정리되어야 한다.
    def boom(a, b):
        raise OSError("rename blocked")

    monkeypatch.setattr(rep.os, "rename", boom)
    with pytest.raises(OSError):
        _replicate(src, tgt)
    assert not os.path.exists(final)
    assert not os.path.exists(final + ".partial")


def test_atomic_rename_success_leaves_no_partial(tmp_path):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()
    result = _replicate(src, tgt)
    final = os.path.join(str(tgt), "replica.sqlite")
    assert result.status == "success"
    assert os.path.isfile(final)
    assert not os.path.exists(final + ".partial")


# --------------------------------------------------------------------------- #
# 4) 기존 성공본 overwrite 거부
# --------------------------------------------------------------------------- #
def test_refuse_overwrite_existing(tmp_path):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()
    final = tgt / "replica.sqlite"
    final.write_bytes(b"PRIOR-REPLICA-DO-NOT-TOUCH")
    before = final.read_bytes()

    with pytest.raises(rep.SafetyError):
        _replicate(src, tgt)
    # 기존 파일이 그대로 보존됨
    assert final.read_bytes() == before


# --------------------------------------------------------------------------- #
# 5) source 와 target 동일 경로/동일 파일 거부
# --------------------------------------------------------------------------- #
def test_reject_same_file(tmp_path):
    src = _mk_source(tmp_path, name="market_history.sqlite")
    # target-dir = 원본 디렉터리, replica 이름 = 원본 파일명 → 동일 파일
    with pytest.raises(rep.SafetyError):
        _replicate(src, tmp_path, replica_name="market_history.sqlite")


def test_reject_same_source_dir(tmp_path):
    src = _mk_source(tmp_path)
    with pytest.raises(rep.SafetyError):
        _replicate(src, tmp_path, replica_name="other.sqlite")


# --------------------------------------------------------------------------- #
# 6) 저장소 내부 target 거부
# --------------------------------------------------------------------------- #
def test_reject_repo_internal_target(tmp_path):
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    src = str(repo / "data" / "market_history.sqlite")
    _make_db(src, _rows())
    inside = repo / "backups"
    inside.mkdir()
    with pytest.raises(rep.SafetyError):
        _replicate(src, inside, repo_root=str(repo))


# --------------------------------------------------------------------------- #
# 동일 device 거부 (외부 저장소가 아님) — 명시 opt-in 없을 때
# --------------------------------------------------------------------------- #
def test_reject_same_device_without_flag(tmp_path):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()
    with pytest.raises(rep.SafetyError):
        _replicate(src, tgt, allow_same_device=False)


# --------------------------------------------------------------------------- #
# path traversal replica 이름 거부
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["../evil.sqlite", "sub/evil.sqlite", ".."])
def test_reject_path_traversal_name(tmp_path, bad):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()
    with pytest.raises(rep.SafetyError):
        _replicate(src, tgt, replica_name=bad)


# --------------------------------------------------------------------------- #
# 7) 손상 DB / integrity 실패 시 SUCCESS receipt 생성 금지
# --------------------------------------------------------------------------- #
def test_corrupt_source_no_success(tmp_path):
    src = str(tmp_path / "garbage.sqlite")
    with open(src, "wb") as fh:
        fh.write(os.urandom(4096))  # sqlite 가 아님
    tgt = tmp_path / "ext"
    tgt.mkdir()
    receipt = str(tmp_path / "receipt.json")
    code = rep.main([
        "--source-db", src, "--target-dir", str(tgt),
        "--target-id", "t", "--allow-same-device",
        "--receipt", receipt, "--smoke-dir", str(tgt),
    ])
    # 열 수 없는 손상 원본은 verify 실패로 분류(EXIT_VERIFY).
    assert code == rep.EXIT_VERIFY
    # receipt 가 기록됐더라도 status 는 절대 success 가 아니어야 한다.
    with open(receipt, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["status"] != "success"
    # 복제본 파일이 남지 않는다.
    assert not any(p.endswith(".sqlite") and "garbage" not in p
                   for p in os.listdir(str(tgt)))


def test_integrity_not_ok_no_success(tmp_path, monkeypatch):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()

    real = rep.db_inventory

    def fake_inv(path):
        inv = real(path)
        inv["integrity"] = "malformed"  # integrity 실패 시뮬레이션
        return inv

    monkeypatch.setattr(rep, "db_inventory", fake_inv)
    with pytest.raises(rep.VerificationError):
        _replicate(src, tgt)
    # 어떤 복제본도 생성되지 않았다.
    assert os.listdir(str(tgt)) == []


# --------------------------------------------------------------------------- #
# 8) 복제 중 실패 시 기존 성공본 보존 (+ 실패본 미잔존)
# --------------------------------------------------------------------------- #
def test_failure_preserves_prior_replica(tmp_path, monkeypatch):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()
    # 이전 성공 복제본(다른 이름)
    prior = tgt / "replica.prior.sqlite"
    prior.write_bytes(b"GOOD-PRIOR")
    prior_sha = rep.compute_sha256(str(prior))

    # rename 이후 단계인 restore smoke 를 실패시켜 rename 후 실패를 유발
    monkeypatch.setattr(
        rep, "restore_smoke_test",
        lambda *a, **k: {"status": "failed", "reason": "forced"},
    )
    with pytest.raises(rep.VerificationError):
        _replicate(src, tgt, replica_name="replica.new.sqlite")

    # 이전 성공본은 그대로
    assert prior.exists()
    assert rep.compute_sha256(str(prior)) == prior_sha
    # 실패한 새 복제본은 성공본처럼 남지 않는다(final·partial 모두 제거)
    assert not (tgt / "replica.new.sqlite").exists()
    assert not (tgt / "replica.new.sqlite.partial").exists()


# --------------------------------------------------------------------------- #
# 9) restore smoke 검증 (실제 복원 후 read-only 재검증, canonical 위치 미사용)
# --------------------------------------------------------------------------- #
def test_restore_smoke_verifies(tmp_path):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()
    smoke = tmp_path / "smoke"
    smoke.mkdir()
    result = _replicate(src, tgt, smoke_dir=str(smoke))
    assert result.restore_smoke["status"] == "ok"
    assert result.restore_smoke["sha256_match"] is True
    assert result.restore_smoke["integrity"] == "ok"
    # 임시 복원 파일은 정리되어 남지 않는다.
    assert os.listdir(str(smoke)) == []


# --------------------------------------------------------------------------- #
# 10) receipt 결정성 + 시크릿/절대경로 비노출
# --------------------------------------------------------------------------- #
def test_sanitized_receipt_deterministic_no_secrets(tmp_path):
    src = _mk_source(tmp_path)
    t1 = tmp_path / "ext1"
    t2 = tmp_path / "ext2"
    t1.mkdir()
    t2.mkdir()
    r1 = str(tmp_path / "san1.json")
    r2 = str(tmp_path / "san2.json")
    common = [
        "--source-db", src, "--target-id", "ext-ssd",
        "--target-kind", "external_disk", "--allow-same-device",
        "--replica-name", "replica.sqlite",
        "--as-of-utc", "20260723T000000Z",
        "--source-commit", "deadbeef",
    ]
    assert rep.main(common + ["--target-dir", str(t1),
                              "--smoke-dir", str(t1),
                              "--sanitized-receipt", r1]) == rep.EXIT_OK
    assert rep.main(common + ["--target-dir", str(t2),
                              "--smoke-dir", str(t2),
                              "--sanitized-receipt", r2]) == rep.EXIT_OK

    s1 = open(r1, encoding="utf-8").read()
    s2 = open(r2, encoding="utf-8").read()
    # 결정성: 두 sanitized receipt 는 byte 동일
    assert s1 == s2
    data = json.loads(s1)
    assert data["status"] == "success"
    assert data["replica"]["sha256_match"] is True
    # 절대경로/홈경로 비노출
    assert str(tmp_path) not in s1
    assert os.path.expanduser("~") not in s1
    assert "abs_path" not in s1
    assert "target_dir" not in s1
    # 시크릿 유사 키 비노출
    for bad in ("token", "secret", "password", "credential", "access_key"):
        assert bad not in s1.lower()


def test_detailed_receipt_has_abs_paths(tmp_path):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()
    receipt = str(tmp_path / "detail.json")
    assert rep.main([
        "--source-db", src, "--target-dir", str(tgt),
        "--target-id", "t", "--target-kind", "external_disk",
        "--allow-same-device", "--replica-name", "replica.sqlite",
        "--as-of-utc", "20260723T000000Z", "--smoke-dir", str(tgt),
        "--receipt", receipt,
    ]) == rep.EXIT_OK
    data = json.load(open(receipt, encoding="utf-8"))
    assert data["status"] == "success"
    assert data["replica"]["abs_path"].endswith("replica.sqlite")
    assert os.path.isabs(data["source"]["abs_path"])


# --------------------------------------------------------------------------- #
# 11) dry-run 은 파일을 생성하지 않음
# --------------------------------------------------------------------------- #
def test_dry_run_creates_no_files(tmp_path):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()
    result = _replicate(src, tgt, dry_run=True)
    assert result.status == "dry-run"
    assert os.listdir(str(tgt)) == []
    # 원본 무결성 지문은 dry-run 에서도 수집된다.
    assert result.source_inventory["integrity"] == "ok"


def test_dry_run_via_cli_no_files(tmp_path):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()
    code = rep.main([
        "--source-db", src, "--target-dir", str(tgt),
        "--target-id", "t", "--allow-same-device", "--dry-run",
        "--replica-name", "replica.sqlite",
    ])
    assert code == rep.EXIT_OK
    assert os.listdir(str(tgt)) == []


# --------------------------------------------------------------------------- #
# retention: 기본 OFF · 명시 시에만 과거본 정리 · 방금 만든 본 보호
# --------------------------------------------------------------------------- #
def test_retention_off_by_default(tmp_path):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()
    # 과거 복제본 2개(같은 stem prefix)
    (tgt / "market_history.replica.20260101T000000Z.sqlite").write_bytes(b"a")
    (tgt / "market_history.replica.20260102T000000Z.sqlite").write_bytes(b"b")
    before = set(os.listdir(str(tgt)))
    rep.main([
        "--source-db", src, "--target-dir", str(tgt),
        "--target-id", "t", "--allow-same-device", "--smoke-dir", str(tgt),
        "--replica-name", "market_history.replica.20260103T000000Z.sqlite",
        "--as-of-utc", "20260103T000000Z",
    ])
    after = set(os.listdir(str(tgt)))
    # 과거본이 그대로 유지(삭제 없음)
    assert before.issubset(after)


def test_retention_deletes_old_when_enabled(tmp_path):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()
    (tgt / "market_history.replica.20260101T000000Z.sqlite").write_bytes(b"a")
    (tgt / "market_history.replica.20260102T000000Z.sqlite").write_bytes(b"b")
    rep.main([
        "--source-db", src, "--target-dir", str(tgt),
        "--target-id", "t", "--allow-same-device", "--smoke-dir", str(tgt),
        "--replica-name", "market_history.replica.20260103T000000Z.sqlite",
        "--as-of-utc", "20260103T000000Z", "--retention-count", "2",
    ])
    names = sorted(n for n in os.listdir(str(tgt)) if n.endswith(".sqlite"))
    # keep=2 → 방금 만든 최신 + 직전 1개 = 2개, 가장 오래된 것 삭제
    assert "market_history.replica.20260101T000000Z.sqlite" not in names
    assert "market_history.replica.20260103T000000Z.sqlite" in names
    assert len(names) == 2


def test_retention_ignores_unrelated_sqlite(tmp_path):
    # 복제본 네이밍 패턴이 아닌 파일은 stem 접두가 같아도 삭제하지 않는다.
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()
    unrelated = tgt / "market_history_manual_export.sqlite"
    unrelated.write_bytes(b"KEEP-ME")
    (tgt / "market_history.replica.20260101T000000Z.sqlite").write_bytes(b"a")
    rep.main([
        "--source-db", src, "--target-dir", str(tgt),
        "--target-id", "t", "--allow-same-device", "--smoke-dir", str(tgt),
        "--replica-name", "market_history.replica.20260103T000000Z.sqlite",
        "--as-of-utc", "20260103T000000Z", "--retention-count", "1",
    ])
    # keep=1 → 과거 replica 는 삭제, 패턴 밖 수동 export 는 보존
    assert unrelated.exists()
    assert unrelated.read_bytes() == b"KEEP-ME"
    assert not (tgt / "market_history.replica.20260101T000000Z.sqlite").exists()


# --------------------------------------------------------------------------- #
# 실패 경로 sanitized receipt 도 절대경로/홈경로를 새지 않는다 (reason 필드)
# --------------------------------------------------------------------------- #
def test_failure_sanitized_receipt_no_abs_path(tmp_path):
    src = _mk_source(tmp_path)
    tgt = tmp_path / "ext"
    tgt.mkdir()
    # 기존 성공본 → overwrite 거부 실패 경로 유발(예외 메시지에 final_path 포함)
    (tgt / "replica.sqlite").write_bytes(b"PRIOR")
    san = str(tmp_path / "san_fail.json")
    code = rep.main([
        "--source-db", src, "--target-dir", str(tgt),
        "--target-id", "t", "--allow-same-device",
        "--replica-name", "replica.sqlite",
        "--sanitized-receipt", san, "--smoke-dir", str(tgt),
    ])
    assert code == rep.EXIT_SAFETY
    s = open(san, encoding="utf-8").read()
    # 실패 사유에 절대경로가 있었더라도 sanitized 에는 새지 않아야 한다.
    assert str(tmp_path) not in s
    assert os.path.expanduser("~") not in s
    assert "abs_path" not in s
    data = json.loads(s)
    assert data["status"] == "failed"
    assert data["reason"] in {"safety", "verify", "error", "failed"}
    assert "/" not in str(data["reason"])
