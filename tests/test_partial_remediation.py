"""partial→final 감사 remediation 정책(이슈 #72).

first-write-wins 완화 없이, partial 잔존을 **감사 가능한 삭제**로만 교체한다:
- 기본 dry-run: DB 무변경.
- execute: expected 행수·hash 필수 + 사전 일치 검증(불일치 → 삭제 거부).
- 정확한 대상(symbol/interval/거래소-일자)만 삭제, 나머지 무변경.
- 백업(무결성 ok·pre-image 일치) + integrity_check + audit receipt 검증.

모든 mutation 은 임시 DB 에서만. canonical 미접근.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

import pytest

from src.history.partial_remediation import (
    RemediationError,
    RemediationPlan,
    identify_partial,
    remediate,
)
from src.history.store import HistoryStore, ImportStats
from src.models import Candle


def _c(symbol: str, iso_et: str, interval: str = "1m", close: float = 100.0) -> Candle:
    m = datetime.fromisoformat(iso_et)
    return Candle.from_moment(symbol=symbol, interval=interval, moment=m,
                              o=close, h=close + 1, low=close - 1, c=close, v=1000.0,
                              currency="USD", adjusted=True)


def _seed(dbp: str) -> None:
    candles = [
        # QQQ 1m 2025-07-16 (partial 대상, 2행)
        _c("QQQ", "2025-07-16T09:31:00-04:00"),
        _c("QQQ", "2025-07-16T09:32:00-04:00"),
        # QQQ 1m 2025-07-15 (보존, 3행)
        _c("QQQ", "2025-07-15T09:31:00-04:00"),
        _c("QQQ", "2025-07-15T09:32:00-04:00"),
        _c("QQQ", "2025-07-15T09:33:00-04:00"),
        # NVDA 1m 2025-07-16 (다른 심볼, 보존)
        _c("NVDA", "2025-07-16T09:31:00-04:00"),
        # QQQ 1d 2025-07-16 (다른 interval, 보존)
        _c("QQQ", "2025-07-16T00:00:00-04:00", interval="1d"),
    ]
    with HistoryStore(dbp) as s:
        s.upsert_candles(candles, source="toss_backfill", run_id="seed",
                         stats=ImportStats(source="toss_backfill"))
        s.commit()


def _count(dbp, symbol, interval, day=None) -> int:
    con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    try:
        if day is None:
            return con.execute(
                "SELECT COUNT(*) FROM candle_history WHERE symbol=? AND interval=?",
                (symbol, interval)).fetchone()[0]
        return con.execute(
            "SELECT COUNT(*) FROM candle_history WHERE symbol=? AND interval=? "
            "AND substr(ts_exchange,1,10)=?", (symbol, interval, day)).fetchone()[0]
    finally:
        con.close()


def test_dry_run_no_mutation(tmp_path):
    dbp = str(tmp_path / "m.sqlite")
    _seed(dbp)
    info = identify_partial(dbp, "QQQ", "1m", ["2025-07-16"])
    assert info["row_count"] == 2
    plan = RemediationPlan("QQQ", "1m", ("2025-07-16",),
                           expected_row_count=info["row_count"],
                           expected_content_hash=info["content_hash"])
    r = remediate(dbp, plan, backup_dir=str(tmp_path / "bk"), execute=False)
    assert r["status"] == "dry_run" and r["mutated"] is False
    assert r["would_delete"] == 2 and r["matches_expected"] is True
    # DB 무변경
    assert _count(dbp, "QQQ", "1m", "2025-07-16") == 2
    assert not (tmp_path / "bk").exists()  # dry-run 은 백업도 만들지 않음


def test_execute_deletes_only_target(tmp_path):
    dbp = str(tmp_path / "m.sqlite")
    _seed(dbp)
    info = identify_partial(dbp, "QQQ", "1m", ["2025-07-16"])
    plan = RemediationPlan("QQQ", "1m", ("2025-07-16",),
                           expected_row_count=info["row_count"],
                           expected_content_hash=info["content_hash"])
    receipt_path = str(tmp_path / "receipts.jsonl")
    r = remediate(dbp, plan, backup_dir=str(tmp_path / "bk"), execute=True,
                  receipt_path=receipt_path)
    assert r["status"] == "deleted" and r["mutated"] is True
    assert r["deleted"] == 2 and r["post_row_count"] == 0
    # 대상만 삭제
    assert _count(dbp, "QQQ", "1m", "2025-07-16") == 0
    # 나머지 전부 보존
    assert _count(dbp, "QQQ", "1m", "2025-07-15") == 3
    assert _count(dbp, "NVDA", "1m", "2025-07-16") == 1
    assert _count(dbp, "QQQ", "1d", "2025-07-16") == 1
    # 백업 무결성 ok + pre-image 보존
    bk = sqlite3.connect(f"file:{r['backup_path']}?mode=ro", uri=True)
    try:
        assert bk.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert bk.execute("SELECT COUNT(*) FROM candle_history WHERE symbol='QQQ' "
                          "AND interval='1m' AND substr(ts_exchange,1,10)='2025-07-16'"
                          ).fetchone()[0] == 2
    finally:
        bk.close()
    # audit receipt 기록
    lines = [json.loads(x) for x in open(receipt_path, encoding="utf-8")]
    assert lines[-1]["status"] == "deleted"
    assert lines[-1]["pre_content_hash"] == info["content_hash"]
    assert "refetch_hint" in lines[-1]


def test_refuse_on_hash_mismatch(tmp_path):
    dbp = str(tmp_path / "m.sqlite")
    _seed(dbp)
    plan = RemediationPlan("QQQ", "1m", ("2025-07-16",),
                           expected_row_count=2,
                           expected_content_hash="deadbeef" * 8)  # 틀린 hash
    with pytest.raises(RemediationError):
        remediate(dbp, plan, backup_dir=str(tmp_path / "bk"), execute=True)
    assert _count(dbp, "QQQ", "1m", "2025-07-16") == 2  # 무변경


def test_refuse_on_rowcount_mismatch(tmp_path):
    dbp = str(tmp_path / "m.sqlite")
    _seed(dbp)
    info = identify_partial(dbp, "QQQ", "1m", ["2025-07-16"])
    plan = RemediationPlan("QQQ", "1m", ("2025-07-16",),
                           expected_row_count=99,  # 틀린 행수
                           expected_content_hash=info["content_hash"])
    with pytest.raises(RemediationError):
        remediate(dbp, plan, backup_dir=str(tmp_path / "bk"), execute=True)
    assert _count(dbp, "QQQ", "1m", "2025-07-16") == 2  # 무변경


def test_execute_requires_expected(tmp_path):
    dbp = str(tmp_path / "m.sqlite")
    _seed(dbp)
    plan = RemediationPlan("QQQ", "1m", ("2025-07-16",))  # expected 없음
    with pytest.raises(RemediationError):
        remediate(dbp, plan, backup_dir=str(tmp_path / "bk"), execute=True)
    assert _count(dbp, "QQQ", "1m", "2025-07-16") == 2  # 무변경


def test_hash_fields_include_semantic_fields():
    # ★리뷰 P2: 삭제 대상 거래일을 정하는 ts_exchange 및 market/currency/adjusted 가 hash 에 포함.
    from src.history.partial_remediation import _HASH_FIELDS
    for f in ("ts_exchange", "market", "currency", "adjusted"):
        assert f in _HASH_FIELDS


def test_safe_token_blocks_path_traversal():
    # ★리뷰 P2: symbol 이 백업 파일명으로 경로 이탈하지 못하게 slug 화.
    from src.history.partial_remediation import _safe_token
    for evil in ("../../etc/passwd", "..", "/abs/path", "a/b\\c"):
        t = _safe_token(evil)
        assert "/" not in t and "\\" not in t and ".." not in t
    assert _safe_token("BRK.B") == "BRK_B"
    assert _safe_token("") == "x"


def test_execute_backup_stays_in_backup_dir_for_slashy_symbol(tmp_path):
    # 경로 구분자가 든 symbol 이어도 백업은 backup_dir 안에 안전하게 생성된다.
    dbp = str(tmp_path / "m.sqlite")
    evil = "../evil"
    with HistoryStore(dbp) as s:
        s.upsert_candles([_c(evil, "2025-07-16T09:31:00-04:00")],
                         source="toss_backfill", run_id="seed",
                         stats=ImportStats(source="toss_backfill"))
        s.commit()
    info = identify_partial(dbp, evil, "1m", ["2025-07-16"])
    bk = tmp_path / "bk"
    r = remediate(dbp, RemediationPlan(evil, "1m", ("2025-07-16",),
                                       expected_row_count=info["row_count"],
                                       expected_content_hash=info["content_hash"]),
                  backup_dir=str(bk), execute=True)
    assert r["status"] == "deleted"
    # 백업 경로가 backup_dir 내부
    assert os.path.realpath(r["backup_path"]).startswith(os.path.realpath(str(bk)))
