"""manifest completed_through ↔ ingest_log 교차검증 (이슈 #79 Scope A).

build_manifest 는 `collection_completed_through` CLI 인자만 믿지 않고, **명시적 collection
run_id** 로 ingest_log 에 기록된 실제 수집 cutoff 와 대조한다. 오선언 cutoff(≥ as_of 인데 실제
미수집)로 인한 거짓 OK 를 막는다. missing/malformed/conflicting/mismatch/incomplete-universe 는
모두 fail-closed(ProvenanceError). 레거시(run_id/cutoff provenance 없음)는 조용히 VERIFIED 로
승격하지 않는다. 검증은 **read-only** 이며 DB 내용·mtime 을 바꾸지 않고, 로그 행 순서에 무관하게
결정론적이다. daily/intraday 양쪽 동일 규율.

★ 자본 안전: order/broker/risk/live/runtime/pipeline/strategy import 0(정적 AST 검사, test #18).
"""
from __future__ import annotations

import ast
import hashlib
import os
import sqlite3

import pytest

from src.history import daily_manifest as dm
from src.history import intraday_manifest as im
from src.history.ingest_provenance import (
    BACKFILL_SOURCE,
    ProvenanceError,
    parse_ingest_note,
    parse_stopped_reason,
)
from src.history.store import HistoryStore, ImportStats
from src.ingest.backfill_history import BACKFILL_SOURCE as BF_SOURCE

YAML = "config/universe_p0_us30.yaml"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --------------------------------------------------------------------------
# 헬퍼: run_id + cutoff 로 ingest_log 행을 심는다(실 API/candle 불필요).
# --------------------------------------------------------------------------
def _universe() -> list[str]:
    return dm.read_p0_universe(YAML)["symbols"]


def _note(cutoff: str | None, *, reason: str = "nextbefore_null",
          extra: str | None = None) -> str:
    """`_ingest_note` 와 동일 문법의 구조화 note 를 만든다."""
    parts = [f"reason={reason}"]
    if cutoff is not None:
        parts += [f"completed_through={cutoff}", "excluded_after_cutoff=0"]
    if extra:
        parts.append(extra)
    return "; ".join(parts)


def _write_log(store: HistoryStore, run_id: str, symbol: str, interval: str,
               note: str | None) -> None:
    store.write_ingest_log(
        run_id, source=BACKFILL_SOURCE, stats=ImportStats(source=BACKFILL_SOURCE),
        rows_seen=0, earliest=None, latest=None, note=note,
        symbol=symbol, interval=interval)


def _seed_run(dbp: str, run_id: str, interval: str, *, cutoff: str | None,
              symbols: list[str] | None = None,
              per_symbol_note=None) -> None:
    """유니버스(또는 지정 심볼)에 run_id 로그를 심는다. per_symbol_note(sym)->note 로 개별 지정 가능."""
    syms = symbols if symbols is not None else _universe()
    with HistoryStore(dbp) as store:
        for s in syms:
            note = per_symbol_note(s) if per_symbol_note else _note(cutoff)
            _write_log(store, run_id, s, interval, note)
        store.commit()


def _empty_db(tmp_path) -> str:
    dbp = str(tmp_path / "m.sqlite")
    with HistoryStore(dbp):
        pass
    return dbp


def _build(mod, dbp, **kw):
    return mod.build_manifest(dbp, YAML, collection_params={},
                              collection_code_commit=None, **kw)


# 파라미터: (모듈, interval) — daily/intraday 동일 규율(test #11).
MODS = [(dm, "1d"), (im, "1m")]
IDS = ["daily", "intraday"]


# ==========================================================================
# note 파서 계약
# ==========================================================================
def test_parse_ingest_note_structured_with_error_semicolon():
    n = ("reason=no_progress; sessions_allowed=POST,PRE,REGULAR; "
         "completed_through=2026-07-21; excluded_after_cutoff=2; "
         "error=RuntimeError('a; b=c')")
    p = parse_ingest_note(n)
    assert p["reason"] == "no_progress"
    assert p["completed_through"] == "2026-07-21"
    assert p["excluded_after_cutoff"] == "2"
    assert p["error"] == "RuntimeError('a; b=c')"  # ';'/'=' 포함 error 도 온전히 분리
    assert p["structured"] is True


def test_parse_ingest_note_legacy_bare_has_no_completed_through():
    p = parse_ingest_note("no_progress")
    assert p["completed_through"] is None
    assert p["structured"] is False


def test_parse_stopped_reason_matches_legacy_behavior():
    assert parse_stopped_reason("reason=max_pages; completed_through=2026-07-21") == "max_pages"
    assert parse_stopped_reason("no_progress") == "no_progress"       # bare 통째
    assert parse_stopped_reason("reason=; x=1") is None               # 빈 reason
    assert parse_stopped_reason(None) is None


def test_backfill_source_constant_matches():
    """교차검증 source 상수가 backfill 이 쓰는 값과 일치(불일치 시 검증이 조용히 무의미)."""
    assert BACKFILL_SOURCE == BF_SOURCE == "toss_backfill"


# ==========================================================================
# test #2: 선언 cutoff == ingest_log cutoff → VERIFIED
# ==========================================================================
@pytest.mark.parametrize("mod,interval", MODS, ids=IDS)
def test_verified_when_declared_equals_ingest_cutoff(tmp_path, mod, interval):
    dbp = _empty_db(tmp_path)
    _seed_run(dbp, "RUN-A", interval, cutoff="2026-07-21")
    man = _build(mod, dbp, as_of="2026-07-21",
                 collection_completed_through="2026-07-21", collection_run_id="RUN-A")
    pv = man["provenance_verification"]
    assert pv["provenance_status"] == "VERIFIED"
    assert pv["collection_run_id"] == "RUN-A"
    assert pv["verified_completed_through"] == "2026-07-21"
    assert pv["declared_completed_through"] == "2026-07-21"
    assert pv["verified_symbol_count"] == 30
    assert pv["source"] == "toss_backfill" and pv["interval"] == interval


@pytest.mark.parametrize("mod,interval", MODS, ids=IDS)
def test_verified_without_declared_uses_ingest_cutoff(tmp_path, mod, interval):
    """선언 cutoff 를 안 줘도 run_id 만으로 검증 cutoff 가 권위가 된다(as_of 경계 판정)."""
    dbp = _empty_db(tmp_path)
    _seed_run(dbp, "RUN-A", interval, cutoff="2026-07-21")
    man = _build(mod, dbp, as_of="2026-07-17", collection_run_id="RUN-A")
    pv = man["provenance_verification"]
    assert pv["provenance_status"] == "VERIFIED"
    assert pv["verified_completed_through"] == "2026-07-21"
    assert pv["declared_completed_through"] is None


# ==========================================================================
# test #3: 선언 cutoff != ingest_log cutoff → fail-closed
# ==========================================================================
@pytest.mark.parametrize("mod,interval", MODS, ids=IDS)
def test_declared_mismatch_fails_closed(tmp_path, mod, interval):
    dbp = _empty_db(tmp_path)
    _seed_run(dbp, "RUN-A", interval, cutoff="2026-07-21")
    with pytest.raises(ProvenanceError, match="DECLARED_MISMATCH"):
        _build(mod, dbp, as_of="2026-07-17",
               collection_completed_through="2026-07-18", collection_run_id="RUN-A")


# ==========================================================================
# test #4: ingest_log completed_through 누락 → fail-closed
# ==========================================================================
@pytest.mark.parametrize("mod,interval", MODS, ids=IDS)
def test_missing_completed_through_fails_closed(tmp_path, mod, interval):
    dbp = _empty_db(tmp_path)
    # 전 종목 로그는 있으나 completed_through 가 없는 레거시/비-cutoff 노트.
    _seed_run(dbp, "RUN-A", interval, cutoff=None,
              per_symbol_note=lambda s: _note(None))
    with pytest.raises(ProvenanceError, match="INCONCLUSIVE_PROVENANCE"):
        _build(mod, dbp, as_of="2026-07-21", collection_run_id="RUN-A")


@pytest.mark.parametrize("mod,interval", MODS, ids=IDS)
def test_legacy_bare_note_not_promoted(tmp_path, mod, interval):
    """레거시 bare note(구조화 키 없음)를 조용히 VERIFIED 로 승격하지 않는다."""
    dbp = _empty_db(tmp_path)
    _seed_run(dbp, "RUN-A", interval, cutoff=None,
              per_symbol_note=lambda s: "nextbefore_null")
    with pytest.raises(ProvenanceError, match="INCONCLUSIVE_PROVENANCE"):
        _build(mod, dbp, as_of="2026-07-21", collection_run_id="RUN-A")


# ==========================================================================
# test #5: malformed completed_through → fail-closed
# ==========================================================================
@pytest.mark.parametrize("mod,interval", MODS, ids=IDS)
def test_malformed_completed_through_fails_closed(tmp_path, mod, interval):
    dbp = _empty_db(tmp_path)
    _seed_run(dbp, "RUN-A", interval, cutoff=None,
              per_symbol_note=lambda s: _note("2026/07/21"))  # 잘못된 구분자
    with pytest.raises(ProvenanceError, match="MALFORMED_COMPLETED_THROUGH"):
        _build(mod, dbp, as_of="2026-07-21", collection_run_id="RUN-A")


# ==========================================================================
# test #6: 같은 run 안 conflicting cutoff → fail-closed
# ==========================================================================
@pytest.mark.parametrize("mod,interval", MODS, ids=IDS)
def test_conflicting_cutoff_in_same_run_fails_closed(tmp_path, mod, interval):
    dbp = _empty_db(tmp_path)
    syms = _universe()
    # 한 종목만 다른 cutoff 를 갖게 한다.
    odd = syms[0]
    _seed_run(dbp, "RUN-A", interval, cutoff=None,
              per_symbol_note=lambda s: _note("2026-07-20" if s == odd else "2026-07-21"))
    with pytest.raises(ProvenanceError, match="CONFLICTING_CUTOFF"):
        _build(mod, dbp, as_of="2026-07-17", collection_run_id="RUN-A")


# ==========================================================================
# test #7: 대상 symbol 하나의 로그 누락 → fail-closed
# ==========================================================================
@pytest.mark.parametrize("mod,interval", MODS, ids=IDS)
def test_missing_symbol_log_fails_closed(tmp_path, mod, interval):
    dbp = _empty_db(tmp_path)
    syms = _universe()
    _seed_run(dbp, "RUN-A", interval, cutoff="2026-07-21", symbols=syms[1:])  # 첫 종목 누락
    with pytest.raises(ProvenanceError, match="MISSING_SYMBOLS"):
        _build(mod, dbp, as_of="2026-07-21", collection_run_id="RUN-A")


# ==========================================================================
# test #8: 다른 run_id 의 로그로 누락 symbol 을 보충하지 않음
# ==========================================================================
@pytest.mark.parametrize("mod,interval", MODS, ids=IDS)
def test_other_run_does_not_supplement_missing_symbol(tmp_path, mod, interval):
    dbp = _empty_db(tmp_path)
    syms = _universe()
    # RUN-A 는 첫 종목이 없고, 그 종목은 RUN-B 에만 있다 → RUN-A 검증은 여전히 실패.
    _seed_run(dbp, "RUN-A", interval, cutoff="2026-07-21", symbols=syms[1:])
    _seed_run(dbp, "RUN-B", interval, cutoff="2026-07-21", symbols=[syms[0]])
    with pytest.raises(ProvenanceError, match="MISSING_SYMBOLS"):
        _build(mod, dbp, as_of="2026-07-21", collection_run_id="RUN-A")


def test_no_run_logs_fails_closed(tmp_path):
    """존재하지 않는 run_id → NO_RUN_LOGS(다른 run 로 보충 금지)."""
    dbp = _empty_db(tmp_path)
    _seed_run(dbp, "RUN-A", "1d", cutoff="2026-07-21")
    with pytest.raises(ProvenanceError, match="NO_RUN_LOGS"):
        _build(dm, dbp, as_of="2026-07-21", collection_run_id="RUN-MISSING")


def test_empty_run_id_rejected_clearly(tmp_path):
    """빈/공백 run_id 는 NO_RUN_LOGS 로 흐르지 않고 EMPTY_RUN_ID 로 명확히 거부(fail-closed)."""
    dbp = _empty_db(tmp_path)
    _seed_run(dbp, "RUN-A", "1d", cutoff="2026-07-21")
    with pytest.raises(ProvenanceError, match="EMPTY_RUN_ID"):
        _build(dm, dbp, as_of="2026-07-21", collection_run_id="   ")


def test_wrong_interval_not_mixed(tmp_path):
    """run_id 는 있으나 다른 interval 로그만 존재하면 대상 interval 로는 NO_RUN_LOGS."""
    dbp = _empty_db(tmp_path)
    _seed_run(dbp, "RUN-A", "1m", cutoff="2026-07-21")   # 1m 만 심음
    with pytest.raises(ProvenanceError, match="NO_RUN_LOGS"):
        _build(dm, dbp, as_of="2026-07-21", collection_run_id="RUN-A")  # daily(1d) 검증


# ==========================================================================
# test #9: 같은 run_id resume/retry 복수 로그(동일 cutoff) → 결정론 통과
# ==========================================================================
@pytest.mark.parametrize("mod,interval", MODS, ids=IDS)
def test_resume_retry_same_cutoff_deterministic_pass(tmp_path, mod, interval):
    dbp = _empty_db(tmp_path)
    # 각 종목에 같은 run_id·같은 cutoff 로그를 2개씩(중단→resume 모사) 심는다.
    _seed_run(dbp, "RUN-A", interval, cutoff="2026-07-21")
    _seed_run(dbp, "RUN-A", interval, cutoff="2026-07-21")
    man = _build(mod, dbp, as_of="2026-07-21",
                 collection_completed_through="2026-07-21", collection_run_id="RUN-A")
    assert man["provenance_verification"]["provenance_status"] == "VERIFIED"
    assert man["provenance_verification"]["verified_completed_through"] == "2026-07-21"


# ==========================================================================
# test #10: 검증 cutoff < as_of → fail-closed(기존과 동일 규율, 검증 경로)
# ==========================================================================
@pytest.mark.parametrize("mod,interval", MODS, ids=IDS)
def test_verified_cutoff_before_asof_fails_closed(tmp_path, mod, interval):
    dbp = _empty_db(tmp_path)
    _seed_run(dbp, "RUN-A", interval, cutoff="2026-07-17")
    with pytest.raises(ProvenanceError, match="VERIFIED_CUTOFF_BEFORE_ASOF"):
        _build(mod, dbp, as_of="2026-07-21", collection_run_id="RUN-A")


# ==========================================================================
# 레거시 경로(run_id 미지정): 의미 소급 변경 없음 — declared-only 표기
# ==========================================================================
@pytest.mark.parametrize("mod,interval", MODS, ids=IDS)
def test_declared_only_status_when_no_run_id(tmp_path, mod, interval):
    dbp = _empty_db(tmp_path)
    man = _build(mod, dbp, as_of="2026-07-21", collection_completed_through="2026-07-21")
    pv = man["provenance_verification"]
    assert pv["provenance_status"] == "DECLARED_UNVERIFIED"
    assert pv["collection_run_id"] is None
    assert pv["verified_completed_through"] is None


@pytest.mark.parametrize("mod,interval", MODS, ids=IDS)
def test_none_status_when_no_cutoff_no_run_id(tmp_path, mod, interval):
    dbp = _empty_db(tmp_path)
    man = _build(mod, dbp, as_of="2026-07-21")
    assert man["provenance_verification"]["provenance_status"] == "NONE"


@pytest.mark.parametrize("mod,interval", MODS, ids=IDS)
def test_declared_only_cutoff_before_asof_still_fails(tmp_path, mod, interval):
    """레거시 declared-only 경로의 cutoff<as_of fail-closed 는 그대로 유지(이슈 #72)."""
    dbp = _empty_db(tmp_path)
    with pytest.raises(ValueError):
        _build(mod, dbp, as_of="2026-07-21", collection_completed_through="2026-07-17")


# ==========================================================================
# test #16: 검증은 read-only — DB 내용/크기/mtime 무변경
# ==========================================================================
def test_verification_is_read_only(tmp_path):
    dbp = _empty_db(tmp_path)
    _seed_run(dbp, "RUN-A", "1d", cutoff="2026-07-21")
    _seed_run(dbp, "RUN-A", "1m", cutoff="2026-07-21")

    def _sig(p):
        with open(p, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        st = os.stat(p)
        return (digest, st.st_size, st.st_mtime_ns)

    before = _sig(dbp)
    _build(dm, dbp, as_of="2026-07-21", collection_run_id="RUN-A")
    _build(im, dbp, as_of="2026-07-21", collection_run_id="RUN-A")
    assert _sig(dbp) == before  # 내용·크기·mtime 완전 불변


# ==========================================================================
# test #17: 로그 행 순서가 바뀌어도 manifest 결과·지문 동일
# ==========================================================================
def test_provenance_order_independent(tmp_path):
    syms = _universe()
    db1 = str(tmp_path / "a.sqlite")
    db2 = str(tmp_path / "b.sqlite")
    with HistoryStore(db1) as s:
        for x in syms:
            _write_log(s, "RUN-A", x, "1d", _note("2026-07-21"))
        s.commit()
    with HistoryStore(db2) as s:                 # 역순 삽입
        for x in reversed(syms):
            _write_log(s, "RUN-A", x, "1d", _note("2026-07-21"))
        s.commit()
    m1 = _build(dm, db1, as_of="2026-07-21", collection_run_id="RUN-A")
    m2 = _build(dm, db2, as_of="2026-07-21", collection_run_id="RUN-A")
    assert m1["provenance_verification"] == m2["provenance_verification"]
    assert m1["data_content_sha256"] == m2["data_content_sha256"]


def test_provenance_not_in_content_hash(tmp_path):
    """provenance_verification 은 data_content_sha256 에 영향을 주지 않는다(지문 분리, 조건 11)."""
    dbp = _empty_db(tmp_path)
    _seed_run(dbp, "RUN-A", "1d", cutoff="2026-07-21")
    verified = _build(dm, dbp, as_of="2026-07-21", collection_run_id="RUN-A")
    declared = _build(dm, dbp, as_of="2026-07-21", collection_completed_through="2026-07-21")
    plain = _build(dm, dbp, as_of="2026-07-21")
    assert verified["data_content_sha256"] == declared["data_content_sha256"] == plain["data_content_sha256"]
    # 그러나 provenance_status 는 서로 다르다(실행 provenance 는 별개 지문).
    assert verified["provenance_verification"]["provenance_status"] == "VERIFIED"
    assert declared["provenance_verification"]["provenance_status"] == "DECLARED_UNVERIFIED"


# ==========================================================================
# 실패 메시지에 시크릿·절대경로 없음(조건 9)
# ==========================================================================
def test_error_message_has_no_abs_path_or_secret(tmp_path):
    dbp = _empty_db(tmp_path)
    _seed_run(dbp, "RUN-A", "1d", cutoff="2026-07-21")
    with pytest.raises(ProvenanceError) as ei:
        _build(dm, dbp, as_of="2026-07-21",
               collection_completed_through="2026-07-18", collection_run_id="RUN-A")
    msg = str(ei.value)
    assert tmp_path.name not in msg and "/Users/" not in msg and ".sqlite" not in msg


# ==========================================================================
# test #18: 자본 경로 격리(정적 AST) — 신규/변경 모듈이 자본 경로를 import 하지 않음
# ==========================================================================
FORBIDDEN_PREFIXES = (
    "src.live", "src.order", "src.broker", "src.risk", "src.runtime",
    "src.pipeline", "src.strategy", "src.engines", "src.exits", "src.pnl",
    "src.portfolio", "src.reconcile", "src.journal",
)
GUARDED_MODULES = [
    "src/history/ingest_provenance.py",
    "src/history/daily_manifest.py",
    "src/history/intraday_manifest.py",
    "src/ingest/backfill_history.py",
]


def _imports(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.append(node.module)
    return mods


@pytest.mark.parametrize("rel", GUARDED_MODULES)
def test_capital_path_isolation(rel):
    path = os.path.join(REPO, rel)
    offenders = [m for m in _imports(path)
                 if any(m == p or m.startswith(p + ".") for p in FORBIDDEN_PREFIXES)]
    assert offenders == [], f"{rel} 가 자본 경로를 import 함: {offenders}"


def test_guarded_modules_exist():
    for rel in GUARDED_MODULES:
        assert os.path.exists(os.path.join(REPO, rel)), f"검사 대상 파일 없음: {rel}"
