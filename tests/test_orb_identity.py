"""ORB P3 스냅샷 identity 검증 테스트 — 이슈 #91 (4-A).

사전등록 §2: 4개 hash 중 하나라도 불일치하면 분석을 실행하지 않는다(fail-closed). raw
아티팩트가 없으면 그 항목은 NOT_RUN 이며, 나머지 3개가 전부 맞았다고 진행하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3

import pytest

from src.history.raw_snapshot import RawSnapshotStore
from src.research.orb import identity as ident
from src.research.orb.contract import ContractIntegrityError, load_contract


@pytest.fixture(scope="module")
def contract() -> dict:
    return load_contract()


def _make_snapshot_db(path: str) -> tuple[str, str]:
    """`src.history.research_snapshot` 가 이해하는 최소 candle_history 스키마의 DB 생성."""
    from src.history.store import SCHEMA
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute(
        "INSERT INTO candle_history "
        "(symbol,market,interval,ts_utc,ts_exchange,session,open,high,low,close,volume,"
        "currency,adjusted,source,created_at,updated_at) VALUES "
        "('AAA','US','1m','2024-01-02T14:30:00+00:00','2024-01-02T09:30:00-05:00','REGULAR',"
        "1,1,1,1,100,'USD',1,'toss_backfill','t','t')"
    )
    con.commit()
    con.close()
    file_sha = _sha256_file(path)
    con2 = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        from src.history.research_snapshot import content_sha256
        content_sha = content_sha256(con2)
    finally:
        con2.close()
    return file_sha, content_sha


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fake_contract(*, snapshot_file_sha: str, snapshot_content_sha: str,
                   manifest_sha: str, raw_sha: str) -> dict:
    return {
        "data_identity": {
            "snapshot_file_sha256": snapshot_file_sha,
            "snapshot_content_sha256": snapshot_content_sha,
            "manifest_data_sha256": manifest_sha,
            "raw_content_sha256": raw_sha,
        }
    }


def _make_manifest(path: str, sha: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"data_content_sha256": sha}, f)


def _make_raw_store_with_one_file(root: str, payload: dict) -> str:
    store = RawSnapshotStore(root)
    store.emit(symbol="AAA", interval="1m", before=None, requested_at="t", received_at="t",
              source="test", payload=payload)
    return store.manifest()["raw_content_sha256"]


# --------------------------------------------------------------------------
# 순서·이름 고정
# --------------------------------------------------------------------------
def test_check_order_and_names_are_fixed(tmp_path):
    fake = _fake_contract(snapshot_file_sha="a" * 64, snapshot_content_sha="b" * 64,
                          manifest_sha="c" * 64, raw_sha="d" * 64)
    result = ident.verify_snapshot_identity(
        contract=fake,
        snapshot_path=str(tmp_path / "nope.sqlite"),
        raw_root=str(tmp_path / "raw_nope"),
        manifest_path=str(tmp_path / "manifest_nope.json"),
    )
    assert [c.name for c in result.checks] == list(ident.CHECK_NAMES)
    assert ident.CHECK_NAMES == (
        "snapshot_file", "snapshot_content", "manifest_data", "raw_content")


# --------------------------------------------------------------------------
# raw 부재 → NOT_RUN
# --------------------------------------------------------------------------
def test_raw_dir_absent_yields_not_run_and_blocks_ok(tmp_path):
    snap_path = tmp_path / "snap.sqlite"
    file_sha, content_sha = _make_snapshot_db(str(snap_path))
    manifest_path = tmp_path / "manifest.json"
    _make_manifest(str(manifest_path), "manifest-sha")

    fake = _fake_contract(snapshot_file_sha=file_sha, snapshot_content_sha=content_sha,
                          manifest_sha="manifest-sha", raw_sha="whatever")
    result = ident.verify_snapshot_identity(
        contract=fake, snapshot_path=str(snap_path),
        raw_root=str(tmp_path / "raw_missing_dir"),   # 존재하지 않음
        manifest_path=str(manifest_path),
    )
    raw_check = next(c for c in result.checks if c.name == "raw_content")
    assert raw_check.status == "not_run"
    assert raw_check.actual is None
    assert result.ok is False
    assert result.verdict == "NOT_RUN"
    # identity 검증이 raw 디렉터리를 새로 만들지 않았어야 한다(read-only 검증의 부작용 금지).
    assert not os.path.isdir(str(tmp_path / "raw_missing_dir"))


def test_other_three_match_but_raw_not_run_still_fails_ok(tmp_path):
    """나머지 3개가 전부 match 여도 raw 가 not_run 이면 ok=False."""
    snap_path = tmp_path / "snap.sqlite"
    file_sha, content_sha = _make_snapshot_db(str(snap_path))
    manifest_path = tmp_path / "manifest.json"
    _make_manifest(str(manifest_path), "manifest-sha-2")

    fake = _fake_contract(snapshot_file_sha=file_sha, snapshot_content_sha=content_sha,
                          manifest_sha="manifest-sha-2", raw_sha="irrelevant")
    result = ident.verify_snapshot_identity(
        contract=fake, snapshot_path=str(snap_path),
        raw_root=str(tmp_path / "still_missing"),
        manifest_path=str(manifest_path),
    )
    by_name = {c.name: c for c in result.checks}
    assert by_name["snapshot_file"].status == "match"
    assert by_name["snapshot_content"].status == "match"
    assert by_name["manifest_data"].status == "match"
    assert by_name["raw_content"].status == "not_run"
    assert result.ok is False
    assert result.verdict == "NOT_RUN"


def test_raw_dir_present_but_empty_is_also_not_run(tmp_path):
    """디렉터리는 있는데 완료본이 0개면(§2 의 "raw 아티팩트가 없으면") 여전히 not_run 이어야 한다."""
    empty_root = tmp_path / "raw_empty"
    empty_root.mkdir()
    fake = _fake_contract(snapshot_file_sha="a" * 64, snapshot_content_sha="b" * 64,
                          manifest_sha="c" * 64, raw_sha="d" * 64)
    result = ident.verify_snapshot_identity(
        contract=fake, snapshot_path=str(tmp_path / "nope.sqlite"),
        raw_root=str(empty_root), manifest_path=str(tmp_path / "nope.json"),
    )
    raw_check = next(c for c in result.checks if c.name == "raw_content")
    assert raw_check.status == "not_run"


# --------------------------------------------------------------------------
# 임의 항목 mismatch
# --------------------------------------------------------------------------
def test_any_hash_mismatch_fails_ok(tmp_path):
    snap_path = tmp_path / "snap.sqlite"
    file_sha, content_sha = _make_snapshot_db(str(snap_path))
    manifest_path = tmp_path / "manifest.json"
    _make_manifest(str(manifest_path), "manifest-sha-3")
    raw_root = tmp_path / "raw"
    raw_sha = _make_raw_store_with_one_file(str(raw_root), {"candles": [1, 2, 3]})

    # 모두 맞춘 기준선 — ok=True 여야 한다(대조군).
    good = _fake_contract(snapshot_file_sha=file_sha, snapshot_content_sha=content_sha,
                          manifest_sha="manifest-sha-3", raw_sha=raw_sha)
    good_result = ident.verify_snapshot_identity(
        contract=good, snapshot_path=str(snap_path), raw_root=str(raw_root),
        manifest_path=str(manifest_path))
    assert good_result.ok is True
    assert good_result.verdict is None
    assert all(c.status == "match" for c in good_result.checks)

    # mutation: manifest hash 하나만 틀리게.
    bad = _fake_contract(snapshot_file_sha=file_sha, snapshot_content_sha=content_sha,
                         manifest_sha="wrong-manifest-sha", raw_sha=raw_sha)
    bad_result = ident.verify_snapshot_identity(
        contract=bad, snapshot_path=str(snap_path), raw_root=str(raw_root),
        manifest_path=str(manifest_path))
    manifest_check = next(c for c in bad_result.checks if c.name == "manifest_data")
    assert manifest_check.status == "mismatch"
    assert bad_result.ok is False
    assert bad_result.verdict is None      # 부재가 아니라 순수 불일치이므로 NOT_RUN 아님


def test_require_identity_raises_when_not_ok(tmp_path):
    fake = _fake_contract(snapshot_file_sha="a" * 64, snapshot_content_sha="b" * 64,
                          manifest_sha="c" * 64, raw_sha="d" * 64)
    result = ident.verify_snapshot_identity(
        contract=fake, snapshot_path=str(tmp_path / "nope.sqlite"),
        raw_root=str(tmp_path / "nope_raw"), manifest_path=str(tmp_path / "nope.json"))
    assert result.ok is False
    with pytest.raises(ident.IdentityMismatchError):
        ident.require_identity(result)


def test_require_identity_passes_when_ok(tmp_path):
    snap_path = tmp_path / "snap.sqlite"
    file_sha, content_sha = _make_snapshot_db(str(snap_path))
    manifest_path = tmp_path / "manifest.json"
    _make_manifest(str(manifest_path), "manifest-sha-4")
    raw_root = tmp_path / "raw"
    raw_sha = _make_raw_store_with_one_file(str(raw_root), {"candles": [9]})

    good = _fake_contract(snapshot_file_sha=file_sha, snapshot_content_sha=content_sha,
                          manifest_sha="manifest-sha-4", raw_sha=raw_sha)
    result = ident.verify_snapshot_identity(
        contract=good, snapshot_path=str(snap_path), raw_root=str(raw_root),
        manifest_path=str(manifest_path))
    ident.require_identity(result)   # 예외 없이 통과해야 한다


# --------------------------------------------------------------------------
# 읽기 실패는 부재와 구분(예외 전파)
# --------------------------------------------------------------------------
def test_corrupt_snapshot_file_propagates_instead_of_being_treated_as_absent(tmp_path):
    """스냅샷 파일이 **존재하지만** sqlite 형식이 아니면 부재로 뭉개지 말고 예외를 전파한다."""
    bad_snap = tmp_path / "corrupt.sqlite"
    bad_snap.write_bytes(b"not a sqlite file")
    fake = _fake_contract(snapshot_file_sha="a" * 64, snapshot_content_sha="b" * 64,
                          manifest_sha="c" * 64, raw_sha="d" * 64)
    with pytest.raises(sqlite3.DatabaseError):
        ident.verify_snapshot_identity(
            contract=fake, snapshot_path=str(bad_snap),
            raw_root=str(tmp_path / "raw_missing"), manifest_path=str(tmp_path / "m.json"))


def test_malformed_manifest_json_propagates(tmp_path):
    bad_manifest = tmp_path / "bad_manifest.json"
    bad_manifest.write_text("{not valid json", encoding="utf-8")
    fake = _fake_contract(snapshot_file_sha="a" * 64, snapshot_content_sha="b" * 64,
                          manifest_sha="c" * 64, raw_sha="d" * 64)
    with pytest.raises(json.JSONDecodeError):
        ident.verify_snapshot_identity(
            contract=fake, snapshot_path=str(tmp_path / "nope.sqlite"),
            raw_root=str(tmp_path / "raw_missing"), manifest_path=str(bad_manifest))


# --------------------------------------------------------------------------
# 실제 계약과 배선 확인(형 오류 없이 호출 가능한지) — 실행 환경에 raw artifact 가 없으므로
# NOT_RUN 이 되는 것이 정상이다(이 워크트리는 canonical DB/raw snapshot 을 보존하지 않는다).
# --------------------------------------------------------------------------
def test_real_contract_wiring_smoke(contract, tmp_path):
    result = ident.verify_snapshot_identity(
        contract=contract,
        snapshot_path=str(tmp_path / "does-not-exist.sqlite"),
        raw_root=str(tmp_path / "does-not-exist-raw"),
        manifest_path=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "docs", "research", "P0_US30_ORB_CLEAN_1M_MANIFEST.json"),
    )
    by_name = {c.name: c for c in result.checks}
    assert by_name["manifest_data"].status == "match"   # tracked manifest 는 저장소에 있다
    assert by_name["snapshot_file"].status == "not_run"
    assert by_name["raw_content"].status == "not_run"
    assert result.verdict == "NOT_RUN"


# --------------------------------------------------------------------------
# D4 — 계약 값 접근은 `_require` 를 거쳐 오류 종류가 `ContractIntegrityError` 로 일관돼야 한다
# (이전 판은 `contract["data_identity"]` 를 직접 접근해 누락 시 평범한 KeyError 를 냈다).
# --------------------------------------------------------------------------
def test_missing_data_identity_key_raises_contract_integrity_error(tmp_path):
    """R5-3 — `verify_snapshot_identity` 는 `_require` 를 5번(최상위 + 4개 해시 필드) 호출하므로
    `match=` 로 어느 키가 걸렸는지 고정한다."""
    with pytest.raises(ContractIntegrityError, match="누락 지점 'data_identity'"):
        ident.verify_snapshot_identity(
            contract={}, snapshot_path=str(tmp_path / "x"),
            raw_root=str(tmp_path / "y"), manifest_path=str(tmp_path / "z"))


def test_missing_individual_hash_field_raises_contract_integrity_error(tmp_path):
    with pytest.raises(ContractIntegrityError, match="snapshot_content_sha256"):
        ident.verify_snapshot_identity(
            contract={"data_identity": {"snapshot_file_sha256": "a" * 64}},  # 나머지 3개 없음
            snapshot_path=str(tmp_path / "x"),
            raw_root=str(tmp_path / "y"), manifest_path=str(tmp_path / "z"))


# --------------------------------------------------------------------------
# R4-9(lane4 I7, Low) — snapshot content 검증은 스냅샷을 읽기 전용으로 연다
# --------------------------------------------------------------------------
def test_snapshot_content_check_opens_the_database_read_only(monkeypatch, tmp_path):
    """`mode=ro` → `rw` 변조가 라운드 2·3 연속 생존했다 — 검증 대상 아티팩트를 읽기 전용으로
    연다는 성질에 테스트가 없었다. 운영 환경의 검증 대상은 7.3GB canonical 스냅샷이며, 이를
    쓰기 가능하게 여는 것은 사전등록 절대규칙("스냅샷 파일 수정 금지")과 직결된다.

    `sqlite3.connect` 를 몽키패치해 `identity.py` 가 실제로 넘기는 URI 를 가로챈다 — 소스
    문자열을 정적으로 대조하는 것보다 강하다(그 코드 경로가 실제로 실행돼야 통과한다)."""
    snap_path = tmp_path / "snap.sqlite"
    file_sha, content_sha = _make_snapshot_db(str(snap_path))

    captured_uris: list[str] = []
    real_connect = sqlite3.connect

    def spying_connect(database, *args, **kwargs):
        if isinstance(database, str) and database.startswith("file:"):
            captured_uris.append(database)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spying_connect)

    fake = _fake_contract(snapshot_file_sha=file_sha, snapshot_content_sha=content_sha,
                          manifest_sha="whatever", raw_sha="whatever")
    ident.verify_snapshot_identity(
        contract=fake, snapshot_path=str(snap_path),
        raw_root=str(tmp_path / "raw_missing"), manifest_path=str(tmp_path / "m.json"))

    assert captured_uris, "snapshot content 검증이 URI 스타일 sqlite3.connect 를 쓰지 않았다"
    assert all("mode=ro" in uri for uri in captured_uris), captured_uris
