"""공식 P0-US30 과거 캔들 backfill — **완료-거래일 cutoff 강제**(read-only 시세, 주문 없음).

이슈 #72. 이 스크립트는 **공식 스냅샷 수집 경로**다. 비공식/프로브 실험은
`scripts/history_backfill.py`(안전장치 없이 실행 가능, 공식 스냅샷 아님)를 쓴다.

공식 경로의 fail-closed 계약:
- `--completed-through YYYY-MM-DD` 가 **필수**다. 없으면 자격증명/ API/DB 접근 전에 exit 2.
- 요청 cutoff 가 실행 시점 NYSE 의 마지막 **완료** 거래일보다 늦으면(진행 중/미래 세션) exit 2.
  · 정상일 16:00 ET / 조기폐장일 13:00 ET / 주말·휴장·DST 를 exchange_calendar + tz-aware ET 로 판정.
  · 현재시각은 주입 가능(main(argv, now_utc=...)) — 코드 내부에서 임의 고정하지 않는다.
- 유니버스는 `config/universe_p0_us30.yaml`(market=US)에서 읽는다. 심볼은 P0-US30(US) 집합이어야
  하며 그 밖(예: 코스피 코드)은 거부한다 — US 외 시장을 NYSE 규칙으로 추측하지 않는다.

저장 순서(계약 #72): provider raw immutable snapshot → parse → **완료-거래일 cutoff** →
  SessionFilter(PRE/REGULAR/POST) → canonical upsert. cutoff 제외 봉도 raw 스냅샷엔 남는다.

사용(공식 실행 예):
  # 30종목 3년 1분봉(세션 PRE/REGULAR/POST, raw 스냅샷 보존)
  ./.venv/bin/python scripts/history_backfill_official.py \
      --db data/market_history.sqlite --interval 1m \
      --completed-through 2026-07-21 \
      --raw-snapshot-dir data/raw_snapshots --spacing 0.5 --gap-skip-minutes 200
  # 30종목 10년 일봉
  ./.venv/bin/python scripts/history_backfill_official.py \
      --db data/market_history.sqlite --interval 1d --completed-through 2026-07-21
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_settings  # noqa: E402
from src.history import HistoryStore  # noqa: E402
from src.history import exchange_calendar as _cal  # noqa: E402
from src.history.completed_session import (  # noqa: E402
    CompletedThroughError,
    completion_scope_for_sessions,
    parse_completed_through,
    validate_completed_through,
)
from src.history.daily_manifest import read_p0_universe  # noqa: E402
from src.history.raw_snapshot import RawSnapshotStore  # noqa: E402
from src.history.session_filter import SessionFilter  # noqa: E402
from src.ingest.backfill_history import backfill_symbols  # noqa: E402
from src.providers.toss_market_data import TossMarketDataProvider  # noqa: E402

DEFAULT_YAML = "config/universe_p0_us30.yaml"


def main(argv: list[str], *, now_utc: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="공식 P0-US30 backfill(완료-거래일 cutoff 강제, read-only, 주문 없음)")
    ap.add_argument("--completed-through", default=None,
                    help="필수: 완료-거래일 경계 YYYY-MM-DD. 이 일자 이하 거래일만 저장하고 "
                         "이후(진행 중/미완성)는 제외한다. 마지막 완료 거래일보다 늦으면 거부.")
    ap.add_argument("--db", default="data/market_history.sqlite")
    ap.add_argument("--yaml", default=DEFAULT_YAML,
                    help="P0-US30 유니버스 YAML(정본, market=US)")
    ap.add_argument("--symbols", default=None,
                    help="쉼표 구분 심볼(기본=유니버스 전체 30종목). 각 심볼은 P0-US30(US) 이어야 함")
    ap.add_argument("--interval", default="1m", choices=["1m", "1d"])
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--page-size", type=int, default=200)
    ap.add_argument("--spacing", type=float, default=0.5, help="페이지/종목 간 sleep(초)")
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--backoff-base", type=float, default=0.5)
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--gap-skip-minutes", type=int, default=0,
                    help="빈 페이지/nextBefore=null 을 UTC 일자 경계로 보고 이만큼 과거로 건너뜀")
    ap.add_argument("--max-consecutive-empty", type=int, default=40)
    ap.add_argument("--sessions", default=None,
                    help="1분봉 저장 세션(쉼표). 기본 PRE,REGULAR,POST. 1d 엔 미적용")
    ap.add_argument("--all-sessions", action="store_true",
                    help="1분봉 전 세션 저장(필터 OFF) — 명시적 opt-in")
    ap.add_argument("--raw-snapshot-dir", default=None,
                    help="immutable raw 페이지 스냅샷 저장 디렉터리(gitignore 위치 권장)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    now_utc = now_utc or datetime.now(timezone.utc)

    # === 1) 완료-거래일 cutoff 필수 + 형식(자격증명/ API/DB 접근 전 fail-closed) ===
    if args.completed_through is None:
        print("공식 P0-US30 수집은 --completed-through YYYY-MM-DD 가 필수다(fail-closed). "
              "비공식 실험은 scripts/history_backfill.py 를 쓰라.", file=sys.stderr)
        return 2
    try:
        requested = parse_completed_through(args.completed_through)
    except ValueError:
        print(f"--completed-through 형식 오류(YYYY-MM-DD 필요): {args.completed_through!r}",
              file=sys.stderr)
        return 2

    # === 2) 세션 필터(1분봉 전용) — 순수 인자 검증. 완료 판정 scope 산출에 필요. ===
    session_filter = None
    if args.interval == "1m":
        try:
            session_filter = SessionFilter.from_spec(args.sessions,
                                                     all_sessions=args.all_sessions)
        except ValueError as e:
            print(f"세션 옵션 오류: {e}", file=sys.stderr)
            return 2
    elif args.sessions is not None or args.all_sessions:
        print("--sessions/--all-sessions 는 1분봉(1m)에만 적용된다(1d 미지원).", file=sys.stderr)
        return 2

    # === 3) cutoff 완료 검증 — 수집 세션 범위(scope)에 맞춘 완료 경계로 판정 ===
    #   POST 를 기본 수집하는 1m 은 애프터마켓(20:00/17:00 ET)까지 끝나야 그 날이 완료다.
    scope = completion_scope_for_sessions(
        args.interval, session_filter.allowed if session_filter else None)
    try:
        completed_through = validate_completed_through(requested, now_utc,
                                                       session_scope=scope)
    except (CompletedThroughError, _cal.CalendarRangeError) as e:
        print(f"--completed-through 거부: {e}", file=sys.stderr)
        return 2

    # === 4) 유니버스 로드 + US-only + P0-US30 소속 검증(비 US 시장 추측 금지) ===
    uni = read_p0_universe(args.yaml)
    if uni["market"] != "US":
        print(f"유니버스 시장이 US 가 아님({uni['market']}) — 공식 경로는 US(NYSE) 전용.",
              file=sys.stderr)
        return 2
    universe = list(uni["symbols"])
    universe_set = set(universe)
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        unknown = sorted(set(symbols) - universe_set)
        if unknown:
            print(f"P0-US30(US) 유니버스 밖 심볼 거부: {unknown} — 비공식 경로"
                  f"(scripts/history_backfill.py)를 쓰라.", file=sys.stderr)
            return 2
    else:
        symbols = universe

    # gap-skip 예산 안전 점검(장기 휴장 조기 종료 경고).
    if args.gap_skip_minutes > 0:
        recommended = _cal.recommended_max_consecutive_empty(
            args.gap_skip_minutes, _cal.CALENDAR_START, _cal.CALENDAR_END)
        if args.max_consecutive_empty < recommended:
            print(f"⚠️  --max-consecutive-empty={args.max_consecutive_empty} < 권장 {recommended}"
                  f"(gap_skip={args.gap_skip_minutes}m) — 장기 휴장 조기 종료 위험.", file=sys.stderr)

    # === 4) 자격증명(검증을 모두 통과한 뒤에만) ===
    settings = load_settings()
    if not settings.has_credentials:
        print(".env 의 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 이 비어있습니다.", file=sys.stderr)
        return 2

    raw_store = RawSnapshotStore(args.raw_snapshot_dir) if args.raw_snapshot_dir else None
    raw_sink = raw_store.emit if raw_store is not None else None
    max_pages = args.max_pages

    with TossMarketDataProvider(settings) as prov, HistoryStore(args.db) as store:
        report = backfill_symbols(
            store, prov, symbols, args.interval,
            max_pages=max_pages, page_size=args.page_size, spacing=args.spacing,
            max_retries=args.max_retries, backoff_base=args.backoff_base,
            resume=not args.no_resume, gap_skip_minutes=args.gap_skip_minutes,
            max_consecutive_empty=args.max_consecutive_empty,
            session_filter=session_filter, completed_through=completed_through,
            raw_sink=raw_sink)

    if args.json:
        out = report.as_dict()
        out["completed_through"] = completed_through.isoformat()
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"=== 공식 P0-US30 backfill {args.db}  interval={args.interval} ===")
    print(f"  완료-거래일 cutoff: {completed_through.isoformat()} "
          f"(scope={scope}, 기준시각 {now_utc.isoformat()})")
    if session_filter is not None:
        print(f"  세션 필터: allowed={sorted(session_filter.allowed)} "
              f"all_sessions={session_filter.all_sessions}")
    if raw_store is not None:
        print(f"  raw 스냅샷: {raw_store.root}")
    excluded_total = 0
    for r in report.results:
        depth = f"{r.earliest} ~ {r.latest}" if r.earliest else "(수집 없음)"
        excluded_total += r.rows_excluded_after_cutoff
        print(f"\n### {r.symbol} [{r.interval}]")
        print(f"  pages={r.pages_fetched} rows_seen={r.rows_seen} stored={r.rows_stored} "
              f"inserted={r.inserted} dup={r.duplicate} conflict={r.conflict}")
        if r.rows_excluded_after_cutoff:
            print(f"  cutoff 제외: {r.rows_excluded_after_cutoff}행 "
                  f"days={_day_range_str(r)}")
        if r.excluded_by_session:
            print(f"  세션 제외: {dict(sorted(r.excluded_by_session.items()))}")
        print(f"  깊이(UTC): {depth}")
        print(f"  정지사유: {r.stopped_reason}")
        if r.error:
            print(f"  ⚠️ error: {r.error}")
    print(f"\n총 cutoff 제외 행수: {excluded_total}")
    return 0


def _day_range_str(result) -> str | None:
    days = sorted(result.excluded_after_cutoff_days)
    if not days:
        return None
    return days[0] if len(days) == 1 else f"{days[0]}..{days[-1]}"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
