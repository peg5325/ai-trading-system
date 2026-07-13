"""Session Isolation — Identity + Startup Guard 테스트 (v2.1 Phase 1 · PR 1-A).

매매 로직 무관 startup 게이트. 실주문/AI 호출 없음. 결정론적(주입식 시각/PID 판정).

핵심 계약(계약서 docs/DESIGN_DELTA_V2_1.md §2-1, §3-5):
- fresh 실행이 기존 namespace/DB 를 재사용하면 시작 전에 fail-fast.
- resume 은 모든 identity/provenance 필드가 일치할 때만 허용.
- 같은 namespace 동시 실행은 두 번째 프로세스 거부(single-writer lock).
- guard 실패 시 candle/journal/metrics DB 에 신규 쓰기 0.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.runtime.config import RuntimeConfig
from src.runtime.isolation import (
    BROKER_PAPER,
    SESSION_SCHEMA_VERSION,
    GuardResult,
    IdentityMismatchError,
    LockHeldError,
    ManifestReadError,
    MissingManifestError,
    NamespaceCollisionError,
    SessionIdentity,
    SessionLock,
    SessionManifest,
    StaleLockError,
    StartupGuardError,
    canonical_config_hash,
    capture_identity,
    infer_market_currency,
    producer_commit,
    read_manifest,
    resolve_session_paths,
    startup_guard,
)


# ---- helpers ----
def _cfg(tmp_path, **over):
    base = dict(
        symbols=["SOXL"], initial_capital=2000.0,
        journal_db=str(tmp_path / "journal.sqlite"),
        candle_db=str(tmp_path / "market.sqlite"),
        metrics_db=str(tmp_path / "metrics.sqlite"),
        session_id="007", run_id="007", start_mode="FRESH",
        market="US", currency="USD", ai_provider="fake")
    base.update(over)
    return RuntimeConfig(**base)


def _identity(**over):
    base = dict(
        session_id="007", run_id="007", parent_run_id=None, start_mode="FRESH",
        config_hash="deadbeef", producer_commit="abc123", schema_version=SESSION_SCHEMA_VERSION,
        market="US", currency="USD", initial_equity=2000.0, broker_mode=BROKER_PAPER,
        ai_provider="fake")
    base.update(over)
    return SessionIdentity(**base)


def _paths(tmp_path):
    return {
        "manifest_path": str(tmp_path / "session_manifest.json"),
        "lock_path": str(tmp_path / "session.lock"),
        "db_paths": [str(tmp_path / "journal.sqlite"),
                     str(tmp_path / "market.sqlite"),
                     str(tmp_path / "metrics.sqlite")],
    }


_ALIVE = lambda pid, host: True
_DEAD = lambda pid, host: False
_CLOCK = lambda: "2026-07-14T00:00:00Z"


# ---- infer market/currency ----
def test_infer_market_currency_us():
    assert infer_market_currency(["SOXL", "QQQ"]) == ("US", "USD")


def test_infer_market_currency_kr():
    # 6자리 숫자 티커 → KRX
    assert infer_market_currency(["005930", "000660"]) == ("KR", "KRW")


# ---- config hash ----
def test_config_hash_deterministic(tmp_path):
    c1 = _cfg(tmp_path)
    c2 = _cfg(tmp_path)
    assert canonical_config_hash(c1) == canonical_config_hash(c2)
    assert len(canonical_config_hash(c1)) == 64      # sha256 hex


def test_config_hash_sensitive_to_trading_field(tmp_path):
    base = _cfg(tmp_path)
    changed = _cfg(tmp_path, max_positions=base.max_positions + 1)
    assert canonical_config_hash(base) != canonical_config_hash(changed)


def test_config_hash_ignores_ops_paths(tmp_path):
    # 경로/로그 등 운영 필드는 거래 정체성이 아니므로 hash 불변
    base = _cfg(tmp_path)
    moved = _cfg(tmp_path, log_dir="other_logs", status_path="x.json",
                 reports_dir="other_reports")
    assert canonical_config_hash(base) == canonical_config_hash(moved)


# ---- capture identity ----
def test_capture_identity_from_config(tmp_path):
    cfg = _cfg(tmp_path, ai_provider="fake")
    idn = capture_identity(cfg, commit="c0ffee")
    assert idn.session_id == "007" and idn.run_id == "007" and idn.start_mode == "FRESH"
    assert idn.market == "US" and idn.currency == "USD"
    assert idn.initial_equity == 2000.0
    assert idn.broker_mode == BROKER_PAPER
    assert idn.ai_provider == "fake"
    assert idn.producer_commit == "c0ffee"
    assert idn.schema_version == SESSION_SCHEMA_VERSION
    assert idn.config_hash == canonical_config_hash(cfg)


def test_capture_identity_defaults_run_id_to_session_id(tmp_path):
    cfg = _cfg(tmp_path, run_id=None)
    assert capture_identity(cfg, commit="x").run_id == "007"


def test_capture_identity_infers_market_when_absent(tmp_path):
    cfg = _cfg(tmp_path, symbols=["005930"], market=None, currency=None)
    idn = capture_identity(cfg, commit="x")
    assert idn.market == "KR" and idn.currency == "KRW"


# ---- producer commit (graceful) ----
def test_producer_commit_graceful_on_failure():
    def boom(*a, **k):
        raise FileNotFoundError("git missing")
    assert producer_commit(run=boom) is None


def test_producer_commit_reads_sha():
    class R:
        returncode = 0
        stdout = "abc123def456\n"
    assert producer_commit(run=lambda *a, **k: R()) == "abc123def456"


# ---- FRESH: 정상 시작 ----
def test_fresh_namespace_starts(tmp_path):
    p = _paths(tmp_path)
    res = startup_guard(_identity(), now_iso=_CLOCK, is_process_alive=_DEAD, **p)
    assert isinstance(res, GuardResult)
    assert res.manifest.lifecycle_state == "CREATED"
    assert res.manifest.identity == _identity()
    assert os.path.exists(p["manifest_path"])
    assert os.path.exists(p["lock_path"])
    # 재읽기 동일
    assert read_manifest(p["manifest_path"]).identity == _identity()


# ---- FRESH collision: 기존 manifest ----
def test_fresh_rejected_when_manifest_exists(tmp_path):
    p = _paths(tmp_path)
    startup_guard(_identity(), now_iso=_CLOCK, is_process_alive=_DEAD, **p)  # 1회차
    with pytest.raises(NamespaceCollisionError):
        startup_guard(_identity(run_id="007b"), now_iso=_CLOCK, is_process_alive=_DEAD, **p)


# ---- FRESH collision: 기존 DB 파일 (핵심 #006R 재사용 방지) ----
def test_fresh_rejected_when_db_exists(tmp_path):
    p = _paths(tmp_path)
    Path(p["db_paths"][0]).write_bytes(b"SQLite format 3\x00existing-006-data")
    with pytest.raises(NamespaceCollisionError):
        startup_guard(_identity(), now_iso=_CLOCK, is_process_alive=_DEAD, **p)
    assert not os.path.exists(p["manifest_path"])    # manifest 미생성


# ---- #006 namespace collision fixture: RESTART(006R)이 006 namespace 재사용 → 거부 ----
def test_006_restart_into_existing_namespace_rejected(tmp_path):
    p = _paths(tmp_path)
    # 006 이 이미 점유한 namespace(manifest + DB)
    startup_guard(_identity(session_id="006", run_id="006"),
                  now_iso=_CLOCK, is_process_alive=_DEAD, **p)
    Path(p["db_paths"][1]).write_bytes(b"SQLite format 3\x00candles")
    # 006R 이 RESTART 로 같은 경로를 가리킴 → 시작 전에 거부
    with pytest.raises(NamespaceCollisionError):
        startup_guard(_identity(session_id="006", run_id="006R", parent_run_id="006",
                                start_mode="RESTART"),
                      now_iso=_CLOCK, is_process_alive=_DEAD, **p)


# ---- guard 실패 시 DB 무쓰기 ----
def test_guard_failure_leaves_db_untouched(tmp_path):
    p = _paths(tmp_path)
    payload = b"SQLite format 3\x00" + b"row" * 100
    Path(p["db_paths"][2]).write_bytes(payload)
    before = Path(p["db_paths"][2]).read_bytes()
    with pytest.raises(NamespaceCollisionError):
        startup_guard(_identity(), now_iso=_CLOCK, is_process_alive=_DEAD, **p)
    assert Path(p["db_paths"][2]).read_bytes() == before      # 바이트 동일


# ---- RESUME: identity 일치 → 허용 ----
def test_resume_identity_match_ok(tmp_path):
    p = _paths(tmp_path)
    startup_guard(_identity(), now_iso=_CLOCK, is_process_alive=_DEAD, **p)
    res = startup_guard(_identity(start_mode="RESUME"),
                        now_iso=lambda: "2026-07-14T01:00:00Z",
                        is_process_alive=_DEAD, **p)
    assert res.manifest.identity.session_id == "007"
    assert res.manifest.updated_at == "2026-07-14T01:00:00Z"


# ---- RESUME: manifest 없음 → 거부 ----
def test_resume_without_manifest_rejected(tmp_path):
    p = _paths(tmp_path)
    with pytest.raises(MissingManifestError):
        startup_guard(_identity(start_mode="RESUME"), now_iso=_CLOCK,
                      is_process_alive=_DEAD, **p)


# ---- RESUME: provenance 불일치 → 거부 (필드별) ----
@pytest.mark.parametrize("field,value", [
    ("session_id", "999"),
    ("run_id", "999"),
    ("config_hash", "different"),
    ("producer_commit", "othersha"),
    ("market", "KR"),
    ("currency", "KRW"),
    ("initial_equity", 5000.0),
    ("schema_version", "9.9"),
    ("broker_mode", "LIVE"),
    ("ai_provider", "openai"),
])
def test_resume_mismatch_rejected(tmp_path, field, value):
    p = _paths(tmp_path)
    startup_guard(_identity(), now_iso=_CLOCK, is_process_alive=_DEAD, **p)
    with pytest.raises(IdentityMismatchError) as ei:
        startup_guard(_identity(start_mode="RESUME", **{field: value}),
                      now_iso=_CLOCK, is_process_alive=_DEAD, **p)
    assert field in str(ei.value)


# ---- 동시 실행: 활성 lock → 두 번째 거부 ----
def test_concurrent_second_process_rejected(tmp_path):
    p = _paths(tmp_path)
    startup_guard(_identity(), now_iso=_CLOCK, is_process_alive=_DEAD, **p)  # lock 획득
    # 두 번째: 살아있는 프로세스가 lock 보유 → 거부
    with pytest.raises(LockHeldError):
        startup_guard(_identity(start_mode="RESUME"), now_iso=_CLOCK,
                      is_process_alive=_ALIVE, **p)


# ---- stale lock: 검증된 resume 에서만 복구 ----
def test_stale_lock_recovered_on_resume(tmp_path):
    p = _paths(tmp_path)
    startup_guard(_identity(), now_iso=_CLOCK, is_process_alive=_DEAD, **p)
    # 프로세스 죽음(stale) → resume 이 lock 회수
    res = startup_guard(_identity(start_mode="RESUME"), now_iso=_CLOCK,
                        is_process_alive=_DEAD, **p)
    assert res.lock is not None


# ---- manifest atomic write: .tmp 잔류 없음 ----
def test_manifest_atomic_no_tmp_left(tmp_path):
    p = _paths(tmp_path)
    startup_guard(_identity(), now_iso=_CLOCK, is_process_alive=_DEAD, **p)
    assert not os.path.exists(p["manifest_path"] + ".tmp")


# ---- manifest round-trip ----
def test_manifest_roundtrip(tmp_path):
    m = SessionManifest.create(_identity(), "2026-07-14T00:00:00Z")
    d = m.to_dict()
    assert SessionManifest.from_dict(d) == m


# ---- identity from_dict: 누락된 기본값 필드는 기본값 사용(forward-compat) ----
def test_identity_from_dict_defaults_missing_id_version():
    d = _identity().to_dict()
    d.pop("id_version")
    assert SessionIdentity.from_dict(d).id_version == "1"


# ---- identity 필수 필드 누락 manifest → 손상으로 차단 ----
def test_manifest_missing_required_identity_field_blocks(tmp_path):
    p = _paths(tmp_path)
    Path(p["manifest_path"]).write_text(
        '{"identity": {"session_id": "007"}, "lifecycle_state": "CREATED"}', encoding="utf-8")
    with pytest.raises(ManifestReadError):
        startup_guard(_identity(), now_iso=_CLOCK, is_process_alive=_DEAD, **p)


# ---- lock 해제 후 재획득 가능 ----
def test_lock_release_allows_reacquire(tmp_path):
    p = _paths(tmp_path)
    res = startup_guard(_identity(), now_iso=_CLOCK, is_process_alive=_DEAD, **p)
    res.lock.release()
    assert not os.path.exists(p["lock_path"])
    # 해제 후 RESUME 재획득(살아있는 판정이어도 lock 파일이 없으므로 통과)
    res2 = startup_guard(_identity(start_mode="RESUME"), now_iso=_CLOCK,
                         is_process_alive=_ALIVE, **p)
    assert res2.lock is not None


# ---- FRESH 는 stale lock 을 회수하지 않는다(계약: 재사용 금지) ----
def test_fresh_rejected_by_stale_lock(tmp_path):
    p = _paths(tmp_path)
    # 빈 namespace 에 orphan(죽은 프로세스) lock 만 존재
    SessionLock.acquire(p["lock_path"], _identity(run_id="ghost"),
                        is_process_alive=_DEAD, now_iso=_CLOCK)
    with pytest.raises(StaleLockError):
        startup_guard(_identity(), now_iso=_CLOCK, is_process_alive=_DEAD, **p)


# ---- RESTART 는 검증된 재시작이므로 orphan lock 회수 ----
def test_restart_recovers_stale_lock_in_fresh_namespace(tmp_path):
    p = _paths(tmp_path)
    SessionLock.acquire(p["lock_path"], _identity(run_id="ghost"),
                        is_process_alive=_DEAD, now_iso=_CLOCK)
    res = startup_guard(_identity(start_mode="RESTART"), now_iso=_CLOCK,
                        is_process_alive=_DEAD, **p)
    assert res.lock is not None


# ---- RESTART parent 링크가 manifest 에 보존 ----
def test_restart_records_parent_run_id(tmp_path):
    p = _paths(tmp_path)
    res = startup_guard(
        _identity(session_id="006", run_id="006R", parent_run_id="006", start_mode="RESTART"),
        now_iso=_CLOCK, is_process_alive=_DEAD, **p)
    assert res.manifest.identity.parent_run_id == "006"
    assert read_manifest(p["manifest_path"]).identity.parent_run_id == "006"


# ---- 손상 manifest 는 시작을 차단(없는 것으로 취급하지 않음) ----
def test_corrupt_manifest_blocks_start(tmp_path):
    p = _paths(tmp_path)
    Path(p["manifest_path"]).write_text("{ this is not valid json", encoding="utf-8")
    with pytest.raises(ManifestReadError):
        startup_guard(_identity(), now_iso=_CLOCK, is_process_alive=_DEAD, **p)


# ---- 알 수 없는 start_mode 거부 ----
def test_unknown_start_mode_rejected(tmp_path):
    p = _paths(tmp_path)
    with pytest.raises(StartupGuardError):
        startup_guard(_identity(start_mode="BOGUS"), now_iso=_CLOCK,
                      is_process_alive=_DEAD, **p)


# ---- resolve_session_paths: config → 경로 파생(운영 진입점 경로) ----
def test_resolve_session_paths_defaults(tmp_path):
    cfg = _cfg(tmp_path)
    mp, lp, dbs = resolve_session_paths(cfg)
    assert mp == cfg.journal_db + ".manifest.json"
    assert lp == cfg.journal_db + ".lock"
    assert dbs == [cfg.journal_db, cfg.candle_db, cfg.metrics_db]


def test_resolve_session_paths_override(tmp_path):
    cfg = _cfg(tmp_path, session_manifest_path="data/x.manifest.json",
               session_lock_path="data/x.lock")
    mp, lp, _ = resolve_session_paths(cfg)
    assert mp == "data/x.manifest.json" and lp == "data/x.lock"
