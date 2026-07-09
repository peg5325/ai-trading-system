"""기존 session candle DB 를 통합 저장소로 병합(read-only import + idempotent upsert).

사용:
  ./.venv/bin/python scripts/history_import_sessions.py \
      --db data/market_history.sqlite --src-glob 'data/market_*.sqlite'

- 소스 DB 는 mode=ro 로만 읽는다(원본 불변). Toss API 호출 없음.
- PK(symbol,interval,ts_utc) 충돌: 값 동일=no-op, 다르면 우선순위(adjusted>backfill) + import_conflict 로그.
- 재실행 안전(멱등): 이미 있는 봉은 duplicate 로 집계, 신규 insert 0.
설계: docs/MARKET_HISTORY_BACKFILL_DESIGN.md
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history import HistoryStore  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="session DB → market_history 병합")
    ap.add_argument("--db", default="data/market_history.sqlite")
    ap.add_argument("--src-glob", default="data/market_*.sqlite")
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args(argv)

    with HistoryStore(args.db) as store:
        results = store.import_glob(args.src_glob, run_id=args.run_id)
        tot = dict(seen=0, inserted=0, updated=0, duplicate=0, conflict=0)
        print(f"[import] target={args.db}  src_glob={args.src_glob}")
        print(f"[import] {'source':<44}{'seen':>8}{'ins':>8}{'upd':>7}{'dup':>8}{'conf':>6}")
        for s in results:
            print(f"[import] {s.source:<44}{s.rows_seen:>8}{s.inserted:>8}{s.updated:>7}"
                  f"{s.duplicate:>8}{s.conflict:>6}"
                  + (f"  ERR:{s.errors}" if s.errors else ""))
            tot["seen"] += s.rows_seen; tot["inserted"] += s.inserted
            tot["updated"] += s.updated; tot["duplicate"] += s.duplicate
            tot["conflict"] += s.conflict
        print(f"[import] {'TOTAL':<44}{tot['seen']:>8}{tot['inserted']:>8}"
              f"{tot['updated']:>7}{tot['duplicate']:>8}{tot['conflict']:>6}")
        print(f"[import] unified total rows: {store.total_rows()}")
        print("[import] === symbol coverage ===")
        for c in store.symbol_coverage():
            print(f"  {c['market']} {c['symbol']:<8} [{c['interval']}] "
                  f"n={c['n']} days={c['days']}  {c['earliest']} ~ {c['latest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
