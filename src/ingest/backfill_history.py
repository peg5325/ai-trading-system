"""Toss 과거 캔들 backfill 드라이버 → 통합 저장소 market_history.sqlite (PR 3-A).

설계: docs/MARKET_HISTORY_BACKFILL_DESIGN.md §4 · 구현순서 4~5단계.

- cursor pagination(fetch_page + nextBefore)로 과거로 페이징하며 HistoryStore 에 멱등 upsert.
- backfill_cursor 테이블로 resume(중단 지점 이어받기). 페이지 단위 커밋 → 내구성.
- rate-limit 429 지수 backoff 재시도 + 종목 격리(한 종목 실패가 다른 종목 backfill 중단 안 함).
- oldest ts 비감소(stale 커서) 감지 시 정지 → 무한루프 차단.

★ 자본 안전: 이 모듈은 **시장데이터 read + 저장만** 한다. 주문/broker/pipeline 을 import 하지 않는다.
   provider 는 fetch_page 만 요구하는 얇은 포트다(실 Toss / 테스트 fake 교체 가능).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol

from src.history.store import HistoryStore, ImportStats
from src.models import Candle

BACKFILL_SOURCE = "toss_backfill"

logger = logging.getLogger(__name__)


def _new_run_id(interval: str) -> str:
    """실행마다 유니크한 run_id — ingest_log/conflict provenance 를 실행별로 구분."""
    return f"backfill-{interval}-{datetime.now(timezone.utc).isoformat()}"


class MarketDataPort(Protocol):
    """backfill 이 요구하는 최소 인터페이스(TossMarketDataProvider.fetch_page 호환)."""

    def fetch_page(self, symbol: str, interval: str, *, count: int = 200,
                   before: str | None = None) -> tuple[list[Candle], str | None]:
        ...


@dataclass
class SymbolBackfillResult:
    symbol: str
    interval: str
    pages_fetched: int = 0
    rows_seen: int = 0
    inserted: int = 0
    updated: int = 0
    duplicate: int = 0
    conflict: int = 0
    earliest: str | None = None
    latest: str | None = None
    exhausted: bool = False
    stopped_reason: str | None = None
    error: str | None = None
    gap_skips: int = 0
    empty_fetches: int = 0      # 빈 응답 호출 수(= pages 예산 밖의 추가 API 호출)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "interval": self.interval,
            "pages_fetched": self.pages_fetched, "rows_seen": self.rows_seen,
            "inserted": self.inserted, "updated": self.updated,
            "duplicate": self.duplicate, "conflict": self.conflict,
            "earliest": self.earliest, "latest": self.latest,
            "exhausted": self.exhausted, "stopped_reason": self.stopped_reason,
            "error": self.error, "gap_skips": self.gap_skips,
            "empty_fetches": self.empty_fetches,
        }


@dataclass
class BackfillReport:
    interval: str
    results: list[SymbolBackfillResult] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"interval": self.interval, "results": [r.as_dict() for r in self.results]}


def _status_code(exc: Exception) -> int | None:
    """httpx.HTTPStatusError(.response.status_code) 및 fake(.status_code) 양쪽 지원."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    if code is None:
        code = getattr(exc, "status_code", None)
    return code


def _shift_before(before: str, minutes: int) -> str:
    """`before` 커서를 minutes 만큼 과거로 이동하되 **UTC 일자 경계를 넘지 않는다**.

    Toss 는 오프셋 표기(+09:00)로 nextBefore 를 주므로 UTC 로 정규화한다.

    ★ 경계 클램프가 없으면 데이터가 유실된다. 빈 응답은 "요청창 전체가 비었다"가 아니라
      **"before 가 속한 UTC 일자 안에서 그 이전이 비었다"** 만 확인해 준다(API 가 일자
      경계에서 응답을 자른다). 그러므로 경계를 넘어 통째로 건너뛰면 **직전 일자의 꼬리
      구간을 조회 없이 버린다.**

      실측(수정 전 수집분): `before=D 00:00Z` 빈 응답 → 200분 점프 → `D-1 20:40Z` 로
      이동하면서 `D-1 20:41~23:59Z` 가 매 경계마다 유실됐다. SOXL 시간대별 분봉 수에서
      00~19시는 36일치가 다 있는데 21~23시만 9~10일치뿐이었다.

      그래서 경계를 만나면 200분을 건너뛰지 않고 **직전 일자의 마지막 분(23:59Z)** 으로만
      이동해 그 일자의 꼬리부터 정상 페이징을 재개한다. 같은 일자 안에서의 이동은
      빈 응답이 실제로 그 구간을 확인해 준 것이므로 그대로 안전하다.

      부수 효과로 `00:00Z` 조회(계약상 항상 빈 응답)를 건너뛰어 낭비 호출도 사라진다.
    """
    dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if dt <= day_start:
        # 이미 일자 시작 → 직전 일자의 마지막 분으로.
        return (day_start - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    target = dt - timedelta(minutes=minutes)
    if target <= day_start:
        # 경계를 넘거나 정확히 걸침 → 직전 일자 마지막 분까지만.
        return (day_start - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return target.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_with_retry(provider: MarketDataPort, symbol: str, interval: str, *,
                      count: int, before: str | None, max_retries: int,
                      backoff_base: float, sleep: Callable[[float], None]):
    """429 는 지수 backoff(base*2^attempt)로 재시도. 그 외 예외/한도초과는 상위로 전파."""
    attempt = 0
    while True:
        try:
            return provider.fetch_page(symbol, interval, count=count, before=before)
        except Exception as exc:  # noqa: BLE001 — 상태코드로 재시도 여부 판정
            if _status_code(exc) == 429 and attempt < max_retries:
                sleep(backoff_base * (2 ** attempt))
                attempt += 1
                continue
            raise


def backfill_symbol(store: HistoryStore, provider: MarketDataPort, symbol: str,
                    interval: str, *, max_pages: int, page_size: int = 200,
                    spacing: float = 0.0, sleep: Callable[[float], None] = time.sleep,
                    max_retries: int = 3, backoff_base: float = 0.5,
                    resume: bool = True, run_id: str | None = None,
                    gap_skip_minutes: int = 0,
                    max_consecutive_empty: int = 40) -> SymbolBackfillResult:
    """한 종목을 과거로 backfill. 페이지마다 upsert + cursor 저장 + 커밋.

    gap_skip_minutes > 0 이면 **gap-skip 모드**(opt-in). Toss 1m 캔들 API 는
    `before` 기준 [before - count*interval, before] 구간만 돌려주고 **UTC 일자 경계를
    넘지 않는다**. 따라서 00:01Z 까지 페이징하면 nextBefore=null / 빈 페이지가 되어
    (실제로는 더 과거 데이터가 있는데도) 종목이 하루치에서 영구 정지한다.

    gap-skip 은 그 두 신호를 **종료가 아니라 경계**로 해석한다:
      - 데이터가 있는데 nextBefore=null  → before = oldest - 1min 로 경계를 넘어 계속
      - 빈 페이지                        → `_shift_before` 로 과거로 이동해 계속

    이동은 **UTC 일자 경계를 넘지 않는다**(`_shift_before` 참조). 빈 응답이 확인해 주는
    범위는 `before` 가 속한 일자 안쪽뿐이라, 경계를 넘어 건너뛰면 직전 일자의 꼬리를
    조회 없이 버린다(실측 유실: 매 경계마다 21:00~23:59Z). 같은 일자 안에서의 이동만
    "확인된 공백"으로 취급한다.

    연속 빈 페이지가 max_consecutive_empty 를 넘으면 진짜 소진으로 보고 정지한다.
    경계 클램프 때문에 하루가 통째로 비면 최대 `1440/gap_skip_minutes + 1` 회(200분 기준
    약 8회)의 빈 응답을 소비하므로, 기본 40 은 **약 5일**의 연속 휴장을 커버한다.

    ⚠ 한계: 그보다 긴 휴장(연말연시 등)을 만나면 `exhausted=True` 로 멈추고, exhausted
    커서는 다음 실행에서 처음부터 재스캔하므로 **같은 지점에서 다시 멈춘다** — 그 이전
    구간에는 영구히 닿지 못한다. 그런 구간이 필요하면 `max_consecutive_empty` 를 늘려
    재실행해야 한다(빈 응답은 `pages` 예산과 별도로 계수되지만 `spacing` 은 적용된다).

    gap_skip_minutes = 0(기본)이면 기존 동작(빈 페이지/nextBefore=null → exhausted) 유지.
    """
    run_id = run_id or _new_run_id(interval)
    stats = ImportStats(source=BACKFILL_SOURCE)
    result = SymbolBackfillResult(symbol=symbol, interval=interval)

    # resume: 미완(exhausted=False) 커서가 있으면 next_before 부터 + prev_oldest 시드(재개
    # 첫 페이지에서 즉시 no_progress 판정 가능). 완료/stalled 커서는 처음부터 재스캔.
    before: str | None = None
    prev_oldest: datetime | None = None
    if resume:
        cur = store.get_cursor(symbol, interval)
        if cur and not cur["exhausted"] and cur["next_before"]:
            before = cur["next_before"]
            if cur["oldest_ts_utc"]:
                prev_oldest = datetime.fromisoformat(cur["oldest_ts_utc"])

    pages = 0
    consecutive_empty = 0
    try:
        while pages < max_pages:
            candles, nxt = _fetch_with_retry(
                provider, symbol, interval, count=page_size, before=before,
                max_retries=max_retries, backoff_base=backoff_base, sleep=sleep)
            if not candles:
                # gap-skip: 빈 창은 "확인된 공백"이므로 창 크기만큼 건너뛰고 계속.
                # before 가 None(=최신부터)인데 비었으면 건너뛸 기준점이 없어 종료.
                if gap_skip_minutes > 0 and before is not None:
                    consecutive_empty += 1
                    # 빈 호출은 pages(데이터 페이지) 예산과 별도라 총 API 호출 수가
                    # max_pages 를 넘을 수 있다 → 호출자가 볼 수 있게 계수한다.
                    result.empty_fetches += 1
                    if consecutive_empty <= max_consecutive_empty:
                        result.gap_skips += 1
                        before = _shift_before(before, gap_skip_minutes)
                        if spacing:
                            sleep(spacing)
                        continue
                    result.exhausted = True
                    result.stopped_reason = "empty_budget_exhausted"
                    store.save_cursor(symbol, interval,
                                      oldest_ts_utc=prev_oldest.isoformat() if prev_oldest else None,
                                      next_before=None, exhausted=True)
                    store.commit()
                    break
                result.exhausted = True
                result.stopped_reason = "empty_page"
                store.save_cursor(symbol, interval,
                                  oldest_ts_utc=prev_oldest.isoformat() if prev_oldest else None,
                                  next_before=None, exhausted=True)
                store.commit()
                break
            consecutive_empty = 0

            pages += 1
            result.rows_seen += len(candles)
            # 시간 비교는 datetime 으로(문자열 사전식 비교의 오프셋/서브초 가정 회피).
            oldest_dt = min(c.ts_utc for c in candles)
            newest_dt = max(c.ts_utc for c in candles)
            oldest, newest = oldest_dt.isoformat(), newest_dt.isoformat()
            if result.earliest is None or oldest_dt < datetime.fromisoformat(result.earliest):
                result.earliest = oldest
            if result.latest is None or newest_dt > datetime.fromisoformat(result.latest):
                result.latest = newest

            store.upsert_candles(candles, source=BACKFILL_SOURCE, run_id=run_id, stats=stats)

            if nxt is None:
                # gap-skip: nextBefore=null 은 UTC 일자 경계일 뿐 소진이 아니다.
                # oldest 직전으로 before 를 옮겨 경계를 넘어 계속 페이징한다.
                if gap_skip_minutes > 0:
                    result.gap_skips += 1
                    prev_oldest = oldest_dt
                    before = _shift_before(oldest, 1)
                    store.save_cursor(symbol, interval, oldest_ts_utc=oldest,
                                      next_before=before, exhausted=False)
                    store.commit()
                    if spacing:
                        sleep(spacing)
                    continue
                store.save_cursor(symbol, interval, oldest_ts_utc=oldest,
                                  next_before=None, exhausted=True)
                store.commit()
                result.exhausted = True
                result.stopped_reason = "nextbefore_null"
                break
            # stale 커서로 더 과거로 진행하지 못하면(oldest 비감소) → 종료성 정지.
            # 커서를 exhausted 로 마킹해 다음 실행이 같은 stale 커서를 무한 재fetch 하지 않고
            # 처음부터 재스캔하도록(영구 미완/API 낭비 방지). next_before 는 exhausted 커서라
            # 어차피 소비되지 않으므로 다른 종료 경로와 동일하게 None(일관성).
            if prev_oldest is not None and oldest_dt >= prev_oldest:
                store.save_cursor(symbol, interval, oldest_ts_utc=oldest,
                                  next_before=None, exhausted=True)
                store.commit()
                result.stopped_reason = "no_progress"
                break

            prev_oldest = oldest_dt
            store.save_cursor(symbol, interval, oldest_ts_utc=oldest,
                              next_before=nxt, exhausted=False)
            store.commit()
            before = nxt
            if spacing:
                sleep(spacing)
        else:
            result.stopped_reason = "max_pages"
    except Exception as exc:  # noqa: BLE001 — 종목 격리(부분 진행분은 커밋 유지)
        # 격리하되 조용한 실패 방지: 전체 traceback 을 로그로 남긴다.
        logger.warning("backfill 종목 %s(%s) 실패 — 격리하고 다음 종목 진행: %s",
                       symbol, interval, exc, exc_info=True)
        store.commit()
        result.error = repr(exc)
        result.stopped_reason = result.stopped_reason or "error"

    result.pages_fetched = pages
    result.inserted = stats.inserted
    result.updated = stats.updated
    result.duplicate = stats.duplicate
    result.conflict = stats.conflict
    store.write_ingest_log(run_id, source=BACKFILL_SOURCE, stats=stats,
                           rows_seen=result.rows_seen, earliest=result.earliest,
                           latest=result.latest, note=result.error or result.stopped_reason,
                           symbol=symbol, interval=interval)
    store.commit()
    return result


def backfill_symbols(store: HistoryStore, provider: MarketDataPort, symbols: list[str],
                     interval: str, *, max_pages: int, page_size: int = 200,
                     spacing: float = 0.0, sleep: Callable[[float], None] = time.sleep,
                     max_retries: int = 3, backoff_base: float = 0.5,
                     resume: bool = True, run_id: str | None = None,
                     gap_skip_minutes: int = 0, max_consecutive_empty: int = 40) -> BackfillReport:
    """여러 종목을 순차 backfill. 종목 격리: 한 종목 실패가 나머지를 막지 않는다."""
    run_id = run_id or _new_run_id(interval)
    report = BackfillReport(interval=interval)
    last = len(symbols) - 1
    for i, sym in enumerate(symbols):
        try:
            res = backfill_symbol(
                store, provider, sym, interval, max_pages=max_pages, page_size=page_size,
                spacing=spacing, sleep=sleep, max_retries=max_retries,
                backoff_base=backoff_base, resume=resume, run_id=run_id,
                gap_skip_minutes=gap_skip_minutes,
                max_consecutive_empty=max_consecutive_empty)
        except Exception as exc:  # noqa: BLE001 — 방어적 격리(backfill_symbol 은 내부 격리하지만 이중 안전)
            res = SymbolBackfillResult(symbol=sym, interval=interval,
                                       error=repr(exc), stopped_reason="error")
        report.results.append(res)
        if spacing and i < last:  # 마지막 종목 뒤 불필요한 대기 제거
            sleep(spacing)
    return report
