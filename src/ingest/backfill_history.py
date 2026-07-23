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
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Protocol

from src.history.completed_session import partition_by_cutoff
from src.history.session_filter import SessionFilter
from src.history.store import HistoryStore, ImportStats
from src.models import Candle

BACKFILL_SOURCE = "toss_backfill"

logger = logging.getLogger(__name__)


def new_run_id(interval: str) -> str:
    """실행마다 유니크한 collection run_id — 한 수집 실행(공유 run_id)을 provenance 로 식별한다.

    공식 CLI 는 API/DB 접근 **전에** 이 값을 생성·출력해 두고 backfill_symbols 에 명시 전달한다.
    중단 후 resume 시 운영자가 같은 run_id 를 다시 지정하면(--run-id) ingest_log 가 같은 collection
    식별자로 이어져 manifest 교차검증(이슈 #79)이 하나의 run 으로 인식한다.
    """
    return f"backfill-{interval}-{datetime.now(timezone.utc).isoformat()}"


# 하위호환 alias(기존 내부 호출부 유지).
_new_run_id = new_run_id


def _day_range(days: set[str]) -> str | None:
    """제외된 거래소-로컬 일자 집합을 감사용 범위 문자열로(단일=그 날, 다수='min..max')."""
    if not days:
        return None
    s = sorted(days)
    return s[0] if len(s) == 1 else f"{s[0]}..{s[-1]}"


class MarketDataPort(Protocol):
    """backfill 이 요구하는 최소 인터페이스(TossMarketDataProvider.fetch_page 호환).

    raw_sink 는 선택적 확장(계약 §4.1): 지정되면 provider 가 canonical 변환 **전에** 응답
    payload 를 넘긴다. 미지정 시 기존 provider/fake 는 그대로 동작한다(backfill 은 raw_sink 가
    None 이면 fetch_page 에 인자를 전달하지 않는다 → 미지원 fake 도 호환).
    """

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
    # ★범위 의미 구분(이슈 #79 Scope B):
    #   earliest/latest = **fetch 도달 범위**(provider 가 돌려준 전체 candle, cutoff/session 제외분
    #   포함). 저장 범위가 아니다 → deprecated alias(=fetched_*). 아래 accepted_* 와 혼동 금지.
    earliest: str | None = None      # = fetched_earliest (legacy_fetch_range alias)
    latest: str | None = None        # = fetched_latest (legacy_fetch_range alias)
    # accepted_* = cutoff + SessionFilter 를 통과해 **이 실행에서 upsert 로 전달된** 범위.
    #   페이지 전체가 cutoff/세션으로 제외되면 fetched 범위는 있어도 accepted 는 None 이다.
    #   accepted 는 '실제 inserted'(=stats.inserted)와 다르다 — duplicate no-op 도 accepted 에 포함.
    accepted_earliest: str | None = None
    accepted_latest: str | None = None
    exhausted: bool = False
    stopped_reason: str | None = None
    error: str | None = None
    gap_skips: int = 0
    empty_fetches: int = 0      # 빈 응답 호출 수(= pages 예산 밖의 추가 API 호출)
    # 세션 필터(계약 §2.2): 저장 전 제외된 세션별 행수(run/symbol 단위 집계).
    rows_stored: int = 0        # 세션 필터 통과 후 실제 upsert 대상 행수(rows_seen 이상 아님)
    excluded_by_session: dict[str, int] = field(default_factory=dict)
    allowed_sessions: list[str] | None = None   # 실제 실행값(provenance)
    # 완료-거래일 cutoff(계약 #72): 저장 전에 미완성 진행일 봉을 제외한 provenance.
    completed_through: str | None = None                 # resolved cutoff(ISO date) or None
    rows_excluded_after_cutoff: int = 0
    excluded_after_cutoff_days: set[str] = field(default_factory=set)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "interval": self.interval,
            "pages_fetched": self.pages_fetched, "rows_seen": self.rows_seen,
            "inserted": self.inserted, "updated": self.updated,
            "duplicate": self.duplicate, "conflict": self.conflict,
            # deprecated alias(하위호환): earliest/latest == fetched_earliest/fetched_latest.
            "earliest": self.earliest, "latest": self.latest,
            "fetched_earliest": self.earliest, "fetched_latest": self.latest,
            "accepted_earliest": self.accepted_earliest,
            "accepted_latest": self.accepted_latest,
            # 소비자가 의미를 오독하지 않도록 범위 의미를 산출물에 명시(이슈 #79 Scope B).
            "range_semantics": {
                "earliest": "legacy_fetch_range (deprecated alias of fetched_earliest)",
                "latest": "legacy_fetch_range (deprecated alias of fetched_latest)",
                "fetched": "provider fetch-reached range (incl cutoff/session-excluded rows)",
                "accepted": ("post-cutoff + SessionFilter range passed to upsert this run "
                             "(None if all rows excluded; != actual inserted — dup included)"),
            },
            "exhausted": self.exhausted, "stopped_reason": self.stopped_reason,
            "error": self.error, "gap_skips": self.gap_skips,
            "empty_fetches": self.empty_fetches,
            "rows_stored": self.rows_stored,
            "excluded_by_session": dict(sorted(self.excluded_by_session.items())),
            "excluded_total": sum(self.excluded_by_session.values()),
            "allowed_sessions": self.allowed_sessions,
            "completed_through": self.completed_through,
            "rows_excluded_after_cutoff": self.rows_excluded_after_cutoff,
            "excluded_after_cutoff_days": sorted(self.excluded_after_cutoff_days),
            "excluded_after_cutoff_day_range": _day_range(self.excluded_after_cutoff_days),
        }


@dataclass
class BackfillReport:
    interval: str
    results: list[SymbolBackfillResult] = field(default_factory=list)
    # 이 수집 실행의 collection run_id(이슈 #79) — 공유 run_id 를 외부에 명확히 노출한다.
    run_id: str | None = None

    def as_dict(self) -> dict:
        return {"interval": self.interval, "run_id": self.run_id,
                "results": [r.as_dict() for r in self.results]}


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


def _ingest_note(result: "SymbolBackfillResult",
                 session_filter: SessionFilter | None) -> str | None:
    """ingest_log.note 조합 — 정지사유/에러 + 세션 필터 provenance(기본값+실행값+제외수).

    세션 필터가 없으면 기존과 동일(정지사유/에러만). 있으면 실제 허용 세션과 제외 행수를
    함께 남겨 감사(계약 §2.2: '기본값과 실제 실행값을 ingest log 에 기록')를 가능케 한다.
    """
    if session_filter is None and result.completed_through is None:
        # 세션 필터도 cutoff 도 없음(예: 레거시 1d) → 기존과 동일: 정지사유/에러만.
        return result.error or result.stopped_reason
    # ★reason= 을 정지사유 enum(';' 미포함)으로 **첫 키** 에 둔다. error= 는 repr 이라 ';' 를
    #   포함할 수 있어 **맨 뒤** 에 둔다 → 소비측(_parse_stopped_reason)이 reason= 를 안전하게
    #   파싱(위치 split 의 취약성 제거). cutoff 필드는 세션 필드 뒤·error= 앞에 붙인다.
    parts = [f"reason={result.stopped_reason or ''}"]
    if session_filter is not None:
        allowed = ",".join(sorted(session_filter.allowed))
        excl = ",".join(f"{k}:{v}" for k, v in sorted(result.excluded_by_session.items()))
        parts += [f"sessions_allowed={allowed}",
                  f"all_sessions={session_filter.all_sessions}",
                  f"excluded={{{excl}}}",
                  f"stored={result.rows_stored}"]
    if result.completed_through is not None:
        # 완료-거래일 cutoff provenance(계약 #72): resolved 경계·제외 행수·제외 일자 범위.
        parts += [f"completed_through={result.completed_through}",
                  f"excluded_after_cutoff={result.rows_excluded_after_cutoff}"]
        rng = _day_range(result.excluded_after_cutoff_days)
        if rng:
            parts.append(f"cutoff_excluded_days={rng}")
    if result.error:
        parts.append(f"error={result.error}")
    return "; ".join(parts)


def _fetch_with_retry(provider: MarketDataPort, symbol: str, interval: str, *,
                      count: int, before: str | None, max_retries: int,
                      backoff_base: float, sleep: Callable[[float], None],
                      raw_sink: Callable[..., object] | None = None):
    """429 는 지수 backoff(base*2^attempt)로 재시도. 그 외 예외/한도초과는 상위로 전파.

    raw_sink 는 None 이 아닐 때만 fetch_page 로 전달한다(미지원 provider/fake 호환).
    """
    kwargs: dict = {"count": count, "before": before}
    if raw_sink is not None:
        kwargs["raw_sink"] = raw_sink
    attempt = 0
    while True:
        try:
            return provider.fetch_page(symbol, interval, **kwargs)
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
                    max_consecutive_empty: int = 40,
                    session_filter: SessionFilter | None = None,
                    completed_through: date | None = None,
                    raw_sink: Callable[..., object] | None = None) -> SymbolBackfillResult:
    """한 종목을 과거로 backfill. 페이지마다 upsert + cursor 저장 + 커밋.

    completed_through(계약 #72): 지정 시 **저장 전에** 거래소-로컬 일자가 그 cutoff 를 초과하는
    (= 미완성 진행일 이후) 봉을 제외한다. 필터는 cursor 계산 **후**·SessionFilter **전**에 적용해
    페이지네이션 탐색 깊이를 바꾸지 않는다(cursor 는 필터 전 전체 페이지 기준). raw 스냅샷은
    parse 전 단계라 제외 봉도 그대로 보존한다. resolved cutoff·제외 행수는 결과/ingest_log 에 기록.

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
    if session_filter is not None:
        result.allowed_sessions = sorted(session_filter.allowed)
    if completed_through is not None:
        result.completed_through = completed_through.isoformat()

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
                max_retries=max_retries, backoff_base=backoff_base, sleep=sleep,
                raw_sink=raw_sink)
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

            # ★필터는 **저장 전에** 적용한다(사후 DELETE 아님). 페이지네이션 커서(oldest/newest)
            #   는 위에서 **필터 전 전체 페이지** 기준으로 계산했으므로 어떤 필터도 과거로 페이징
            #   하는 경계를 바꾸지 않는다(제외돼도 커서는 정상 전진).
            #   순서(계약 #72): raw(provider) → parse → ①완료-거래일 cutoff → ②SessionFilter → upsert.
            page = candles
            # ① 완료-거래일 cutoff: 미완성 진행일(다음 거래일 이후) 봉을 세션 필터 전에 제외.
            if completed_through is not None:
                page, cut_n, cut_days = partition_by_cutoff(page, completed_through)
                result.rows_excluded_after_cutoff += cut_n
                result.excluded_after_cutoff_days |= cut_days
            # ② SessionFilter: unknown 세션 라벨은 partition 이 fail-closed → 종목 격리.
            if session_filter is not None:
                to_store, excluded = session_filter.partition(page)
                for sess, cnt in excluded.items():
                    result.excluded_by_session[sess] = (
                        result.excluded_by_session.get(sess, 0) + cnt)
            else:
                to_store = page
            result.rows_stored += len(to_store)
            # ★accepted 범위(이슈 #79 Scope B): cutoff+세션 필터를 통과해 실제 upsert 로 전달된
            #   봉의 ts_utc 범위. 페이지 전체가 제외되면(to_store 비면) accepted 는 갱신되지 않아
            #   None 으로 남는다(fetched 범위와 구분). duplicate no-op 도 accepted 에 포함된다.
            if to_store:
                acc_old = min(c.ts_utc for c in to_store)
                acc_new = max(c.ts_utc for c in to_store)
                if (result.accepted_earliest is None
                        or acc_old < datetime.fromisoformat(result.accepted_earliest)):
                    result.accepted_earliest = acc_old.isoformat()
                if (result.accepted_latest is None
                        or acc_new > datetime.fromisoformat(result.accepted_latest)):
                    result.accepted_latest = acc_new.isoformat()
            store.upsert_candles(to_store, source=BACKFILL_SOURCE, run_id=run_id, stats=stats)

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
    # ingest_log.earliest/latest 는 **legacy_fetch_range**(fetch 도달 범위)다 — 저장(accepted)
    #   범위가 아니다(이슈 #79 Scope B). accepted 범위는 SymbolBackfillResult.accepted_* / report
    #   JSON 으로 노출한다. cutoff cross-check(이슈 #79 Scope A)는 note 의 completed_through 를 쓴다.
    store.write_ingest_log(run_id, source=BACKFILL_SOURCE, stats=stats,
                           rows_seen=result.rows_seen, earliest=result.earliest,
                           latest=result.latest,
                           note=_ingest_note(result, session_filter),
                           symbol=symbol, interval=interval)
    store.commit()
    return result


def backfill_symbols(store: HistoryStore, provider: MarketDataPort, symbols: list[str],
                     interval: str, *, max_pages: int, page_size: int = 200,
                     spacing: float = 0.0, sleep: Callable[[float], None] = time.sleep,
                     max_retries: int = 3, backoff_base: float = 0.5,
                     resume: bool = True, run_id: str | None = None,
                     gap_skip_minutes: int = 0, max_consecutive_empty: int = 40,
                     session_filter: SessionFilter | None = None,
                     completed_through: date | None = None,
                     raw_sink: Callable[..., object] | None = None) -> BackfillReport:
    """여러 종목을 순차 backfill. 종목 격리: 한 종목 실패가 나머지를 막지 않는다.

    completed_through(계약 #72): 지정 시 모든 종목에 동일한 완료-거래일 cutoff 를 적용한다.

    run_id(이슈 #79): 한 호출의 모든 종목이 공유하는 collection run_id. 지정하지 않으면 새로
    생성하고 report.run_id 로 노출한다(공식 CLI 는 API 접근 전에 미리 생성·출력해 전달한다).
    resume 시 같은 run_id 를 다시 지정하면 ingest_log 가 하나의 collection 으로 이어진다."""
    run_id = run_id or new_run_id(interval)
    report = BackfillReport(interval=interval, run_id=run_id)
    last = len(symbols) - 1
    for i, sym in enumerate(symbols):
        try:
            res = backfill_symbol(
                store, provider, sym, interval, max_pages=max_pages, page_size=page_size,
                spacing=spacing, sleep=sleep, max_retries=max_retries,
                backoff_base=backoff_base, resume=resume, run_id=run_id,
                gap_skip_minutes=gap_skip_minutes,
                max_consecutive_empty=max_consecutive_empty,
                session_filter=session_filter, completed_through=completed_through,
                raw_sink=raw_sink)
        except Exception as exc:  # noqa: BLE001 — 방어적 격리(backfill_symbol 은 내부 격리하지만 이중 안전)
            res = SymbolBackfillResult(symbol=sym, interval=interval,
                                       error=repr(exc), stopped_reason="error")
        report.results.append(res)
        if spacing and i < last:  # 마지막 종목 뒤 불필요한 대기 제거
            sleep(spacing)
    return report
