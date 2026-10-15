"""완료-거래일 cutoff — 진행 중(미완성) 세션 봉이 canonical DB 에 새는 것을 막는다(이슈 #72).

배경: `daily_manifest`/`intraday_manifest` 는 `as_of` 초과 봉을 **판정에서** 제외하지만,
수집 경로(`backfill_history`)에는 저장 전에 미완성 진행일을 차단하는 게이트가 없다.
운영자가 장중의 오늘 날짜를 경계로 지정하면 partial 봉이 그대로 저장된다(#70/PR #71 사후 삭제 사례).

이 모듈은 **저장 전 게이트**를 제공한다:
- `last_completed_trading_day(now_utc, session_scope=...)` — 기준 시각 기준 NYSE 의 마지막 **완료**
  거래일. 완료 시각은 **수집 세션 범위**의 정상일 창 끝에 맞춘다:
  regular/1d = 16:00 ET, POST 포함(기본 1m) = 20:00 ET, OVERNIGHT 포함 = 익일 04:00 ET.
  조기폐장일에도 이 창을 좁히지 않는다. 2026-10-15 부터 `classify_session` 은 조기폐장일 REGULAR 를
  13:00 에 끝내고 13:00–20:00 을 POST 로 라벨하지만, 각 scope 의 마지막 세션 끝(16:00/20:00/익일 04:00)
  이후에만 '완료'로 보는 이 경계는 그보다 같거나 늦어 partial 이 새지 않는다(이슈 #72 핵심).
  주말·휴장·DST·자정 교차는 `exchange_calendar` + tz-aware ET datetime 비교로 판정한다.
- `validate_completed_through(requested, now_utc)` — 요청 cutoff 가 형식·범위·거래일·완료 여부를
  통과하지 못하면 fail-closed(자격증명/ API/DB 접근 전에 거부).
- `partition_by_cutoff(candles, completed_through)` — 저장 전 필터. `ts_exchange` 거래소-로컬
  일자가 cutoff 이하면 보존, 초과(다음 거래일 이후)면 제외. **입력 순서 보존**(결정론).

원칙:
- 현재시각은 **주입**(now_utc)한다. 코드 내부에서 임의로 고정하지 않는다(테스트 재현성).
- ET 변환은 저장소 표준(`src.models.ET`, stdlib zoneinfo)을 재사용한다 → EDT/EST 자동 정확.
- US(NYSE) 전용. 다른 시장을 NYSE 규칙으로 추측하지 않는다(호출측이 시장을 US 로 보증).

★ 자본 안전: 순수 함수. 주문/broker/risk/live/pipeline/전략/config 를 import 하지 않는다.
   Toss API 호출·DB write 없음.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from src.history import exchange_calendar as cal
from src.models import ET, SESSION_OVERNIGHT, SESSION_POST

# 완료 판정의 세션 범위(수집이 어느 세션까지 저장하는지에 따라 그 날의 '완료' 시각이 달라진다).
# 완료 시각은 정상일 세션 창 끝을 쓴다(조기폐장이어도 앞당기지 않음):
#  - "regular":  정규장까지(또는 1d 일봉). 완료 = 16:00 ET(REGULAR 창 끝).
#  - "post":     애프터마켓(POST)까지. 완료 = 20:00 ET(POST 창 끝).
#  - "overnight": 심야(OVERNIGHT)까지. 완료 = 익일 04:00 ET(자정 교차).
SCOPE_REGULAR = "regular"
SCOPE_POST = "post"
SCOPE_OVERNIGHT = "overnight"
# ★완료 판정 창은 **저장을 결정하는** classify_session(src/models.py)의 정상일 US 세션 창 끝과
#   같다: REGULAR 09:30–16:00 / POST 16:00–20:00 / OVERNIGHT(그 외, ~익일 04:00).
#   classify_session 은 2026-10-15 부터 조기폐장일에 REGULAR 09:30–13:00 / POST 13:00–20:00 을
#   쓴다. 그날도 POST·OVERNIGHT 끝은 같고 REGULAR 끝(13:00)은 16:00 보다 이르므로, 고정 경계는
#   저장 창보다 같거나 늦다(보수적). 완료 경계를 조기폐장으로 앞당기면(예 POST 17:00) 19:00 봉이
#   POST 로 저장되므로 '완료' 후 partial 이 샌다(이슈 #72 핵심 결함) — 따라서 창을 좁히지 않는다.
_US_REGULAR_END_MIN = 16 * 60      # REGULAR 창 끝(정상일 classify_session REGULAR 09:30–16:00)
_US_POST_END_MIN = 20 * 60         # POST 창 끝(= classify_session POST ~20:00, 조기폐장일 포함)
_US_OVERNIGHT_END_HOUR = 4         # OVERNIGHT 종료 = 익일 04:00 ET


class CompletedThroughError(ValueError):
    """요청 완료-거래일이 계약을 위반(미완료/미래/비거래일)했을 때 — 저장 시작 전 fail-closed.

    범위 밖 요청은 `exchange_calendar.CalendarRangeError` 로 별도 신호(캘린더 재검증 필요).
    """


def to_eastern(now_utc: datetime) -> datetime:
    """tz-aware UTC(또는 임의 tz-aware) 시각을 미국 동부(ET, EDT/EST 자동)로 변환.

    naive datetime 은 거부(오프셋 가정으로 인한 조용한 오판 방지)."""
    if now_utc.tzinfo is None:
        raise ValueError("now_utc 는 tz-aware 여야 한다(naive 금지).")
    return now_utc.astimezone(ET)


def completion_scope_for_sessions(interval: str, allowed_sessions) -> str:
    """수집 세션 범위 → 완료 판정 scope. 저장하는 **가장 늦은 세션**이 그 날 완료 시각을 정한다.

    - 1d(일봉) → 정규(일봉은 정규 마감에 확정).
    - 1m: OVERNIGHT 포함(all_sessions) → overnight, POST 포함(기본) → post, 그 외 → regular.
    """
    if interval != "1m":
        return SCOPE_REGULAR
    if allowed_sessions is None:
        # 1m 인데 세션 필터가 없으면(방어적) 기본 수집이 POST 포함이므로 보수적으로 post.
        return SCOPE_POST
    allowed = set(allowed_sessions)
    if SESSION_OVERNIGHT in allowed:
        return SCOPE_OVERNIGHT
    if SESSION_POST in allowed:
        return SCOPE_POST
    return SCOPE_REGULAR


def completion_deadline_et(d: date, scope: str) -> datetime:
    """거래일 d 의 수집 세션이 모두 끝나는 ET 시각(tz-aware). d 는 거래일이어야 한다.

    ★classify_session(저장 시 세션 라벨)의 정상일 창 끝을 쓴다(조기폐장이어도 앞당기지
    않는다 — 저장/완료 불일치로 partial 이 새는 것 방지, 이슈 #72):
    - regular: 16:00 ET(REGULAR 창 끝. 정상일 일봉 마감과도 동일).
    - post:    20:00 ET(POST 창 끝).
    - overnight: 익일 04:00 ET(OVERNIGHT 종료, 자정 교차).

    조기폐장일(실 마감 13:00)에도 16:00/20:00 창을 유지한다: classify_session 의 POST 는 그날도
    20:00 까지이므로 완료 경계를 앞당기면 17:00 '완료' 후에도 19:00 봉이 POST 로 저장돼 partial 이 샌다.
    """
    if scope == SCOPE_OVERNIGHT:
        nd = d + timedelta(days=1)
        return datetime(nd.year, nd.month, nd.day, _US_OVERNIGHT_END_HOUR, 0, tzinfo=ET)
    end_min = _US_POST_END_MIN if scope == SCOPE_POST else _US_REGULAR_END_MIN
    return datetime(d.year, d.month, d.day, end_min // 60, end_min % 60, tzinfo=ET)


def last_completed_trading_day(now_utc: datetime, *,
                               session_scope: str = SCOPE_REGULAR) -> date:
    """기준 시각(now_utc) 기준 NYSE 의 마지막 **완료** 거래일(ET 기준, 수집 세션 범위 반영).

    한 거래일 d 는 그 날 수집 세션이 모두 끝나는 시각(`completion_deadline_et`) 이후에만
    '완료'로 본다. 정상일 classify_session 창 끝: regular 16:00 / POST 20:00 / OVERNIGHT 익일
    04:00 ET(조기폐장일에도 동일 — 보수적). now 부터 하루씩 소급하며 첫 완료 거래일을 반환한다.

    ★ POST 를 기본 수집하는 1m 에서 16:00 만으로 '완료'라 하면 진행 중인 애프터마켓 봉이 새므로
      (이슈 #72 핵심 결함) 반드시 수집 세션 범위에 맞춘 경계를 쓴다. 전 판정은 tz-aware ET
      datetime 비교라 DST·자정 교차가 자동으로 맞는다.

    캘린더 artifact 범위 밖이면 `CalendarRangeError`(추측으로 거래일 오판 금지 — 상수 확장 필요).
    """
    now_et = to_eastern(now_utc)
    d = now_et.date()
    while d >= cal.CALENDAR_START:
        # is_trading_day 는 범위 밖이면 CalendarRangeError 를 던진다(미래/구간 밖 방어).
        if cal.is_trading_day(d) and now_et >= completion_deadline_et(d, session_scope):
            return d
        d -= timedelta(days=1)
    raise cal.CalendarRangeError(
        f"{now_et.date()} 기준 캘린더 구간({cal.CALENDAR_START}~) 내 완료 거래일을 찾지 못했다.")


def validate_completed_through(requested: date, now_utc: datetime, *,
                               session_scope: str = SCOPE_REGULAR) -> date:
    """요청 완료-거래일을 검증하고(통과 시) 그대로 반환. 실패 시 fail-closed 예외.

    규칙(자격증명/ API/DB 접근 전에 강제):
    1. 캘린더 artifact 범위 밖 → `CalendarRangeError`(NYSE 재검증 필요).
    2. NYSE 거래일이 아님(주말/휴장) → `CompletedThroughError`.
    3. 기준 시각의 마지막 완료 거래일보다 늦음(진행 중/미래 세션) → `CompletedThroughError`.
       완료 판정은 session_scope 를 반영한다(고정 창 — regular 16:00 / POST 20:00 / OVERNIGHT 익일 04:00).
    """
    # is_trading_day 가 범위 밖이면 CalendarRangeError 를 던진다(1).
    if not cal.is_trading_day(requested):
        raise CompletedThroughError(
            f"완료-거래일 {requested} 는 NYSE 거래일이 아니다(주말/휴장) — "
            f"직전 완료 거래일을 지정하라(fail-closed).")
    last = last_completed_trading_day(now_utc, session_scope=session_scope)
    if requested > last:
        raise CompletedThroughError(
            f"완료-거래일 {requested} 가 기준시각({now_utc.isoformat()})의 마지막 완료 거래일 "
            f"{last}(scope={session_scope}) 보다 늦다 — 진행 중/미래 세션은 수집 금지(fail-closed).")
    return requested


def parse_completed_through(text: str) -> date:
    """CLI 인자용 파서: 'YYYY-MM-DD' 만 허용. 형식 오류는 ValueError(argparse → exit 2)."""
    return date.fromisoformat(text)


def partition_by_cutoff(
    candles: list, completed_through: date
) -> tuple[list, int, set[str]]:
    """저장 전 cutoff 필터. (kept, excluded_count, excluded_days) 반환.

    - kept: `ts_exchange` 거래소-로컬 일자가 `completed_through` **이하**인 캔들(입력 순서 보존).
    - 초과(= 다음 거래일 이후 = 미완성 진행일 봉)는 제외한다.
    - excluded_days: 제외된 거래소-로컬 일자 문자열 집합(감사/범위 기록용).

    ★ 경계: `trading_day <= completed_through` → 당일 포함, 다음 거래일 제외(계약 #72).
       거래소-로컬 일자(`ts_exchange.date()`)를 기준으로 판정한다(US=ET, PRE/REGULAR/POST 동일 일자).
       OVERNIGHT 세션의 자정 이후(익일 00:00–04:00 ET) 봉은 date()=익일이라 cutoff=당일이면 제외되며,
       익일이 완료 처리될 때 다음 실행에서 수집된다(보수적 — partial 유실 아님, 최대 ~4h 경계 지연).
    """
    kept: list = []
    excluded_days: set[str] = set()
    excluded = 0
    for c in candles:
        ex_day = c.ts_exchange.date()
        if ex_day <= completed_through:
            kept.append(c)
        else:
            excluded += 1
            excluded_days.add(ex_day.isoformat())
    return kept, excluded, excluded_days
