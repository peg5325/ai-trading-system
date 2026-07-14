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

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from src.history.store import HistoryStore, ImportStats
from src.models import Candle

BACKFILL_SOURCE = "toss_backfill"


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

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "interval": self.interval,
            "pages_fetched": self.pages_fetched, "rows_seen": self.rows_seen,
            "inserted": self.inserted, "updated": self.updated,
            "duplicate": self.duplicate, "conflict": self.conflict,
            "earliest": self.earliest, "latest": self.latest,
            "exhausted": self.exhausted, "stopped_reason": self.stopped_reason,
            "error": self.error,
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
                    resume: bool = True, run_id: str | None = None) -> SymbolBackfillResult:
    """한 종목을 과거로 backfill. 페이지마다 upsert + cursor 저장 + 커밋."""
    run_id = run_id or f"backfill-{interval}"
    stats = ImportStats(source=BACKFILL_SOURCE)
    result = SymbolBackfillResult(symbol=symbol, interval=interval)

    # resume: 미완(exhausted=False) 커서가 있으면 next_before 부터. 완료 커서는 처음부터 재스캔.
    before: str | None = None
    if resume:
        cur = store.get_cursor(symbol, interval)
        if cur and not cur["exhausted"] and cur["next_before"]:
            before = cur["next_before"]

    prev_oldest: str | None = None
    pages = 0
    try:
        while pages < max_pages:
            candles, nxt = _fetch_with_retry(
                provider, symbol, interval, count=page_size, before=before,
                max_retries=max_retries, backoff_base=backoff_base, sleep=sleep)
            if not candles:
                result.exhausted = True
                result.stopped_reason = "empty_page"
                store.save_cursor(symbol, interval, oldest_ts_utc=prev_oldest,
                                  next_before=None, exhausted=True)
                store.commit()
                break

            pages += 1
            result.rows_seen += len(candles)
            oldest = min(c.ts_utc for c in candles).isoformat()
            newest = max(c.ts_utc for c in candles).isoformat()
            if result.earliest is None or oldest < result.earliest:
                result.earliest = oldest
            if result.latest is None or newest > result.latest:
                result.latest = newest

            store.upsert_candles(candles, source=BACKFILL_SOURCE, run_id=run_id, stats=stats)

            exhausted = nxt is None
            if exhausted:
                store.save_cursor(symbol, interval, oldest_ts_utc=oldest,
                                  next_before=None, exhausted=True)
                store.commit()
                result.exhausted = True
                result.stopped_reason = "nextbefore_null"
                break
            # stale 커서로 더 과거로 진행하지 못하면(oldest 비감소) 무한루프 차단.
            if prev_oldest is not None and oldest >= prev_oldest:
                store.save_cursor(symbol, interval, oldest_ts_utc=oldest,
                                  next_before=nxt, exhausted=False)
                store.commit()
                result.stopped_reason = "no_progress"
                break

            prev_oldest = oldest
            store.save_cursor(symbol, interval, oldest_ts_utc=oldest,
                              next_before=nxt, exhausted=False)
            store.commit()
            before = nxt
            if spacing:
                sleep(spacing)
        else:
            result.stopped_reason = "max_pages"
    except Exception as exc:  # noqa: BLE001 — 종목 격리(부분 진행분은 커밋 유지)
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
                           latest=result.latest, note=result.error or result.stopped_reason)
    store.commit()
    return result


def backfill_symbols(store: HistoryStore, provider: MarketDataPort, symbols: list[str],
                     interval: str, *, max_pages: int, page_size: int = 200,
                     spacing: float = 0.0, sleep: Callable[[float], None] = time.sleep,
                     max_retries: int = 3, backoff_base: float = 0.5,
                     resume: bool = True, run_id: str | None = None) -> BackfillReport:
    """여러 종목을 순차 backfill. 종목 격리: 한 종목 실패가 나머지를 막지 않는다."""
    run_id = run_id or f"backfill-{interval}"
    report = BackfillReport(interval=interval)
    for sym in symbols:
        try:
            res = backfill_symbol(
                store, provider, sym, interval, max_pages=max_pages, page_size=page_size,
                spacing=spacing, sleep=sleep, max_retries=max_retries,
                backoff_base=backoff_base, resume=resume, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 — 방어적 격리(backfill_symbol 은 내부 격리하지만 이중 안전)
            res = SymbolBackfillResult(symbol=sym, interval=interval,
                                       error=repr(exc), stopped_reason="error")
        report.results.append(res)
        if spacing:
            sleep(spacing)
    return report
