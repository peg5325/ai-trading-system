#!/usr/bin/env python3
"""NYSE 조기폐장일 13:00 ET 이후 REGULAR 1분봉을 POST 로 다시 라벨한다(사용자 결정 2026-10-15).

`classify_session` 은 2026-10-15 부터 조기폐장일 REGULAR 를 09:30–13:00 으로, 13:00–20:00 을 POST 로
라벨한다. 그 전에 저장된 행은 13:00–15:59 가 REGULAR 로 남아 있고, 재수집해도 store 는 기존 값을
유지(first-write-wins)하므로 이 스크립트로 한 번 고친다.

대상: market='US', interval='1m', 날짜 = `exchange_calendar` 조기폐장일, ts_exchange 로컬 시각 >= 13:00,
session='REGULAR' → session='POST', updated_at=지금(UTC). 그 외 행·컬럼은 건드리지 않는다.

실행 순서: launchd 가 실행하는 사용자 checkout 을 병합된 main 으로 fast-forward → launchd 재설치
(premarket launchd `--install`) → 그 다음 `--apply`. 옛 코드가 돌고 있으면 더 높은 우선순위 소스의
재수집이 라벨을 다시 REGULAR 로 되돌릴 수 있다.

안전:
- 기본은 dry-run(읽기 전용 연결, 쓰기 없음). `--apply` 가 있어야 쓴다.
- `--apply` 는 실재하는 파일을 가리키는 `--backup-verified <path>` 가 필요하다. 그 백업을 읽기 전용으로
  열어 PRAGMA quick_check == ok 이고 candle_history 테이블이 있어야 한다(아니면 거부). 크기·mtime·
  quick_check 결과를 receipt 에 남긴다.
- `--db` 는 `--root/data` 아래여야 한다(`--allow-db-outside-root` 로만 예외).
- 쓰기 동안 `market_db_lock(root)` 을 잡고, 한 트랜잭션으로 갱신한 뒤 같은 트랜잭션 안에서
  13:00 이후 REGULAR 가 0 건인지 확인한다(아니면 롤백). 다시 돌리면 대상 0 건(멱등).
- 갱신 뒤 독립 검증: INDEXED BY·`_WHERE` 를 쓰지 않고 PK(symbol, interval, ts_utc) 범위로 조기폐장일
  13:00 ET 이후 행을 읽어 파이썬에서 REGULAR 잔존을 센다(있으면 롤백). 갱신된 행 표본은
  `classify_session` 이 POST 를 내는지 다시 확인한다.
- 조회는 심볼·날짜별로 `ix_hist_symbol_day`(symbol, interval, substr(ts_exchange,1,10)) 를 탄다.
- receipt(JSON)는 먼저 출력(flush)한 뒤 `reports/data-ops/` 에 쓴다(--apply 일 때만 파일로 남긴다).
  파일 쓰기가 실패하면 오류를 출력하고 0 이 아닌 코드로 끝난다(DB 변경은 이미 커밋된 상태).
외부 API 를 호출하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history import exchange_calendar as cal
from src.models import ET, MARKET_US, SESSION_POST, SESSION_REGULAR, classify_session
from src.output_layout import data_ops_dir
from src.premarket.operations import market_db_lock

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "relabel_early_close_sessions/v1"
CUTOFF_HHMM = "13:00"

_SYMBOLS_SQL = (
    "WITH RECURSIVE s(sym) AS ("
    " SELECT min(symbol) FROM candle_history"
    " UNION ALL SELECT (SELECT min(symbol) FROM candle_history WHERE symbol > s.sym) FROM s"
    " WHERE s.sym IS NOT NULL) SELECT sym FROM s WHERE sym IS NOT NULL")
_WHERE = ("symbol=? AND interval='1m' AND substr(ts_exchange,1,10)=? AND market='US' "
          "AND session='REGULAR' AND substr(ts_exchange,12,5) >= ?")
# Without ANALYZE stats the planner picks the (symbol, interval) PK prefix and scans a symbol's whole
# history per query (~1 s each on the 26M-row DB); INDEXED BY pins the per-day index (~10 ms).
_COUNT_SQL = f"SELECT COUNT(*) FROM candle_history INDEXED BY ix_hist_symbol_day WHERE {_WHERE}"
_UPDATE_SQL = (f"UPDATE candle_history INDEXED BY ix_hist_symbol_day SET session='POST', updated_at=? "
               f"WHERE {_WHERE}")
# Independent check: PK range on ts_utc (no INDEXED BY, no _WHERE); session/market/time judged in Python.
_VERIFY_SQL = ("SELECT ts_exchange, session, market, updated_at FROM candle_history "
               "WHERE symbol=? AND interval='1m' AND ts_utc>=? AND ts_utc<?")
VERIFY_SAMPLE = 200


def early_close_dates() -> list[str]:
    return [d.isoformat() for d in cal.early_close_days(cal.CALENDAR_START, cal.CALENDAR_END)]


def count_targets(con: sqlite3.Connection, symbols: list[str], dates: list[str]) -> dict[str, dict[str, int]]:
    """{date: {symbol: n}} for symbols with n > 0."""
    out: dict[str, dict[str, int]] = {}
    for day in dates:
        per = {}
        for sym in symbols:
            n = con.execute(_COUNT_SQL, (sym, day, CUTOFF_HHMM)).fetchone()[0]
            if n:
                per[sym] = int(n)
        out[day] = per
    return out


def _utc_iso(d: date, t: dtime) -> str:
    return datetime.combine(d, t, tzinfo=ET).astimezone(timezone.utc).isoformat()


def independent_verify(con: sqlite3.Connection, symbols: list[str], dates: list[str], updated_at: str) -> dict:
    """Remaining REGULAR rows at/after 13:00 ET on early-close dates, and classify_session on updated rows."""
    remaining = 0
    updated_seen = 0
    sample_checked = 0
    sample_mismatch: list[str] = []
    for day in dates:
        d = date.fromisoformat(day)
        lo = _utc_iso(d, dtime(13, 0))
        hi = _utc_iso(d + timedelta(days=1), dtime(0, 0))
        for sym in symbols:
            for ts_ex, session, market, upd in con.execute(_VERIFY_SQL, (sym, lo, hi)):
                local = datetime.fromisoformat(ts_ex).astimezone(ET)
                if market != MARKET_US or local.date() != d or (local.hour, local.minute) < (13, 0):
                    continue
                if session == SESSION_REGULAR:
                    remaining += 1
                if upd == updated_at and session == SESSION_POST:
                    updated_seen += 1
                    if sample_checked < VERIFY_SAMPLE:
                        sample_checked += 1
                        if classify_session(local, MARKET_US) != SESSION_POST:
                            sample_mismatch.append(f"{sym} {ts_ex}")
    return {"remaining_regular_at_or_after_1300": remaining, "updated_rows_seen": updated_seen,
            "classify_sample_checked": sample_checked, "classify_sample_mismatch": sample_mismatch}


def check_backup(path: Path) -> dict:
    """Open the backup read-only; require PRAGMA quick_check == ok and a candle_history table."""
    st = path.stat()
    info: dict = {"path": str(path), "size_bytes": st.st_size,
                  "mtime_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()}
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            result = [r[0] for r in con.execute("PRAGMA quick_check")]
            has_table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                    "AND name='candle_history'").fetchone() is not None
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"backup is not a readable SQLite DB: {exc}") from exc
    if result != ["ok"]:
        raise ValueError(f"backup PRAGMA quick_check failed: {result[:5]}")
    if not has_table:
        raise ValueError("backup has no candle_history table")
    info["quick_check"] = "ok"
    return info


def _totals(counts: dict[str, dict[str, int]]) -> dict[str, int]:
    return {day: sum(per.values()) for day, per in counts.items()}


def _git_commit() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                           check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() or None


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(db: Path, root: Path, *, apply: bool, backup_verified: Path | None,
        backup_info: dict | None = None) -> dict:
    started = time.monotonic()
    started_at = _now_utc()
    dates = early_close_dates()
    receipt: dict = {
        "schema": SCHEMA, "mode": "apply" if apply else "dry_run", "db_path": str(db),
        "backup_verified": str(backup_verified) if backup_verified else None, "backup_check": backup_info,
        "code_commit": _git_commit(), "started_at_utc": started_at,
        "rule": {"market": "US", "interval": "1m", "session_from": "REGULAR", "session_to": "POST",
                 "local_time_at_or_after": CUTOFF_HHMM,
                 "calendar_range": [cal.CALENDAR_START.isoformat(), cal.CALENDAR_END.isoformat()]},
        "early_close_dates": dates,
    }
    if not apply:
        con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            symbols = [r[0] for r in con.execute(_SYMBOLS_SQL)]
            before = count_targets(con, symbols, dates)
        finally:
            con.close()
        receipt.update({"symbols_scanned": len(symbols), "before": before, "before_totals": _totals(before),
                        "target_rows": sum(_totals(before).values()), "updated_rows": 0})
    else:
        with market_db_lock(root):
            con = sqlite3.connect(str(db), isolation_level=None)
            try:
                con.execute("BEGIN IMMEDIATE")
                try:
                    symbols = [r[0] for r in con.execute(_SYMBOLS_SQL)]
                    before = count_targets(con, symbols, dates)
                    now = _now_utc()
                    updated = 0
                    for day, per in before.items():
                        for sym in per:
                            updated += con.execute(_UPDATE_SQL, (now, sym, day, CUTOFF_HHMM)).rowcount
                    after = count_targets(con, symbols, dates)
                    remaining = sum(_totals(after).values())
                    expected = sum(_totals(before).values())
                    verify = independent_verify(con, symbols, dates, now)
                    if (remaining or updated != expected or verify["remaining_regular_at_or_after_1300"]
                            or verify["updated_rows_seen"] != updated or verify["classify_sample_mismatch"]):
                        raise RuntimeError(f"verification failed: updated={updated} expected={expected} "
                                           f"remaining_regular_at_or_after_1300={remaining} independent={verify}")
                    con.execute("COMMIT")
                except BaseException:
                    if con.in_transaction:
                        con.execute("ROLLBACK")
                    raise
            finally:
                con.close()
        receipt.update({"symbols_scanned": len(symbols), "before": before, "before_totals": _totals(before),
                        "target_rows": expected, "updated_rows": updated, "updated_at_utc": now,
                        "after_totals": _totals(after), "remaining_regular_at_or_after_1300": remaining,
                        "independent_verification": verify})
    receipt["finished_at_utc"] = _now_utc()
    receipt["run_seconds"] = round(time.monotonic() - started, 3)
    return receipt


def receipt_path(root: Path, receipt: dict) -> Path:
    # e.g. 2026-10-15T08:47:29.123456+00:00 -> 20261015T084729_123456 (write-once, no overwrite)
    stamp = receipt["started_at_utc"].split("+")[0].replace(":", "").replace("-", "").replace(".", "_")
    return data_ops_dir(root) / f"relabel_early_close_sessions_{stamp}Z.json"


def write_receipt(path: Path, receipt: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as fh:
        json.dump(receipt, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="조기폐장일 13:00 ET 이후 REGULAR 1분봉 → POST 재라벨")
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "market_history.sqlite")
    ap.add_argument("--root", type=Path, default=ROOT, help="lock 과 reports/data-ops 의 기준 저장소 루트")
    ap.add_argument("--apply", action="store_true", help="실제로 쓴다(없으면 dry-run)")
    ap.add_argument("--backup-verified", type=Path, default=None,
                    help="--apply 필수: 검증을 마친 DB 백업 파일 경로(실재해야 함)")
    ap.add_argument("--allow-db-outside-root", action="store_true",
                    help="--db 가 --root/data 밖이어도 허용")
    args = ap.parse_args(argv)
    if not args.db.is_file():
        ap.error(f"DB 파일이 없다: {args.db}")
    data_dir = (args.root / "data").resolve()
    if not args.allow_db_outside_root and not args.db.resolve().is_relative_to(data_dir):
        ap.error(f"--db {args.db} 가 --root/data({data_dir}) 아래가 아니다(--allow-db-outside-root 로만 허용)")
    backup_info = None
    if args.apply:
        if args.backup_verified is None or not args.backup_verified.is_file():
            ap.error("--apply 는 실재하는 파일을 가리키는 --backup-verified <path> 가 필요하다")
        if args.backup_verified.resolve() == args.db.resolve():
            ap.error("--backup-verified 가 대상 DB 자체다")
        try:
            backup_info = check_backup(args.backup_verified)
        except (OSError, ValueError) as exc:
            ap.error(f"--backup-verified 검증 실패: {exc}")
    receipt = run(args.db, args.root, apply=args.apply, backup_verified=args.backup_verified,
                  backup_info=backup_info)
    path = receipt_path(args.root, receipt) if args.apply else None
    if path is not None:
        receipt["receipt_path"] = str(path)
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
    if path is not None:
        try:
            write_receipt(path, receipt)
        except OSError as exc:
            print(f"receipt write failed (DB change already committed): {exc}", file=sys.stderr, flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
