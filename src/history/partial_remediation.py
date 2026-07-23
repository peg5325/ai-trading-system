"""partial→final 안전 교체 정책 — 감사 가능한 delete→refetch(이슈 #72).

`HistoryStore` 는 동일 우선순위 충돌 시 first-write-wins 다(자본 안전 · 원본 불변 정책).
그 결과 진행 중 세션의 partial 봉이 한 번 들어가면 장 마감 후 final 봉이 자동 교체되지 않는다.
generic `force_update` 우회는 **추가하지 않는다**(first-write-wins 완화 금지). 대신 partial 잔존을
**감사 가능한 삭제 후 재수집**으로만 교체한다.

표준 정책(이 모듈이 강제하는 순서):
  1. partial 식별(symbol/interval/거래소-로컬 일자 범위).
  2. pre-image 행수·content hash 기록.
  3. SQLite backup API 로 안전 백업(원본 read-only 스냅샷).
  4. 예상 범위만 transaction DELETE.
  5. 삭제 행수·대상 pre-image hash 가 **예상값과 불일치하면 rollback**(무변경).
  6. integrity_check(불통 시 rollback).
  7. 완료-거래일 cutoff 를 적용해 refetch  ← **이 모듈 밖**: 공식 경로
     `scripts/history_backfill_official.py --completed-through …` 재실행(이번 PR 은 API 호출 0).
  8. post-image hash·행수·감사 receipt(JSONL) 기록.

안전:
- **기본 dry-run**. 실제 삭제는 execute=True + expected_row_count + expected_content_hash 필수.
- 예상값(행수·hash)이 현재 대상과 다르면 삭제 전에 거부(fail-closed) — DB 가 예상 상태가 아니면 손대지 않음.
- 모든 mutation 은 호출자가 준 db_path 에서만 일어난다. canonical `data/market_history.sqlite` 에
  대한 실행은 이번 작업 범위 밖(테스트는 임시 DB 전용).

★ 자본 안전: 주문/broker/risk/live/pipeline/전략/config 를 import 하지 않는다. Toss API 호출 없음(순수 로컬).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

# 백업 파일명 토큰 안전화: 영숫자 외(경로 구분자·`.`·`..` 포함)를 '_' 로 치환해 경로 이탈을 원천 차단.
_UNSAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9]+")


def _safe_token(value: str) -> str:
    """symbol/interval 을 파일명 안전 slug 로(경로 구분자·`..` 제거). 빈 값은 'x'."""
    return _UNSAFE_TOKEN_RE.sub("_", str(value)).strip("_") or "x"

# content hash 대상 = created_at/updated_at(감사 ts) 를 뺀 **모든 의미 필드**. 특히 ts_exchange 는
# 삭제 대상 거래일(substr(ts_exchange,1,10))을 결정하므로 반드시 포함해야 TOCTOU 재검증이 의미 변경을
# 놓치지 않는다(리뷰 P2). market/currency/adjusted 도 포함(수정주가·통화 변경 탐지).
_HASH_FIELDS = ("symbol", "interval", "ts_utc", "ts_exchange", "session", "market",
                "currency", "adjusted", "open", "high", "low", "close", "volume", "source")


class RemediationError(RuntimeError):
    """remediation 계약 위반(예상값 누락·불일치·무결성 실패 등)."""


def _connect_ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _now_iso(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).isoformat()


def _day_placeholders(days: list[str]) -> str:
    return ",".join("?" for _ in days)


def target_rows(con: sqlite3.Connection, symbol: str, interval: str,
                exchange_days: list[str]) -> list[sqlite3.Row]:
    """대상 행(symbol·interval·거래소-로컬 일자 ∈ exchange_days)을 ts_utc 정렬로 반환."""
    if not exchange_days:
        return []
    ph = _day_placeholders(exchange_days)
    sql = (f"SELECT {', '.join(_HASH_FIELDS)} FROM candle_history "
           f"WHERE symbol=? AND interval=? AND substr(ts_exchange,1,10) IN ({ph}) "
           f"ORDER BY ts_utc")
    return con.execute(sql, (symbol, interval, *exchange_days)).fetchall()


def content_hash(rows: list[sqlite3.Row]) -> str:
    """대상 행들의 결정론적 SHA-256(정렬된 데이터 유도 필드만). 같은 내용 → 같은 hash."""
    h = hashlib.sha256()
    for r in rows:
        line = "|".join(f"{r[f]!r}" for f in _HASH_FIELDS) + "\n"
        h.update(line.encode("utf-8"))
    return h.hexdigest()


def identify_partial(db_path: str, symbol: str, interval: str,
                     exchange_days: list[str]) -> dict:
    """대상 partial 범위의 현재 행수·content hash 를 계산(read-only, 무변경).

    반환값의 row_count/content_hash 를 이후 remediate(...) 의 expected_* 로 넘겨
    '내가 본 상태 그대로만 삭제'를 강제한다."""
    con = _connect_ro(db_path)
    try:
        rows = target_rows(con, symbol, interval, exchange_days)
        return {
            "symbol": symbol,
            "interval": interval,
            "exchange_days": sorted(exchange_days),
            "row_count": len(rows),
            "content_hash": content_hash(rows),
        }
    finally:
        con.close()


def _integrity_ok(con: sqlite3.Connection) -> str:
    return con.execute("PRAGMA integrity_check").fetchone()[0]


def _backup_via_api(src_path: str, dst_path: str) -> None:
    """SQLite backup API 로 원본(read-only)을 dst 로 스냅샷(안전 백업)."""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def _append_receipt(receipt_path: str, entry: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(receipt_path)), exist_ok=True)
    with open(receipt_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


@dataclass(frozen=True)
class RemediationPlan:
    symbol: str
    interval: str
    exchange_days: tuple[str, ...]
    expected_row_count: int | None = None
    expected_content_hash: str | None = None


def remediate(db_path: str, plan: RemediationPlan, *, backup_dir: str,
              execute: bool = False, receipt_path: str | None = None,
              now: datetime | None = None) -> dict:
    """partial 범위를 감사 가능하게 삭제(기본 dry-run). 재수집(refetch)은 공식 backfill 재실행이 담당.

    - execute=False(기본): 현재 대상 행수·hash 와 예상값 일치 여부만 보고. **DB 무변경**.
    - execute=True: expected_row_count + expected_content_hash 필수. 현재 대상이 예상과 다르면
      삭제 전에 거부(fail-closed). 백업 → transaction DELETE → 삭제 행수·무결성 검증 →
      불일치/불통 시 rollback → 통과 시 commit. post-image·receipt 기록.
    """
    days = list(plan.exchange_days)
    ts = _now_iso(now)
    con_ro = _connect_ro(db_path)
    try:
        pre_rows = target_rows(con_ro, plan.symbol, plan.interval, days)
    finally:
        con_ro.close()
    pre_count = len(pre_rows)
    pre_hash = content_hash(pre_rows)

    base = {
        "schema": "partial_remediation_receipt/v1",
        "ts": ts,
        "db_basename": os.path.basename(db_path),
        "symbol": plan.symbol,
        "interval": plan.interval,
        "exchange_days": sorted(days),
        "pre_row_count": pre_count,
        "pre_content_hash": pre_hash,
        "expected_row_count": plan.expected_row_count,
        "expected_content_hash": plan.expected_content_hash,
        "matches_expected": (plan.expected_row_count == pre_count
                             and plan.expected_content_hash == pre_hash),
    }

    if not execute:
        receipt = {**base, "status": "dry_run", "mutated": False,
                   "would_delete": pre_count, "backup_path": None,
                   "post_row_count": pre_count, "post_content_hash": pre_hash}
        if receipt_path:
            _append_receipt(receipt_path, receipt)
        return receipt

    # --- execute 경로: 예상값 필수 + 사전 일치 검증(fail-closed) ---
    if plan.expected_row_count is None or plan.expected_content_hash is None:
        receipt = {**base, "status": "refused_missing_expected", "mutated": False,
                   "backup_path": None, "post_row_count": pre_count,
                   "post_content_hash": pre_hash}
        if receipt_path:
            _append_receipt(receipt_path, receipt)
        raise RemediationError(
            "execute 삭제에는 expected_row_count 와 expected_content_hash 가 필수다(dry-run 으로 확인 후 지정).")
    if not base["matches_expected"]:
        receipt = {**base, "status": "refused_mismatch", "mutated": False,
                   "backup_path": None, "post_row_count": pre_count,
                   "post_content_hash": pre_hash}
        if receipt_path:
            _append_receipt(receipt_path, receipt)
        raise RemediationError(
            f"현재 대상(rows={pre_count}, hash={pre_hash[:12]})이 예상값"
            f"(rows={plan.expected_row_count}, hash={str(plan.expected_content_hash)[:12]})과 "
            f"불일치 — 삭제 거부(fail-closed).")

    # --- 백업(원본 read-only 스냅샷) ---
    # 파일명은 안전 slug(경로 이탈 방지) + receipt ts(반복 remediation 백업 덮어쓰기 방지).
    os.makedirs(backup_dir, exist_ok=True)
    ts_tag = ts.replace(":", "").replace("+", "").replace(".", "")
    fname = (f"{_safe_token(plan.symbol)}_{_safe_token(plan.interval)}"
             f"_premediation_{ts_tag}_{pre_hash[:12]}.sqlite")
    backup_path = os.path.join(backup_dir, fname)
    # 방어적 재확인: 정규화 경로가 backup_dir 안인지(경로 이탈 이중 차단).
    real_dir = os.path.realpath(backup_dir)
    if os.path.commonpath([real_dir, os.path.realpath(backup_path)]) != real_dir:
        raise RemediationError(f"백업 경로가 backup_dir 밖 — 거부(경로 이탈 방지): {fname!r}")
    _backup_via_api(db_path, backup_path)
    bcon = _connect_ro(backup_path)
    try:
        if _integrity_ok(bcon) != "ok":
            raise RemediationError("백업 무결성 실패 — 삭제를 진행하지 않는다.")
        backup_target = target_rows(bcon, plan.symbol, plan.interval, days)
    finally:
        bcon.close()
    if content_hash(backup_target) != pre_hash or len(backup_target) != pre_count:
        raise RemediationError("백업이 pre-image 와 불일치 — 삭제를 진행하지 않는다.")

    # --- transaction DELETE + 행수/무결성 검증 → rollback or commit ---
    ph = _day_placeholders(days)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        con.isolation_level = None  # 수동 트랜잭션
        con.execute("BEGIN IMMEDIATE")
        # ★TOCTOU 차단: pre-image 는 별도 연결에서 락 밖에 계산됐다. 삭제 직전 **같은 트랜잭션
        #   (BEGIN IMMEDIATE 쓰기락) 안에서** 대상을 다시 읽어 행수·content hash 를 재검증한다.
        #   락 획득과 재읽기 사이 값이 바뀌었으면(동시 writer) 삭제하지 않고 rollback.
        locked_rows = target_rows(con, plan.symbol, plan.interval, days)
        if (len(locked_rows) != plan.expected_row_count
                or content_hash(locked_rows) != plan.expected_content_hash):
            con.execute("ROLLBACK")
            receipt = {**base, "status": "rollback_toctou", "mutated": False,
                       "backup_path": backup_path, "post_row_count": pre_count,
                       "post_content_hash": pre_hash}
            if receipt_path:
                _append_receipt(receipt_path, receipt)
            raise RemediationError(
                "쓰기락 하 대상이 예상값과 불일치(동시 변경 의심) — rollback(무변경).")
        cur = con.execute(
            f"DELETE FROM candle_history WHERE symbol=? AND interval=? "
            f"AND substr(ts_exchange,1,10) IN ({ph})",
            (plan.symbol, plan.interval, *days))
        deleted = cur.rowcount
        if deleted != plan.expected_row_count:
            con.execute("ROLLBACK")
            receipt = {**base, "status": "rollback_rowcount", "mutated": False,
                       "deleted": deleted, "backup_path": backup_path,
                       "post_row_count": pre_count, "post_content_hash": pre_hash}
            if receipt_path:
                _append_receipt(receipt_path, receipt)
            raise RemediationError(
                f"삭제 행수 {deleted} != 예상 {plan.expected_row_count} — rollback(무변경).")
        integ = _integrity_ok(con)
        if integ != "ok":
            con.execute("ROLLBACK")
            receipt = {**base, "status": "rollback_integrity", "mutated": False,
                       "integrity": integ, "backup_path": backup_path,
                       "post_row_count": pre_count, "post_content_hash": pre_hash}
            if receipt_path:
                _append_receipt(receipt_path, receipt)
            raise RemediationError(f"integrity_check={integ} — rollback(무변경).")
        con.execute("COMMIT")
    finally:
        con.close()

    # --- post-image(삭제 후 대상은 0행) + receipt ---
    con_ro2 = _connect_ro(db_path)
    try:
        post_rows = target_rows(con_ro2, plan.symbol, plan.interval, days)
    finally:
        con_ro2.close()
    receipt = {
        **base, "status": "deleted", "mutated": True,
        "deleted": deleted, "backup_path": backup_path,
        "post_row_count": len(post_rows),
        "post_content_hash": content_hash(post_rows),
        "refetch_hint": ("scripts/history_backfill_official.py 를 동일 심볼/interval 로 "
                         "--completed-through 를 지정해 재실행하면 완성봉으로 재수집된다."),
    }
    if receipt_path:
        _append_receipt(receipt_path, receipt)
    return receipt
