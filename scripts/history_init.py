"""통합 히스토리 저장소 초기화 — market_history.sqlite 스키마 생성(멱등).

사용:
  ./.venv/bin/python scripts/history_init.py --db data/market_history.sqlite

Toss API 호출 없음. 기존 session DB 를 건드리지 않는다(신규 DB 만 생성).
설계: docs/MARKET_HISTORY_BACKFILL_DESIGN.md
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history import HistoryStore  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="market_history.sqlite 초기화(DDL, 멱등)")
    ap.add_argument("--db", default="data/market_history.sqlite")
    args = ap.parse_args(argv)

    existed = os.path.exists(args.db)
    with HistoryStore(args.db) as store:
        print(f"[history_init] {'열림(기존)' if existed else '생성'}: {args.db}")
        print(f"[history_init] total rows: {store.total_rows()}")
        print("[history_init] === schema ===")
        print(store.schema_sql())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
