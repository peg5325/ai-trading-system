"""시장 국면 라벨 조인 — look-ahead 차단이 이 모듈의 존재 이유다 (read-only 연구, H3).

--------------------------------------------------------------------------------
왜 이 모듈이 있는가
--------------------------------------------------------------------------------
예비 관측에서 하루 국면을 **오늘 종가**로 계산했더니 UP−DOWN = +0.120%p (p<0.001) 가
나왔다. 같은 국면을 **전날 종가까지만**으로 바꾸자 +0.006%p 로 붕괴했다. 미세한
look-ahead 하나가 가짜 신호를 ~15배 부풀린 것이다. 그래서 이 모듈의 설계 목표는
"국면을 계산한다"가 아니라 **"결정 시점에 알 수 없는 값이 라벨에 새어들지 못하게 한다"** 다.

방어선 3겹:
  1. 국면 계산은 라이브 계산기 `src.market_context.builder.build_benchmark_context` 를
     **재사용**한다(임의 재구현 금지). 그 함수 자체가 `ts_utc <= decision_time` 완료봉만 쓴다.
  2. 모든 라벨은 `asof` 타임스탬프를 함께 싣는다. `asof <= decision_time` 불변식을
     테스트로 강제한다(`assert_no_lookahead`).
  3. 하루 국면은 **앵커 거래일보다 엄격히 이전(<)** 인 일봉만 본다 — 당일 종가는 물론
     당일 봉 자체에 접근하지 않는다.

--------------------------------------------------------------------------------
결정 시점 정렬 — 왜 `decision_time = 봉 라벨 T` 가 look-ahead 가 아닌가
--------------------------------------------------------------------------------
봉은 **left-label** 이다: 라벨 T 인 봉은 [T, T+5) 를 덮고 그 종가는 T+5 에 확정된다.
후보(candidate)의 앵커도 라벨 T 봉의 종가이며 forward return 은 그 종가에서 출발한다
(`candidate_stream` forward return 규약). 즉 후보 결정이 실효되는 순간은 **T+5** 다.
벤치마크의 라벨 T 봉 종가도 정확히 같은 순간에 확정된다. 따라서 `decision_time = T` 로
국면을 계산하면 후보와 **동일 정보 집합**을 쓰는 것이고 미래 정보가 아니다.

라이브 실측이 이를 뒷받침한다: us-phase6-s001 92 사이클 중 89 건에서
`benchmark_source_bar_end == benchmark_context_asof == 그 사이클의 newest 봉 라벨` 이었다
(나머지 3 건은 QQQ 데이터 도착이 한 봉 늦어 라이브가 T-5 를 썼다 — 미래 정보 문제가
아니라 데이터 도착 지연이다. `tests/test_s001_benchmark_parity.py` 참조).

--------------------------------------------------------------------------------
warm-up 윈도 (성능) — 왜 결과가 바뀌지 않는가
--------------------------------------------------------------------------------
builder 는 호출마다 전체 candle 목록을 훑는다. 결정 봉마다 부르면 O(n^2) 라 12 종목
14k 봉에서 수십 분이 든다. 그래서 각 호출에 **직전 `warmup` 개 봉만** 넘긴다.

이게 안전한 이유: EMA 는 `adjust=False` 재귀식이라 t 시점 값에 대한 k 봉 이전 관측의
기여가 `(1-alpha)^k` 로 감쇠한다. EMA20 은 alpha=2/21 이라 k=500 에서 6e-23,
EMA200 은 alpha=2/201 이라 k=4000 에서 ~1e-17 — float64 해상도 아래다. 기본 warm-up 은
`max(600, 25 * ema_period)` 로 이 여유를 강제로 확보한다. **자르는 게 아니라 수렴을 쓰는
것**이며, `tests/test_regime_join.py::test_windowed_context_matches_full_history` 가
전체 이력 호출과 1e-9 이내 일치를 실데이터로 고정한다.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right, insort
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from src.market_context.builder import build_benchmark_context
from src.market_context.models import TREND_DOWN, TREND_UP
from src.models import Candle

# 라이브 config 기본값(`MarketContextConfig`)과 동일. 국면 정의가 라이브와 갈라지지 않게
# 여기서 재선언하지 않고 의미만 맞춘다.
BENCHMARK_SYMBOL = "QQQ"
INTRADAY_EMA_PERIOD = 20          # (A) 라이브 기본
LONG_EMA_PERIOD = 200             # (C) 장기 — 5m 200봉 ≈ 정규장 2.5일
DAILY_EMA_PERIOD = 20             # (B) 하루 lagged
REGULAR = "REGULAR"

# (F) 변동성: 실현변동성 창과 그 기준선 창(둘 다 완료봉만 → 인과적).
VOL_WINDOW_BARS = 78              # 5m 78봉 ≈ 정규장 1일
VOL_REF_BARS = 1560               # ≈ 20 거래일치 5m 봉

# breadth 계산 시 허용하는 최대 stale 분. 이보다 오래된 봉밖에 없는 종목은 breadth 에서
# 제외한다 — 공백 난 종목의 낡은 상태를 '현재 시장 폭'으로 세면 조용히 틀린다.
BREADTH_MAX_STALE_MIN = 15
# breadth 를 신뢰할 최소 종목 수. 이하이면 None(관측 없음).
BREADTH_MIN_SYMBOLS = 6

UP, DOWN = TREND_UP, TREND_DOWN


def default_warmup(ema_period: int) -> int:
    """EMA 기여가 float64 해상도 아래로 감쇠하는 데 충분한 봉 수."""
    return max(600, 25 * int(ema_period))


@dataclass(frozen=True)
class RegimeLabels:
    """한 결정 시점의 국면 라벨 묶음. 모든 라벨은 자기 `*_asof` 를 함께 싣는다.

    `*_asof` 가 없으면 그 라벨이 언제 정보로 확정됐는지 사후에 검증할 수 없다 —
    look-ahead 를 잡아내는 유일한 증거라서 라벨과 반드시 한 몸으로 다닌다.
    """
    decision_ts_utc: str
    et_date: str
    # (A) 장중 5m 추세 — 라이브 기본 정의
    intraday_trend: str | None = None
    intraday_vs_ema: float | None = None
    intraday_asof: str | None = None
    # (C) 장기 5m EMA200
    long_trend: str | None = None
    long_vs_ema: float | None = None
    long_asof: str | None = None
    # (B) 하루 국면 (전날까지만)
    daily_trend: str | None = None
    daily_vs_ema: float | None = None
    daily_asof: str | None = None
    # (D) breadth
    breadth: float | None = None
    breadth_symbols: int = 0
    breadth_regime: str | None = None
    breadth_asof: str | None = None
    # (F) 변동성 국면
    vol_ratio: float | None = None
    vol_regime: str | None = None
    vol_asof: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def asof_fields(self) -> list[tuple[str, str | None]]:
        """(라벨명, asof) 쌍 — 불변식 검사용."""
        return [("intraday", self.intraday_asof), ("long", self.long_asof),
                ("daily", self.daily_asof), ("breadth", self.breadth_asof),
                ("vol", self.vol_asof)]


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# builder 재사용 (warm-up 윈도)
# ---------------------------------------------------------------------------

def windowed_context(bars: list[Candle], i: int, *, symbol: str, ema_period: int,
                     timeframe: str = "5m", warmup: int | None = None):
    """`bars[i]` 를 마지막 완료봉으로 하는 국면 스냅샷. 라이브 builder 그대로 호출.

    decision_time 을 `bars[i].ts_utc` 로 두므로 builder 내부 필터(`ts_utc <= decision_time`)가
    i 이후 봉을 **구조적으로** 배제한다. 슬라이스는 성능용일 뿐이고, 슬라이스가 없어도
    결과가 같아야 한다(테스트로 고정).
    """
    if not 0 <= i < len(bars):
        raise IndexError(f"bar index {i} 범위 밖 (n={len(bars)})")
    w = default_warmup(ema_period) if warmup is None else warmup
    lo = max(0, i + 1 - w)
    return build_benchmark_context(bars[lo:i + 1], decision_time=bars[i].ts_utc,
                                   symbol=symbol, timeframe=timeframe,
                                   ema_period=ema_period)


def context_series(bars: list[Candle], *, symbol: str, ema_period: int,
                   timeframe: str = "5m", warmup: int | None = None) -> list:
    """모든 봉에 대한 국면 스냅샷(인덱스 정렬). 봉 i 결과는 i 이하 봉만 반영한다."""
    return [windowed_context(bars, i, symbol=symbol, ema_period=ema_period,
                             timeframe=timeframe, warmup=warmup)
            for i in range(len(bars))]


def trend_of(ctx) -> str | None:
    return None if ctx is None else ctx.benchmark_trend


# ---------------------------------------------------------------------------
# (B) 하루 국면 — 전날까지만
# ---------------------------------------------------------------------------

def daily_bars(bars: list[Candle], symbol: str) -> list[Candle]:
    """5m 봉 → ET 거래일별 **정규장** 일봉. 라벨 = 그 날 마지막 REGULAR 봉의 ts_utc.

    라벨을 '마감이 확정된 실제 시각'으로 두는 게 핵심이다. 자정이나 날짜 문자열로
    라벨링하면 `decision_time` 비교가 시각이 아니라 날짜 비교로 흐려져서, 당일 봉이
    슬쩍 포함돼도 불변식 검사가 잡아내지 못한다.

    OHLC 는 정규장 봉만으로 만든다 — PRE/POST 를 섞으면 '일간 종가'가 라이브·통념의
    마감가와 달라져 국면 정의가 조용히 다른 것을 재게 된다.
    """
    by_day: dict[date, list[Candle]] = {}
    for b in bars:
        if b.session != REGULAR:
            continue
        by_day.setdefault(b.ts_exchange.date(), []).append(b)
    out: list[Candle] = []
    for d in sorted(by_day):
        day = sorted(by_day[d], key=lambda c: c.ts_utc)
        last = day[-1]
        out.append(Candle(
            symbol=symbol, interval="1d", ts_utc=last.ts_utc,
            ts_exchange=last.ts_exchange, session=REGULAR,
            open=day[0].open, high=max(c.high for c in day),
            low=min(c.low for c in day), close=last.close,
            volume=sum(c.volume for c in day),
            currency=last.currency, adjusted=last.adjusted))
    return out


def daily_context_index(dbars: list[Candle], *, symbol: str,
                        ema_period: int = DAILY_EMA_PERIOD) -> tuple[list[date], list]:
    """(거래일 목록, 그 날 마감 기준 국면) — 일봉 국면을 한 번만 계산해 둔다.

    timeframe='1d' 로 builder 를 부르면 `_infer_tf_minutes` 가 봉 간격(≈1440분)을 데이터에서
    추론하고, trailing_min=(30,60) 은 `round(30/1440)=0` → None 이 된다(일봉에 30분 trailing
    은 정의되지 않으므로 올바른 결측 처리다).
    """
    days = [b.ts_exchange.date() for b in dbars]
    ctxs = [windowed_context(dbars, i, symbol=symbol, ema_period=ema_period,
                             timeframe="1d", warmup=default_warmup(ema_period))
            for i in range(len(dbars))]
    return days, ctxs


def daily_context_before(days: list[date], ctxs: list, anchor_date: date):
    """앵커 거래일보다 **엄격히 이전(<)** 인 마지막 일봉 국면. 없으면 None.

    `bisect_left` 는 anchor_date 와 같은 날의 위치를 반환하므로 그 앞(-1)이 '전날까지'다.
    `bisect_right` 를 쓰면 당일이 포함돼 정확히 이 모듈이 막으려는 look-ahead 가 된다.
    """
    j = bisect_left(days, anchor_date) - 1
    if j < 0:
        return None
    return ctxs[j]


def daily_context_including(days: list[date], ctxs: list, anchor_date: date):
    """⚠ **오염된(look-ahead) 하루 국면 — 음성 대조군 전용. 분석에 쓰면 안 된다.**

    앵커 거래일 **당일 마감**까지 포함해 국면을 만든다. 결정 시점(장중)에는 알 수 없는
    당일 종가를 쓰므로 명백한 look-ahead 다.

    왜 일부러 만드는가: 예비 관측에서 이 오염 하나가 신호를 +0.006%p → +0.120%p 로
    ~15배 부풀렸다. 정상 라벨과 나란히 측정해 **"우리 차단 장치가 실제로 일을 하고 있다"**
    를 수치로 보여주기 위한 음성 대조군이다. 이 값은 `RegimeLabels` 에 들어가지 않으며
    (그래서 `assert_no_lookahead` 의 대상도 아니다), 리포트의 대조군 절에서만 쓰인다.

    `daily_context_before` 가 `bisect_left`(당일 제외)인 것과 달리 여기는
    `bisect_right`(당일 포함)다 — 이 한 글자가 예비 관측을 15배 부풀린 차이다.
    """
    j = bisect_right(days, anchor_date) - 1
    if j < 0:
        return None
    return ctxs[j]


# ---------------------------------------------------------------------------
# (D) breadth — 유니버스 중 EMA20 위 비율
# ---------------------------------------------------------------------------

@dataclass
class BreadthIndex:
    """종목별 (봉 시각, EMA 대비 위치) 사전계산. 결정 시각마다 bisect 로 조회한다."""
    ts_by_symbol: dict[str, list[datetime]] = field(default_factory=dict)
    vs_by_symbol: dict[str, list[float | None]] = field(default_factory=dict)

    def add(self, symbol: str, bars: list[Candle], ctxs: list) -> None:
        self.ts_by_symbol[symbol] = [b.ts_utc for b in bars]
        self.vs_by_symbol[symbol] = [
            None if c is None else c.benchmark_vs_ema20 for c in ctxs]

    def at(self, decision_time: datetime, *,
           max_stale_min: int = BREADTH_MAX_STALE_MIN) -> tuple[float | None, int, datetime | None]:
        """(breadth, 참여 종목 수, 가장 늦은 asof). 참여 종목이 부족하면 (None, n, asof).

        각 종목에서 `ts <= decision_time` 인 마지막 봉만 본다 → look-ahead 불가.
        너무 낡은 봉(공백 구간)은 제외한다.
        """
        above = 0
        used = 0
        newest: datetime | None = None
        cutoff = decision_time - timedelta(minutes=max_stale_min)
        for sym, ts in self.ts_by_symbol.items():
            k = bisect_right(ts, decision_time) - 1
            if k < 0:
                continue
            bar_ts = ts[k]
            if bar_ts < cutoff:
                continue                      # stale — 이 종목은 breadth 에서 뺀다
            v = self.vs_by_symbol[sym][k]
            if v is None:                     # EMA warm-up 미완
                continue
            used += 1
            if v > 0:
                above += 1
            if newest is None or bar_ts > newest:
                newest = bar_ts
        if used < BREADTH_MIN_SYMBOLS:
            return None, used, newest
        return above / used, used, newest


def breadth_regime(breadth: float | None) -> str | None:
    """과반이 EMA20 위면 UP. 0.5 정확히는 UP 도 DOWN 도 아니다(None)."""
    if breadth is None:
        return None
    if breadth > 0.5:
        return UP
    if breadth < 0.5:
        return DOWN
    return None


# ---------------------------------------------------------------------------
# (F) 변동성 국면 — 인과적 기준선
# ---------------------------------------------------------------------------

def volatility_series(bars: list[Candle], *, window: int = VOL_WINDOW_BARS,
                      ref_bars: int = VOL_REF_BARS) -> tuple[list[float | None], list[float | None]]:
    """(실현변동성, 기준선) — 둘 다 **인덱스 i 이하 봉만** 사용한다.

    ⚠ 여기가 look-ahead 가 가장 새기 쉬운 지점이다. 전체 표본 분위수로 HIGH/LOW 를 나누면
    "그 기간 전체의 변동성 분포"를 결정 시점에 아는 셈이 된다 — 전형적인 미래 정보 누수다.
    그래서 기준선은 **자기 과거(직전 ref_bars 봉)의 중앙값**으로만 잡는다.

    반환값의 i 번째는 '봉 i 종가 시점'의 값이다. warm-up 미달이면 None.
    """
    closes = [float(b.close) for b in bars]
    n = len(closes)
    rets: list[float | None] = [None] * n
    for i in range(1, n):
        prev = closes[i - 1]
        if prev > 0 and closes[i] > 0:
            rets[i] = math.log(closes[i] / prev)

    vol: list[float | None] = [None] * n
    for i in range(n):
        lo = i - window + 1
        if lo < 1:
            continue
        seg = [r for r in rets[lo:i + 1] if r is not None]
        if len(seg) < window // 2:
            continue
        m = sum(seg) / len(seg)
        var = sum((r - m) ** 2 for r in seg) / (len(seg) - 1) if len(seg) > 1 else 0.0
        vol[i] = math.sqrt(var)

    # 기준선 = 직전 ref_bars 봉의 실현변동성 중앙값. 봉마다 창 전체를 정렬하면
    # O(n · w log w) 라 백필이 길어질수록 눈에 띄게 느려진다. 정렬 상태를 유지하는
    # 슬라이딩 창으로 바꿔 한 봉당 삽입/삭제 각 1회만 한다(비교는 O(log w),
    # 이동은 C 레벨 memmove). **결과는 동일해야 하며**, 순진한 구현과의 일치를
    # `test_regime_join.py::test_volatility_ref_matches_naive_median` 이 고정한다.
    ref: list[float | None] = [None] * n
    # 이름은 `window`(파라미터)와 반드시 달라야 한다 — 같은 이름을 쓰면 실현변동성 창
    # 크기(int)를 가려서, 이 아래에 그 파라미터를 쓰는 코드가 추가될 때 조용히 리스트를
    # 받는다. 여기는 look-ahead 가 가장 새기 쉬운 함수라 그런 사고가 비싸다.
    ref_window: list[float] = []        # 현재 창 안의 non-None 값(오름차순 유지)
    for i in range(n):
        if vol[i] is not None:
            insort(ref_window, vol[i])
        lo = i - ref_bars + 1
        if lo > 0:
            old = vol[lo - 1]           # 창 밖으로 나간 봉
            if old is not None:
                ref_window.pop(bisect_left(ref_window, old))
        if len(ref_window) < ref_bars // 4:
            continue
        mid = len(ref_window) // 2
        ref[i] = (ref_window[mid] if len(ref_window) % 2
                  else (ref_window[mid - 1] + ref_window[mid]) / 2.0)
    return vol, ref


def vol_regime(vol: float | None, ref: float | None) -> tuple[float | None, str | None]:
    """(비율, HIGH/LOW). 기준선 대비 위면 HIGH.

    라벨을 UP/DOWN 이 아니라 HIGH/LOW 로 두는 이유: 변동성은 방향이 아니라 크기다.
    UP/DOWN 을 재사용하면 표에서 추세 국면과 같은 축으로 읽혀 오독된다.
    """
    if vol is None or not ref:
        return None, None
    r = vol / ref
    return r, ("HIGH" if r > 1.0 else "LOW")


# ---------------------------------------------------------------------------
# 불변식 — look-ahead 차단의 최종 방어선
# ---------------------------------------------------------------------------

class LookAheadError(AssertionError):
    """국면 라벨이 결정 시점 이후 정보를 참조했다."""


def assert_no_lookahead(labels: RegimeLabels) -> None:
    """모든 `*_asof <= decision_time`. 하루 국면은 **엄격히 이전 거래일**이어야 한다.

    조용히 걸러내지 않고 예외를 던진다 — 이 위반이 통과하면 리포트의 모든 수치가
    무의미해지므로, 크게 실패하는 편이 옳다.
    """
    dt = _parse(labels.decision_ts_utc)
    for name, asof in labels.asof_fields():
        if asof is None:
            continue
        a = _parse(asof)
        if a > dt:
            raise LookAheadError(
                f"{name}_asof({asof}) > decision_time({labels.decision_ts_utc}) — "
                f"국면 라벨이 미래 정보를 참조한다")
    if labels.daily_asof is not None:
        # `_et_date_of` 가 자체적으로 ET 로 변환하므로 여기서 tz 를 맞출 필요가 없다.
        d_asof = _parse(labels.daily_asof)
        anchor_day = date.fromisoformat(labels.et_date)
        # 일봉 라벨은 그 날 정규장 마감 시각이다. 앵커 거래일과 같은 날이면 당일 종가를
        # 쓴 것 → 예비 관측을 15배 부풀린 바로 그 오염이다.
        if _et_date_of(d_asof) >= anchor_day:
            raise LookAheadError(
                f"daily_asof({labels.daily_asof}) 가 앵커 거래일({labels.et_date}) 이상 — "
                f"하루 국면이 당일 정보를 썼다")


def _et_date_of(dt: datetime) -> date:
    from src.models import ET
    return dt.astimezone(ET).date()
