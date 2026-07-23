"""partial→final 감사 remediation CLI(이슈 #72) — 기본 dry-run, 삭제는 명시적 execute 필요.

first-write-wins 를 완화하지 않고, 잔존 partial 봉을 **감사 가능한 삭제 후 재수집**으로만 교체한다.
이 CLI 는 삭제(백업·무결성·영수증)만 담당한다. 재수집(refetch)은 삭제 후
`scripts/history_backfill_official.py --completed-through …` 를 재실행해 완성봉으로 채운다.

안전:
- 기본은 dry-run(무변경) — 현재 대상 행수·content hash 만 보고한다.
- 실제 삭제는 `--execute` + `--expected-rows` + `--expected-hash` 가 모두 필요하며, 현재 대상이
  예상값과 다르면 삭제 전에 거부(fail-closed)한다.
- `--db` 는 필수(기본 canonical 경로를 두지 않아 우발적 canonical 삭제를 막는다).

사용:
  # 1) dry-run 으로 현재 대상 행수·hash 확인(무변경)
  ./.venv/bin/python scripts/history_partial_remediation.py \
      --db /tmp/scratch.sqlite --symbol QQQ --interval 1m --days 2026-07-22 \
      --backup-dir /tmp/bk
  # 2) 위 hash/행수를 그대로 넣어 감사 삭제(백업·무결성·영수증)
  ./.venv/bin/python scripts/history_partial_remediation.py \
      --db /tmp/scratch.sqlite --symbol QQQ --interval 1m --days 2026-07-22 \
      --backup-dir /tmp/bk --receipt /tmp/receipts.jsonl \
      --execute --expected-rows 2 --expected-hash <content_hash>
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.partial_remediation import (  # noqa: E402
    RemediationError,
    RemediationPlan,
    identify_partial,
    remediate,
)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="partial→final 감사 remediation(기본 dry-run, first-write-wins 완화 없음)")
    ap.add_argument("--db", required=True, help="대상 DB(필수). canonical 은 이번 작업 범위 밖.")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--interval", required=True, choices=["1m", "1d"])
    ap.add_argument("--days", required=True,
                    help="거래소-로컬 일자(YYYY-MM-DD) 쉼표 구분 — 이 범위만 대상")
    ap.add_argument("--backup-dir", required=True, help="실제 삭제 시 안전 백업 저장 디렉터리")
    ap.add_argument("--receipt", default=None, help="감사 receipt JSONL 경로(append)")
    ap.add_argument("--execute", action="store_true",
                    help="실제 삭제(기본 dry-run). --expected-rows/--expected-hash 필수")
    ap.add_argument("--expected-rows", type=int, default=None)
    ap.add_argument("--expected-hash", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    days = [d.strip() for d in args.days.split(",") if d.strip()]
    if not days:
        print("--days 가 비었습니다(YYYY-MM-DD 쉼표 구분).", file=sys.stderr)
        return 2

    info = identify_partial(args.db, args.symbol, args.interval, days)
    if not args.execute:
        # dry-run: 현재 대상 상태 보고(이 행수·hash 를 execute 의 expected 로 넣으면 된다).
        r = remediate(args.db, RemediationPlan(args.symbol, args.interval, tuple(days),
                                               expected_row_count=args.expected_rows,
                                               expected_content_hash=args.expected_hash),
                      backup_dir=args.backup_dir, execute=False, receipt_path=args.receipt)
        if args.json:
            print(json.dumps({"identify": info, "dry_run": r}, ensure_ascii=False, indent=2))
        else:
            print(f"[dry-run] {args.symbol}/{args.interval} days={days}")
            print(f"  현재 대상 행수: {info['row_count']}  content_hash: {info['content_hash']}")
            print(f"  실제 삭제하려면: --execute --expected-rows {info['row_count']} "
                  f"--expected-hash {info['content_hash']}")
        return 0

    if args.expected_rows is None or args.expected_hash is None:
        print("--execute 삭제에는 --expected-rows 와 --expected-hash 가 필수다"
              "(먼저 dry-run 으로 확인).", file=sys.stderr)
        return 2
    plan = RemediationPlan(args.symbol, args.interval, tuple(days),
                           expected_row_count=args.expected_rows,
                           expected_content_hash=args.expected_hash)
    try:
        r = remediate(args.db, plan, backup_dir=args.backup_dir, execute=True,
                      receipt_path=args.receipt)
    except RemediationError as e:
        print(f"remediation 거부/rollback: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(f"[executed] status={r['status']} deleted={r.get('deleted')} "
              f"backup={r.get('backup_path')}")
        print(f"  post_row_count={r['post_row_count']}  refetch: {r.get('refetch_hint')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
