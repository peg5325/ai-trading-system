"""ORB opening range · gap · RVOL 피처 — 이슈 #91 (4-A).

성과·수익률·PnL 은 여기서 계산하지 않는다(4-A 는 피처까지). 후보 판정(gap/RVOL 임계 비교)과
매매 로직은 4-B 이후 범위다.

데이터 사유 폐쇄 어휘 4종(사전등록 §11.2 [A]): `DATA_EXCLUSION_REASONS`. 새 사유를 발명하지
않는다 — 여기 없는 사유가 필요해 보이면 그건 이 4개 중 하나로 환원되거나(예: 당일 09:30 1분봉
부재는 opening range 창의 첫 분 부재이므로 `incomplete_opening_range` 로 처리) 사전등록에
새 조항이 필요하다는 신호다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.bars.resample import opening_range
from src.models import Candle, regular_session_window
from src.research.orb.windows import (
    minute_of_day,
    previous_trading_day,
    regular_close_minute_checked,
)

# 사전등록 §11.2 [A] — 정확히 이 4개, 다른 사유를 추가하지 않는다.
DATA_EXCLUSION_REASONS: tuple[str, ...] = (
    "incomplete_opening_range",
    "no_previous_regular_close",
    "insufficient_rvol_lookback",
    "opening_range_not_closed",
)


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float


def _raise_if_duplicate_minute_of_day(candles: tuple[Candle, ...]) -> None:
    """같은 minute-of-day 가 두 번 이상 나타나면 raise(fail-closed, v1.1 lane3 F3 반영).

    snapshot PK `(symbol, interval, ts_utc)` 유일성 덕에 정상 경로에서는 발생하지 않는
    **전제**이지만, 강제도 검증도 안 된 채로 두면 조용한 입력 순서 의존(last-write-wins /
    first-write-wins)이 된다 — 독립 리뷰가 순서만 바꿔 다른 값을 실제로 뽑아냈다. 전제를
    코드로 강제해 그 경로 자체를 없앤다.
    """
    seen: set[int] = set()
    for c in candles:
        mod = minute_of_day(c)
        if mod in seen:
            raise ValueError(
                f"같은 분(minute-of-day={mod})이 두 번 이상 나타났다 — snapshot PK "
                "(symbol, interval, ts_utc) 유일성 전제가 깨졌다는 뜻이다(fail-closed).")
        seen.add(mod)


@dataclass(frozen=True)
class SymbolDayFeatures:
    symbol: str
    day: date
    opening_range: OpeningRange | None
    gap: float | None
    rvol: float | None
    excluded_reasons: tuple[str, ...]   # DATA_EXCLUSION_REASONS 부분집합, 정렬된 결정론적 순서


# --------------------------------------------------------------------------
# opening range (§6.1)
# --------------------------------------------------------------------------
def opening_range_for_spec(day_candles: tuple[Candle, ...], *,
                           minutes: int,
                           day: date) -> tuple[OpeningRange | None, tuple[str, ...]]:
    """primary/sensitivity spec 의 opening range + 배제 사유(있다면).

    `require_complete`/`closed_only` 를 각각 끈 호출로 두 실패 모드를 구분한다:
      - `require_complete=True, closed_only=False` 로도 비면 → 창 안 분이 `minutes` 미만
        (`incomplete_opening_range`).
      - 그 호출은 차는데 `closed_only=True` 로는 비면 → 분은 다 찼지만 종료 증거가 없음
        (`opening_range_not_closed`).
    `evaluation_time` 은 전달하지 않는다(사전등록 §6.1: frontier 는 그 종목 그 날의 마지막
    1분봉 ts 여야 한다 — §8.7 이 명시적으로 (b)안을 기각했다).

    **`day_candles` 는 실제로 `day` 그 날의 봉이어야 한다**(v1.2, H2 — team-lead 재현 재발
    방지). `opening_range()` 는 (symbol, 거래일) 로 그룹핑하므로, 두 날이 섞여도 **OR 을
    만드는 날이 하나뿐이면 결과 개수는 1** 이 되어 예전의 `len(...) > 1` 가드가 침묵했다 —
    이 docstring 이 막는다고 적어 온 "'가장 이른 날'을 조용히 돌려주게 된다"는 사고가 개수
    기반 가드로는 실제로 막히지 않았다(2024-03-04 16봉 완전 + 2024-03-05 14봉 불완전을
    섞으면 반환 OR 은 03-04 것인데 사유는 빈 튜플). 개수가 아니라 **날짜**로 검증한다 —
    `gap_for_day` 의 `wrong_dates`·`decision_bars` 의 `ts_exchange.date() == d` 와 이미
    같은 방식이다.
    """
    wrong_dates = {c.ts_exchange.date() for c in day_candles} - {day}
    if wrong_dates:
        raise ValueError(
            f"opening_range_for_spec: day_candles 에 day={day} 가 아닌 날짜 "
            f"{sorted(wrong_dates)} 가 섞여 있다(호출자는 한 종목의 하루치만 넘겨야 한다).")

    candles = list(day_candles)
    closed_complete = opening_range(
        candles, minutes=minutes, market="US", require_complete=True, closed_only=True)
    if len(closed_complete) > 1:
        raise ValueError(
            f"opening_range_for_spec: 같은 날짜 안에 둘 이상의 opening range 가 나왔다"
            f"({len(closed_complete)}개) — 호출자가 여러 종목을 섞어 넘겼을 수 있다(방어적"
            " 이중화 — day 검증을 이미 통과한 뒤라 정상적으로는 도달하지 않는다).")
    if closed_complete:
        c = closed_complete[0]
        return OpeningRange(high=c.high, low=c.low), ()

    complete_regardless_of_closed = opening_range(
        candles, minutes=minutes, market="US", require_complete=True, closed_only=False)
    if len(complete_regardless_of_closed) > 1:
        raise ValueError(
            f"opening_range_for_spec: 같은 날짜 안에 둘 이상의 opening range 후보가 나왔다"
            f"({len(complete_regardless_of_closed)}개) — 호출자가 여러 종목을 섞어 넘겼을 "
            "수 있다.")
    if not complete_regardless_of_closed:
        return None, ("incomplete_opening_range",)
    return None, ("opening_range_not_closed",)


# --------------------------------------------------------------------------
# gap (§5.1)
# --------------------------------------------------------------------------
def gap_for_day(*, today_regular_1m: tuple[Candle, ...], day: date, prev_day: date,
               prev_day_regular_1m: tuple[Candle, ...]) -> tuple[float | None, tuple[str, ...]]:
    """gap = (today_open - prev_close) / prev_close.

    prev_close = 직전 거래일 REGULAR 세션에서 `ts_exchange` 의 minute-of-day 가
    `regular_close_minute(prev_day)` **미만**인 마지막 완료 1분봉의 close(조기폐장일 13:00
    이후 연장거래 봉 제외 — 부등호는 `미만`이어야 한다, §5.1).
    today_open = 당일 REGULAR 09:30 완료 1분봉의 open.

    당일 09:30 1분봉이 없으면 그 분은 opening range 창의 첫 분이기도 하므로
    `incomplete_opening_range` 로 처리한다(폐쇄 어휘 4종 밖의 새 사유를 만들지 않는다 — 4-A
    구현 해석, 보고서 참고).

    **`today_regular_1m` 도 실제로 `day` 그 날의 봉이어야 한다**(v1.2, M1 — lane4 지적,
    BLOCKER-3 의 비대칭). `prev_day_regular_1m` 쪽만 엄격히 검증하고 오늘 쪽은 방치하면,
    완전히 다른 날 봉을 넘겨도 gap 이 산출되고 사유가 0이며, 그 값이 `RvolWindow.push(day, …)`
    로 틀린 날짜 키에 이력까지 남긴다(H2 와 같은 뿌리).

    **`prev_day` 는 `day` 의 캘린더상 직전 거래일이어야 한다**(v1.2, M2 — §5.1
    "직전 거래일은 `exchange_calendar.trading_days` 기준 직전 거래일"). 봉이 `prev_day` 의
    것인가만 보고 `prev_day` 자체가 진짜 직전 거래일인가는 검증하지 않으면, 10 거래일 전
    날짜를 넘겨도 통과한다 — `previous_trading_day(day)` 와 대조한다.

    **`prev_day_regular_1m` 은 실제로 `prev_day` 그 날의 봉이어야 한다**(v1.1, 독립 리뷰
    BLOCKER-3 반영) — 호출자가 "마지막으로 본 비어있지 않은 날"을 캐시해 더 오래된 종가를
    조용히 흘려보내는 사고를 구조적으로 막는다. 다른 날짜가 섞여 있으면 raise 한다.
    """
    wrong_today_dates = {c.ts_exchange.date() for c in today_regular_1m} - {day}
    if wrong_today_dates:
        raise ValueError(
            f"gap_for_day: today_regular_1m 에 day={day} 가 아닌 날짜 "
            f"{sorted(wrong_today_dates)} 가 섞여 있다(호출자가 다른 날의 봉을 잘못 "
            "전달했다).")

    expected_prev_day = previous_trading_day(day)
    if prev_day != expected_prev_day:
        raise ValueError(
            f"gap_for_day: prev_day={prev_day} 는 day={day} 의 캘린더상 직전 거래일이 "
            f"아니다(기대값 {expected_prev_day}) — §5.1 은 exchange_calendar.trading_days "
            "기준 직전 거래일만 허용한다.")

    wrong_dates = {c.ts_exchange.date() for c in prev_day_regular_1m} - {prev_day}
    if wrong_dates:
        raise ValueError(
            f"gap_for_day: prev_day_regular_1m 에 prev_day={prev_day} 가 아닌 날짜 "
            f"{sorted(wrong_dates)} 가 섞여 있다(호출자가 다른 날의 봉을 잘못 전달했다).")

    prev_close_minute = regular_close_minute_checked(prev_day)
    eligible_prev = [
        c for c in prev_day_regular_1m
        if minute_of_day(c) < prev_close_minute
    ]
    if not eligible_prev:
        return None, ("no_previous_regular_close",)
    _raise_if_duplicate_minute_of_day(eligible_prev)
    prev_close = max(eligible_prev, key=minute_of_day).close

    open_minute, _ = regular_session_window("US")
    today_open = [c for c in today_regular_1m if minute_of_day(c) == open_minute]
    if not today_open:
        return None, ("incomplete_opening_range",)
    _raise_if_duplicate_minute_of_day(today_open)   # len>1 은 전부 같은 분이므로 곧 중복
    today_open_price = today_open[0].open
    return (today_open_price - prev_close) / prev_close, ()


# --------------------------------------------------------------------------
# RVOL (§5.2)
# --------------------------------------------------------------------------
def regular_window_volume(day_regular_1m: tuple[Candle, ...], *, day: date,
                          minutes: int) -> tuple[float, int]:
    """그 날 09:30~09:30+minutes 구간의 REGULAR 1분봉 volume 합 + 관측된 분 수.

    미래 거래량 금지: 창 종료(09:30+minutes) 이상의 minute-of-day 는 구조적으로 제외한다.
    창 시작(09:30) 미만의 minute-of-day 도 제외한다(이 함수는 세션 필터를 하지 않으므로,
    이 하한이 PRE 봉 유입을 막는 유일한 방어선이다 — v1.1 lane3 F7 반영).
    같은 분이 두 번 나오면 raise(v1.1 lane3 F3 — 조용한 순서 의존 대신 fail-closed).

    **`day_regular_1m` 은 실제로 `day` 그 날의 봉이어야 한다**(v1.2, M1 — lane4 지적,
    H2 와 같은 뿌리). 이 저장소에서 이 함수는 오늘(`today_regular_1m`)에 대해서만 불린다 —
    과거일의 원본 봉은 `RvolWindow` 이력에 이미 합산된 형태로만 보관되고 여기로 다시
    들어오지 않는다. 검증 없이 다른 날 봉이 섞이면 거래량이 조용히 산출되는 대신 예외를
    낸다.
    """
    wrong_dates = {c.ts_exchange.date() for c in day_regular_1m} - {day}
    if wrong_dates:
        raise ValueError(
            f"regular_window_volume: day_regular_1m 에 day={day} 가 아닌 날짜 "
            f"{sorted(wrong_dates)} 가 섞여 있다(호출자가 다른 날의 봉을 잘못 전달했다).")

    open_minute, _ = regular_session_window("US")
    hi = open_minute + minutes
    in_window = [c for c in day_regular_1m if open_minute <= minute_of_day(c) < hi]
    _raise_if_duplicate_minute_of_day(in_window)
    total = sum(c.volume for c in in_window)
    return total, len(in_window)


def cum_today_volume(day_regular_1m: tuple[Candle, ...], *, day: date, minutes: int) -> float:
    """RVOL 분자 — opening range 창 안(그 창을 넘는 봉은 절대 포함하지 않음)의 volume 합."""
    total, _observed = regular_window_volume(day_regular_1m, day=day, minutes=minutes)
    return total


def window_sum_for_lookback(day_regular_1m: tuple[Candle, ...], *, day: date,
                            minutes: int) -> float | None:
    """RVOL 분모용 그 날의 opening-window volume 합 — 창이 **전부** 있어야 값을 낸다.

    일부만 있는 날을 분모에 넣으면 `cum_prev` 가 작아져 RVOL 이 부풀려진다(§5.2). 불완전하면
    `None`(그 날을 `RvolWindow` 유효 표본에서 제외).
    """
    total, observed = regular_window_volume(day_regular_1m, day=day, minutes=minutes)
    if observed < minutes:
        return None
    return total


def _median(values: list[float]) -> float:
    """짝수 표본은 가운데 두 값의 산술평균(사전등록 §5.2 표준 정의)."""
    xs = sorted(values)
    n = len(xs)
    mid = n // 2
    if n % 2 == 1:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


class RvolWindow:
    """종목별 직전 `lookback_days` 거래일 opening-window volume 합의 롤링 창.

    **인과성은 문서 규약이 아니라 `rvol()` 자체가 강제한다(v1.1, 독립 리뷰 H2/BLOCKER-1 반영)**:
    `rvol(day, today_sum)` 은 `day` 를 받아 그 날이 이미 `push` 됐으면(`day <= 마지막 push 일`)
    raise 한다 — "오늘 거래량이 자기 분모에 들어간다" 는 인과성 위반이 예외 없이는 표현 불가능하다.
    `compute_symbol_day_features` 가 `rvol()` 호출 뒤 자동으로 `push()` 하므로, 그 함수를 쓰는
    한 순서를 틀릴 자리가 아예 없다. `RvolWindow` 를 직접 쥐고 쓰는 호출자는 계속
    "그 날의 rvol() 을 먼저 → 그 다음 그 날을 push()" 순서를 지켜야 한다.

    **창 길이도 스스로 알아야 한다(v1.1, BLOCKER-2 반영)**: primary(15분)·sensitivity(5분) 처럼
    같은 종목에 창 길이가 다른 `RvolWindow` 를 여러 개 유지할 때, 어느 것이 어느 spec 용인지
    뒤바뀌어도 예전엔 검사가 없었다. `minutes` 를 생성 시점에 고정해 `.minutes` 로 노출하고,
    `compute_symbol_day_features` 가 이를 `opening_range_minutes` 와 대조한다.

    `push` 는 **시간 순서대로만** 호출된다 — 역순·중복 호출은 예외(결정론 보호).
    """

    def __init__(self, lookback_days: int, min_valid_days: int, minutes: int):
        if lookback_days < 1:
            raise ValueError("lookback_days 는 1 이상이어야 한다.")
        if min_valid_days < 1:
            raise ValueError("min_valid_days 는 1 이상이어야 한다.")
        if minutes < 1:
            raise ValueError("minutes 는 1 이상이어야 한다(이 창이 속한 opening range 길이).")
        self._lookback_days = lookback_days
        self._min_valid_days = min_valid_days
        self._minutes = minutes
        self._history: list[float | None] = []
        self._last_day: date | None = None

    @property
    def minutes(self) -> int:
        """이 창이 속한 opening range 길이(분) — primary=15 / sensitivity=5 구분용."""
        return self._minutes

    def push(self, day: date, window_sum: float | None) -> None:
        if self._last_day is not None and day <= self._last_day:
            raise ValueError(
                f"RvolWindow.push 는 시간 순서대로만 호출된다: {day} 는 직전 {self._last_day} "
                "이후여야 한다(역순/중복 호출 금지 — 결정론 보호).")
        self._history.append(window_sum)
        if len(self._history) > self._lookback_days:
            self._history = self._history[-self._lookback_days:]
        self._last_day = day

    def rvol(self, day: date, today_sum: float) -> float | None:
        """`day` 이전(= 엄격히 과거)에 push 된 유효 과거일만으로 RVOL 을 낸다.

        `day` 가 이미 push 된 날짜 이하이면 raise(인과성 위반 — 오늘 거래량이 자기 분모에
        들어가는 것을 구조적으로 차단한다).
        유효일이 `min_valid_days` 미만이거나 median 이 0 이면 `None`
        (호출자가 `insufficient_rvol_lookback` 으로 처리, §5.2).
        """
        if self._last_day is not None and day <= self._last_day:
            raise ValueError(
                f"RvolWindow.rvol({day}) 는 이미 push 된 {self._last_day} 이후여야 한다 — "
                "오늘 거래량이 자기 분모에 들어가는 인과성 위반을 막는다(v1.2, C10 — 형제 "
                "push() 는 '이후'로 옳게 적혀 있었는데 이 메시지만 '이전'으로 반대로 적혀 "
                "있었다).")
        valid = [v for v in self._history if v is not None]
        if len(valid) < self._min_valid_days:
            return None
        med = _median(valid)
        if med == 0:
            return None
        return today_sum / med


# --------------------------------------------------------------------------
# 조립 — SymbolDayFeatures
# --------------------------------------------------------------------------
def compute_symbol_day_features(*, symbol: str, day: date,
                                today_day_candles: tuple[Candle, ...],
                                today_regular_1m: tuple[Candle, ...],
                                prev_day: date | None,
                                prev_day_regular_1m: tuple[Candle, ...],
                                opening_range_minutes: int,
                                rvol_window: RvolWindow) -> SymbolDayFeatures:
    """opening range · gap · RVOL 을 한 종목-일에 대해 조립한다.

    - `today_day_candles`: 그 날 하루치 전체(PRE+REGULAR+POST) — opening range 의 `closed_only`
      frontier 판정에 필요하다(§loading.py 참조, POST 봉이 마지막 REGULAR 버킷을 닫는 시간
      증거가 되는 것과 같은 이유).
    - `rvol_window` 는 이 함수가 **직접 관리**한다: `rvol(day, today_sum)` 으로 값을 얻은 뒤 이
      함수가 스스로 `push(day, window_sum_for_lookback(...))` 를 호출한다(v1.1, 독립 리뷰
      BLOCKER-1 반영) — 호출자가 push 순서를 틀릴 자리를 아예 없앤다. **직접 `RvolWindow` 를
      쥐고 이 함수 밖에서 같은 날짜를 다시 push 하지 마라**(중복 push 로 예외가 난다).
    - `rvol_window.minutes` 가 `opening_range_minutes` 와 다르면 raise 한다(v1.1, BLOCKER-2
      반영) — primary(15분)·sensitivity(5분) 창을 뒤바꿔 넘기는 실수를 구조적으로 막는다.
    - 사유는 폐쇄 어휘(`DATA_EXCLUSION_REASONS`) 부분집합을 **정렬**해 담는다(결정론).

    ⚠ **호출 규율(v1.2, C12 — lane4 지적)**: 이 함수는 **평가 창의 매 거래일마다** 호출해야
    한다 — 데이터가 없는 종목-일도 포함해서다(그 경우 `today_day_candles`/`today_regular_1m`
    이 빈 튜플이면 되고, `rvol_window.push(day, None)` 으로 결측이 기록된다). `RvolWindow` 가
    세는 것은 "직전 20 **거래일**"이 아니라 "직전 20 **push 호출**"이다 — 호출자가 데이터
    없는 날을 건너뛰고 이 함수를 부르지 않으면, `lookback_days=20` 이 캘린더로는 조용히 20
    거래일보다 더 뒤로 뻗는다(실증: 20 push 가 38 캘린더일을 덮은 사례가 있었다). 이 계약은
    이 함수의 시그니처만으로는 강제되지 않으므로, 4-B 는 반드시 평가 창의 모든 거래일을
    빠짐없이 순회하며 호출해야 한다.

    ⚠ **`excluded_reasons` 는 각 필드를 개별적으로 null 화하지 않는다(v1.2, C12)**.
    `opening_range`/`gap`/`rvol` 은 §6.1/§5.1/§5.2 각자의 조건으로 독립적으로 계산되며, 서로
    다른 사유가 그중 하나만 배제해도 나머지 필드는 **값을 낸다**. 특히 §5.5 로 종목-일 전체가
    배제된 뒤에도(예: opening range 창의 일부 분만 결측) `gap`/`rvol` 이 숫자로 나올 수
    있다 — 예를 들어 09:30~09:44 중 09:44 한 분만 결측이면 OR 은 `incomplete_opening_range`
    로 배제되지만, `gap`(09:30 봉만 있으면 됨)과 `rvol`(그 분자 `cum_today_volume` 이 창의
    **존재하는 분만으로** 합산하고 완전성을 요구하지 않음, `window_sum_for_lookback` 의 분모
    쪽과 달리)은 **완전성을 요구하지 않아** 그대로 값을 낸다(실측: 14/15분 창에서
    rvol=0.933 — 분자가 14분 합이라 실제보다 작다). **호출자(4-B)가 후보 판정에 이 값들을
    쓰려면 반드시 `excluded_reasons == ()` 를 먼저 확인해야 한다(AND) — 사유별로 골라 쓰면
    (예: `incomplete_opening_range` 만 보고 넘어가면서 `rvol` 은 그대로 쓰면) 과소평가된
    RVOL 이 조용히 후보 판정에 들어간다.** (판단 근거: 이 함수는 4-A 의 피처 계산 계층이고
    §5.5 의 배제 여부 자체는 4-B 의 후보 판정 로직이 담당한다 — 여기서 필드를 사유별로
    선별적으로 null 화하면 그 판단을 4-A 가 대신 내리는 셈이 되고, 4개 배제 사유 중 특정
    필드와 무관한 사유(예: `no_previous_regular_close` 가 `rvol` 을 null 화)까지 함께
    묶으면 과잉 무효화가 된다. 그래서 구조적 null 화 대신 **이 docstring 의 AND 요구를
    호출자 계약으로 명시**하는 쪽을 선택했다 — team-lead 보고 대상, 이견 있으면 재조정).
    """
    if rvol_window.minutes != opening_range_minutes:
        raise ValueError(
            f"compute_symbol_day_features: rvol_window.minutes({rvol_window.minutes}) != "
            f"opening_range_minutes({opening_range_minutes}) — primary/sensitivity 창이 "
            "뒤바뀐 것으로 보인다.")

    # v1.1(lane3 F9) — 해시 기반 `set` 대신 삽입순서 보존 리스트로 모은다. `set` 을 쓰면 "정렬
    # 제거" 변조가 PYTHONHASHSEED 에 따라 우연히 알파벳순과 같은 순서로 나올 수 있어(리뷰어
    # 실측: 12개 시드 중 3개), 정렬 유무를 판별하는 테스트가 시드 의존적이 된다. 리스트는
    # 항상 결정론적 삽입순(OR→gap→RVOL)이므로, 그 순서는 알파벳순과 다르다는 것이 구조적으로
    # 보장된다 — "정렬을 지웠을 때 테스트가 죽는가"가 더는 운에 좌우되지 않는다.
    reasons: list[str] = []

    def _add_reason(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    or_value, or_reasons = opening_range_for_spec(
        today_day_candles, minutes=opening_range_minutes, day=day)
    for r in or_reasons:
        _add_reason(r)

    if prev_day is None:
        gap_value: float | None = None
        _add_reason("no_previous_regular_close")
    else:
        gap_value, gap_reasons = gap_for_day(
            today_regular_1m=today_regular_1m, day=day, prev_day=prev_day,
            prev_day_regular_1m=prev_day_regular_1m)
        for r in gap_reasons:
            _add_reason(r)

    today_sum = cum_today_volume(today_regular_1m, day=day, minutes=opening_range_minutes)
    rvol_value = rvol_window.rvol(day, today_sum)
    if rvol_value is None:
        _add_reason("insufficient_rvol_lookback")

    # 인과성 강제(A1): 오늘의 rvol() 을 계산한 **뒤에만** 오늘을 이력에 반영한다. 호출자가
    # 별도로 push 할 필요가 없어졌으므로(그럴 자리가 없어졌으므로) 여기서 한 번만 수행한다.
    rvol_window.push(
        day, window_sum_for_lookback(today_regular_1m, day=day, minutes=opening_range_minutes))

    return SymbolDayFeatures(
        symbol=symbol, day=day, opening_range=or_value, gap=gap_value, rvol=rvol_value,
        excluded_reasons=tuple(sorted(reasons)),
    )
