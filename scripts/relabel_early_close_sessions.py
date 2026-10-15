#!/usr/bin/env python3
"""NYSE 조기폐장일 13:00 ET 이후 REGULAR 1분봉을 POST 로 다시 라벨한다(사용자 결정 2026-10-15).

`classify_session` 은 2026-10-15 부터 조기폐장일 REGULAR 를 09:30–13:00 으로, 13:00–20:00 을 POST 로
라벨한다. 그 전에 저장된 행은 13:00–15:59 가 REGULAR 로 남아 있고, 재수집해도 store 는 기존 값을
유지(first-write-wins)하므로 이 스크립트로 한 번 고친다.

대상: market='US', interval='1m', 날짜 = `exchange_calendar` 조기폐장일, ts_exchange 로컬 시각 >= 13:00,
session='REGULAR' → session='POST', updated_at=지금(UTC). 그 외 행·컬럼은 건드리지 않는다.

안전:
- 기본은 dry-run(읽기 전용 연결, 쓰기 없음). `--apply` 가 있어야 쓴다.
- `--apply` 는 실재하는 파일을 가리키는 `--backup-verified <path>` 가 필요하다(없으면 거부).
- 쓰기 동안 `market_db_lock(root)` 을 잡고, 한 트랜잭션으로 갱신한 뒤 같은 트랜잭션 안에서
  13:00 이후 REGULAR 가 0 건인지 확인한다(아니면 롤백). 다시 돌리면 대상 0 건(멱등).
- 조회는 심볼·날짜별로 `ix_hist_symbol_day`(symbol, interval, substr(ts_exchange,1,10)) 를 탄다.
- receipt(JSON)는 `reports/data-ops/` 에 쓰고 출력한다(--apply 일 때만 파일로 남긴다).
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
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history import exchange_calendar as cal
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


def run(db: Path, root: Path, *, apply: bool, backup_verified: Path | None) -> dict:
    started = time.monotonic()
    started_at = _now_utc()
    dates = early_close_dates()
    receipt: dict = {
        "schema": SCHEMA, "mode": "apply" if apply else "dry_run", "db_path": str(db),
        "backup_verified": str(backup_verified) if backup_verified else None,
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
                    if remaining or updated != expected:
                        raise RuntimeError(f"verification failed: updated={updated} expected={expected} "
                                           f"remaining_regular_at_or_after_1300={remaining}")
                    con.execute("COMMIT")
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
            finally:
                con.close()
        receipt.update({"symbols_scanned": len(symbols), "before": before, "before_totals": _totals(before),
                        "target_rows": expected, "updated_rows": updated, "updated_at_utc": now,
                        "after_totals": _totals(after), "remaining_regular_at_or_after_1300": remaining})
    receipt["finished_at_utc"] = _now_utc()
    receipt["run_seconds"] = round(time.monotonic() - started, 3)
    return receipt


def write_receipt(root: Path, receipt: dict) -> Path:
    out_dir = data_ops_dir(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    # e.g. 2026-10-15T08:47:29.123456+00:00 -> 20261015T084729_123456 (write-once, no overwrite)
    stamp = receipt["started_at_utc"].split("+")[0].replace(":", "").replace("-", "").replace(".", "_")
    path = out_dir / f"relabel_early_close_sessions_{stamp}Z.json"
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
    args = ap.parse_args(argv)
    if not args.db.is_file():
        ap.error(f"DB 파일이 없다: {args.db}")
    if args.apply:
        if args.backup_verified is None or not args.backup_verified.is_file():
            ap.error("--apply 는 실재하는 파일을 가리키는 --backup-verified <path> 가 필요하다")
        if args.backup_verified.resolve() == args.db.resolve():
            ap.error("--backup-verified 가 대상 DB 자체다")
    receipt = run(args.db, args.root, apply=args.apply, backup_verified=args.backup_verified)
    if args.apply:
        receipt["receipt_path"] = str(write_receipt(args.root, receipt))
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
