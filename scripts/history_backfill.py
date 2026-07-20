"""Toss 과거 캔들 backfill → 통합 저장소 market_history.sqlite (PR 3-A).

주문 없음. 시장데이터 read + 저장만. cursor pagination·resume·429 backoff·종목 격리.

사용:
  # 프로브(실제 보존 깊이 측정, 종목당 최대 5페이지)
  ./.venv/bin/python scripts/history_backfill.py --db data/market_history.sqlite \
      --symbols QQQ,SOXL --interval 1m --probe
  # 본 backfill (nextBefore 소진까지, spacing 으로 rate-limit 보호)
  ./.venv/bin/python scripts/history_backfill.py --db data/market_history.sqlite \
      --symbols 005930,000660,SOXL,QQQ,NVDA --interval 1m --max-pages 200 --spacing 0.5

benchmark QQQ 는 기본 종목에 포함(Phase 4 Shadow 국면 히스토리 소비).
설계: docs/MARKET_HISTORY_BACKFILL_DESIGN.md
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_settings  # noqa: E402
from src.history import HistoryStore  # noqa: E402
from src.ingest.backfill_history import backfill_symbols  # noqa: E402
from src.providers.toss_market_data import TossMarketDataProvider  # noqa: E402

# benchmark QQQ 포함(Phase 4 Shadow). US/KR 대표 종목 기본 세트.
DEFAULT_SYMBOLS = "QQQ,SOXL,NVDA,005930,000660"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Toss 과거 캔들 backfill(read-only, 주문 없음)")
    ap.add_argument("--db", default="data/market_history.sqlite")
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS,
                    help="쉼표 구분 종목. 기본 세트는 benchmark QQQ 포함")
    ap.add_argument("--interval", default="1m", choices=["1m", "1d"])
    ap.add_argument("--max-pages", type=int, default=200,
                    help="종목당 최대 페이지(호출 제한 보호). nextBefore 소진 시 자연 정지")
    ap.add_argument("--page-size", type=int, default=200)
    ap.add_argument("--spacing", type=float, default=0.5,
                    help="페이지/종목 간 sleep(초) — rate-limit 보호")
    ap.add_argument("--max-retries", type=int, default=3, help="429 지수 backoff 재시도 횟수")
    ap.add_argument("--backoff-base", type=float, default=0.5)
    ap.add_argument("--probe", action="store_true",
                    help="프로브 모드: max_pages=5 로 실제 보존 깊이만 측정")
    ap.add_argument("--no-resume", action="store_true", help="커서 무시하고 처음부터 재스캔")
    ap.add_argument("--gap-skip-minutes", type=int, default=0,
                    help="빈 페이지/nextBefore=null 을 '소진'이 아닌 'UTC 일자 경계'로 보고 "
                         "이만큼 과거로 건너뛰며 계속 페이징(0=비활성, 권장 200=page-size)")
    ap.add_argument("--max-consecutive-empty", type=int, default=40,
                    help="gap-skip 시 연속 빈 창 허용 횟수(초과하면 진짜 소진으로 판정)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    settings = load_settings()
    if not settings.has_credentials:
        print(".env 의 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 이 비어있습니다.", file=sys.stderr)
        return 2

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    max_pages = 5 if args.probe else args.max_pages

    with TossMarketDataProvider(settings) as prov, HistoryStore(args.db) as store:
        report = backfill_symbols(
            store, prov, symbols, args.interval,
            max_pages=max_pages, page_size=args.page_size, spacing=args.spacing,
            max_retries=args.max_retries, backoff_base=args.backoff_base,
            resume=not args.no_resume, gap_skip_minutes=args.gap_skip_minutes,
            max_consecutive_empty=args.max_consecutive_empty)

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return 0

    mode = "프로브(깊이 측정)" if args.probe else "본 backfill"
    print(f"=== Toss backfill [{mode}] {args.db}  interval={args.interval} ===")
    for r in report.results:
        depth = f"{r.earliest} ~ {r.latest}" if r.earliest else "(수집 없음)"
        flag = "EXHAUSTED" if r.exhausted else (r.stopped_reason or "")
        print(f"\n### {r.symbol} [{r.interval}]")
        print(f"  pages={r.pages_fetched} rows_seen={r.rows_seen} "
              f"inserted={r.inserted} dup={r.duplicate} conflict={r.conflict}")
        print(f"  깊이(UTC): {depth}")
        print(f"  정지사유: {r.stopped_reason}  [{flag}]")
        if r.error:
            print(f"  ⚠️ error: {r.error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
