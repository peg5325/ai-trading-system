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
from src.history import exchange_calendar as _cal  # noqa: E402
from src.history.raw_snapshot import RawSnapshotStore  # noqa: E402
from src.history.session_filter import SessionFilter  # noqa: E402
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
    ap.add_argument("--sessions", default=None,
                    help="1분봉 저장 세션(쉼표, 저장 전 필터). 기본 PRE,REGULAR,POST. "
                         "OVERNIGHT 을 포함하려면 --all-sessions 로 명시 opt-in. 1d 엔 미적용")
    ap.add_argument("--all-sessions", action="store_true",
                    help="1분봉 전 세션 저장(필터 OFF) — 명시적 opt-in(계약 §2.2)")
    ap.add_argument("--raw-snapshot-dir", default=None,
                    help="immutable raw 페이지 스냅샷 저장 디렉터리(gitignore 위치 권장, 예 "
                         "data/raw_snapshots). 지정 시 parse 전 응답 payload 를 content-addressed 보존")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    max_pages = 5 if args.probe else args.max_pages

    # --- 순수 인자 검증(자격증명 불필요, creds 확인보다 먼저) ---
    # 세션 필터: 1분봉 전용(일봉은 00:00 ET → OVERNIGHT 라벨이라 필터 부적합). 1d 에 세션 옵션
    # 지정 시 명시적 오류로 중단(조용한 오적용 방지).
    session_filter = None
    if args.interval == "1m":
        try:
            session_filter = SessionFilter.from_spec(args.sessions,
                                                     all_sessions=args.all_sessions)
        except ValueError as e:
            # 오타/모순 세션 스펙 → traceback 대신 명확한 종료(1d 오적용 처리와 일관).
            print(f"세션 옵션 오류: {e}", file=sys.stderr)
            return 2
    elif args.sessions is not None or args.all_sessions:
        print("--sessions/--all-sessions 는 1분봉(1m)에만 적용된다(1d 미지원).", file=sys.stderr)
        return 2

    # gap-skip 예산 안전 점검(계약 §2.4): 거래소 캘린더 최장 비거래 span 을 커버하지 못하는
    # --max-consecutive-empty 는 연말/롱위켄드에서 pagination 이 조기 종료될 수 있다 → 경고.
    if args.gap_skip_minutes > 0:
        recommended = _cal.recommended_max_consecutive_empty(
            args.gap_skip_minutes, _cal.CALENDAR_START, _cal.CALENDAR_END)
        if args.max_consecutive_empty < recommended:
            print(f"⚠️  --max-consecutive-empty={args.max_consecutive_empty} 는 캘린더 최장 "
                  f"비거래 span 커버 권장값 {recommended}(gap_skip={args.gap_skip_minutes}m) 미만 "
                  f"— 장기 휴장에서 조기 종료 위험. 권장값 이상으로 올리세요.", file=sys.stderr)

    settings = load_settings()
    if not settings.has_credentials:
        print(".env 의 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 이 비어있습니다.", file=sys.stderr)
        return 2

    raw_store = RawSnapshotStore(args.raw_snapshot_dir) if args.raw_snapshot_dir else None
    raw_sink = raw_store.emit if raw_store is not None else None

    with TossMarketDataProvider(settings) as prov, HistoryStore(args.db) as store:
        report = backfill_symbols(
            store, prov, symbols, args.interval,
            max_pages=max_pages, page_size=args.page_size, spacing=args.spacing,
            max_retries=args.max_retries, backoff_base=args.backoff_base,
            resume=not args.no_resume, gap_skip_minutes=args.gap_skip_minutes,
            max_consecutive_empty=args.max_consecutive_empty,
            session_filter=session_filter, raw_sink=raw_sink)

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return 0

    mode = "프로브(깊이 측정)" if args.probe else "본 backfill"
    print(f"=== Toss backfill [{mode}] {args.db}  interval={args.interval} ===")
    if session_filter is not None:
        print(f"  세션 필터: allowed={sorted(session_filter.allowed)} "
              f"all_sessions={session_filter.all_sessions}")
    if raw_store is not None:
        print(f"  raw 스냅샷: {raw_store.root}")
    for r in report.results:
        depth = f"{r.earliest} ~ {r.latest}" if r.earliest else "(수집 없음)"
        flag = "EXHAUSTED" if r.exhausted else (r.stopped_reason or "")
        print(f"\n### {r.symbol} [{r.interval}]")
        print(f"  pages={r.pages_fetched} rows_seen={r.rows_seen} "
              f"stored={r.rows_stored} inserted={r.inserted} dup={r.duplicate} conflict={r.conflict}")
        if r.excluded_by_session:
            print(f"  세션 제외: {dict(sorted(r.excluded_by_session.items()))}")
        print(f"  깊이(UTC): {depth}")
        print(f"  정지사유: {r.stopped_reason}  [{flag}]")
        if r.error:
            print(f"  ⚠️ error: {r.error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
