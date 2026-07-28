"""ORB 게이트 경유 로딩 — 이슈 #91 (4-A).

사전등록 §3.5 절대 규칙:
  - 모든 캔들 로드는 `src.history.oos_guard.research_load(..., track="intraday")` 경유.
  - 성과 경로는 반드시 **`purpose="performance"`**(coverage 는 게이트를 통과하지 않아 blind
    강제가 장식이 된다 — 이 모듈은 성과 경로이므로 coverage 를 쓰지 않는다).
  - `src.history.loader.load_candles` **직접 import·호출 금지**.

사전등록 §8.7 로드 범위: 한 종목의 **하루치(PRE+REGULAR+POST)를 한 번에** 로드하고 그 안에서
frontier 를 계산한다. 전체 창을 한 번에 로드하면 다음 날 봉이 오늘의 마지막 버킷을 닫아
라이브가 낼 수 없는 봉이 생긴다(#76 §3.2 frontier 는 로드된 데이터의 함수).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from src.bars.resample import resample_1m
from src.history.oos_guard import TRACK_INTRADAY, research_load
from src.models import ET, SESSION_REGULAR, Candle, regular_session_window
from src.research.orb.contract import DecisionBarSpec
from src.research.orb.windows import minute_of_day, regular_close_minute_checked


def et_day_bounds(d: date) -> tuple[datetime, datetime]:
    """[ET 자정 d, ET 자정 d+1) — tz-aware. `research_load` 가 반개구간이므로 그대로 쓴다."""
    lo = datetime(d.year, d.month, d.day, tzinfo=ET)
    hi = lo + timedelta(days=1)
    return lo, hi


def load_symbol_day(db_path: str, symbol: str, d: date) -> tuple[Candle, ...]:
    """한 종목의 그 거래일 하루치(PRE+REGULAR+POST) 1분봉 — 게이트 경유, ts_utc 오름차순."""
    lo, hi = et_day_bounds(d)
    candles = research_load(
        db_path, symbol, "1m", start=lo, end=hi,
        purpose="performance", track=TRACK_INTRADAY, market="US",
    )
    return tuple(candles)


def regular_1m(candles: tuple[Candle, ...]) -> tuple[Candle, ...]:
    """`session == REGULAR` 만. `research_load` 는 세션 필터를 하지 않으므로 호출자 책임이다."""
    return tuple(c for c in candles if c.session == SESSION_REGULAR)


def decision_bars(day_candles: tuple[Candle, ...], d: date, *,
                  spec: DecisionBarSpec) -> tuple[Candle, ...]:
    """그 거래일의 결정봉(사전등록 §6.2). `spec` 은 `contract.decision_bar_spec(contract)` 로
    계약에서 얻어 넘긴다(기본값 없음 — 코드가 임의로 5·1 을 재입력하지 않는다, v1.1 MAJOR-4 반영).

    0. 입력을 `d` 그 날의 봉으로 제한한다(`ts_exchange.date() == d`) — 호출자가 실수로 여러
       날을 한 번에 넘기면 frontier 가 다음 날 봉으로 오늘 마지막 버킷을 조용히 닫아버릴 수
       있다(v1.1, 독립 리뷰 M2 반영). `load_symbol_day` 가 이미 하루치만 반환하지만, 이 함수는
       스스로도 그 불변식을 강제한다(방어적 이중화).
    1. `resample_1m(same_day, spec.minutes, min_bars=spec.min_bars, closed_only=True)` —
       `evaluation_time` 전달 금지(§8.7 이 (b)안을 기각했다. frontier 는 그 종목 그 날의
       마지막 1분봉 ts 여야 한다).
    2. 결과 중 `session == REGULAR` 만 남긴다(POST 봉은 마지막 REGULAR 버킷을 닫는 시간 증거로만
       쓰이고 매매 대상이 아니다).
    3. `ts_exchange` 의 minute-of-day 가 `regular_close_minute(d)` **미만**인 것만 남긴다
       (조기폐장일 13:00~16:00 의 연장거래 봉이 `classify_session` 의 고정 창 때문에 `REGULAR`
       로 라벨되므로, 이 조건이 없으면 그 봉들이 매매 대상이 된다).
    4. `ts_utc` 오름차순 정렬.
    """
    same_day = [c for c in day_candles if c.ts_exchange.date() == d]
    bars = resample_1m(same_day, spec.minutes, min_bars=spec.min_bars, closed_only=True)
    close_minute = regular_close_minute_checked(d)
    out = [
        b for b in bars
        if b.session == SESSION_REGULAR and minute_of_day(b) < close_minute
    ]
    # resample_1m 이 이미 (ts_utc, symbol, session) 순으로 정렬해 반환하므로 이 정렬은
    # 기능적으로는 중복이다(lane3 지적). 그래도 명시적으로 남긴다 — "ts_utc 오름차순"은
    # 4-B 순차 스캔이 의존하는 **계약**(§B5)이라, resample 내부 구현이 바뀌어도 이 함수
    # 자신의 출력 순서 보장이 깨지지 않게 한다.
    out.sort(key=lambda b: b.ts_utc)
    return tuple(out)


def expected_decision_minutes(d: date, *, spec: DecisionBarSpec) -> tuple[int, ...]:
    """그 거래일에 존재해야 하는 결정봉 시작 시각(ET minute-of-day) 그리드.

    570(09:30)부터 `spec.minutes` 간격, `regular_close_minute(d)` **미만**까지. `spec.minutes=5`
    기준 정상일 78개(570..955), 조기폐장일 42개(570..775).
    """
    close_minute = regular_close_minute_checked(d)
    open_minute, _ = regular_session_window("US")
    return tuple(range(open_minute, close_minute, spec.minutes))
