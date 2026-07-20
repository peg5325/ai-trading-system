"""장기 지평 forward return — EOD / 익일 / 2~3일 (read-only 연구 경로, H3).

왜 별 모듈인가: `candidate_stream.compute_forward_returns` 는 **세션 경계 clip** 이 계약이다
(앵커와 다른 ET 거래일 봉으로 수익률을 만들지 않는다 — 오버나이트 혼입 금지). H2-b 의
30m/60m 관측과 s001 패리티가 그 계약 위에 서 있으므로 **그 함수는 손대지 않는다.**

H3 는 반대로 "국면 효과가 긴 지평에 있는가"를 묻는다(예비 관측에서 60m > 30m). 그래서
**의도적으로** 거래일을 넘는 지평이 필요하다. 계약이 정반대인 두 규약을 한 함수에 넣으면
어느 쪽 호출자가 어떤 clip 을 받는지 알 수 없어진다 → 분리한다.

--------------------------------------------------------------------------------
규약
--------------------------------------------------------------------------------
- 앵커 = 결정 봉(라벨 T)의 **종가** (30m/60m 과 동일 — H2-b 와 앵커가 같아야 비교가 성립).
- **EOD** = 앵커와 **같은 ET 거래일**의 마지막 REGULAR 세션 봉 종가(= 정규장 마감).
  앵커가 이미 POST 면 마감이 지났으므로 **미완결**(`eod_passed`) — 조용히 전일/익일로
  넘어가지 않는다.
- **Nd** = 앵커 ET 거래일로부터 **N 거래일 뒤**의 REGULAR 마감 종가. 달력일이 아니라
  데이터에 실제로 존재하는 거래일을 센다(휴장일 자동 스킵).
- **strict look-ahead**: 목표 봉의 ts_utc 는 항상 앵커 ts_utc 보다 **뒤(>)** 여야 한다.
- 데이터 부족/공백은 **미완결로 표기**한다. 조용한 drop 도, 0 대입도 하지 않는다.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import date

from src.models import Candle

# 정규장 마감을 정의하는 세션 라벨. PRE/POST 는 '마감'이 아니다.
REGULAR = "REGULAR"

# 기본 일 단위 지평.
DEFAULT_DAY_HORIZONS = (1, 2, 3)


@dataclass(frozen=True)
class ExtendedForward:
    """EOD/Nd 지평 1건. complete=False 면 집계에서 제외되어야 한다.

    `horizon_key` 는 'eod' | '1d' | '2d' | '3d' — 분(minute) 라벨을 쓰지 않는 이유는
    거래일 경계를 넘는 지평의 실현 분(minute)이 휴장/세션 길이에 따라 가변이기 때문이다.
    실제 경과 분은 `realized_minutes` 에 따로 기록한다.
    """
    horizon_key: str
    complete: bool
    return_pct: float | None = None
    anchor_price: float | None = None
    exit_price: float | None = None
    exit_ts_utc: str | None = None
    exit_et_date: str | None = None
    # eod_passed | no_regular_close | no_future_day | bad_anchor | not_forward
    incomplete_reason: str | None = None
    realized_minutes: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def regular_closes_by_date(bars: list[Candle]) -> dict[date, Candle]:
    """ET 거래일 → 그 날의 **마지막 REGULAR 봉**. 정규장 마감 종가의 원천.

    REGULAR 봉이 하루도 없는 날(데이터 공백/조기 종료)은 키 자체를 만들지 않는다 —
    PRE/POST 봉으로 '마감'을 대신 채우면 조용히 다른 것을 재는 셈이 된다.
    """
    out: dict[date, Candle] = {}
    for b in bars:
        if b.session != REGULAR:
            continue
        d = b.ts_exchange.date()
        cur = out.get(d)
        if cur is None or b.ts_utc > cur.ts_utc:
            out[d] = b
    return out


def _incomplete(key: str, anchor_price: float | None, reason: str) -> dict:
    return ExtendedForward(key, False, anchor_price=anchor_price,
                           incomplete_reason=reason).to_dict()


def _realized(anchor: Candle, exit_bar: Candle) -> int:
    return int(round((exit_bar.ts_utc - anchor.ts_utc).total_seconds() / 60.0))


def _forward(key: str, anchor: Candle, exit_bar: Candle) -> dict:
    anchor_price = anchor.close
    ret = (exit_bar.close - anchor_price) / anchor_price * 100.0
    return ExtendedForward(
        key, True, return_pct=ret, anchor_price=anchor_price,
        exit_price=exit_bar.close, exit_ts_utc=exit_bar.ts_utc.isoformat(),
        exit_et_date=exit_bar.ts_exchange.date().isoformat(),
        realized_minutes=_realized(anchor, exit_bar)).to_dict()


def compute_extended_forwards(anchor: Candle, *, closes: dict[date, Candle],
                              trading_days: list[date],
                              day_horizons: tuple[int, ...] = DEFAULT_DAY_HORIZONS,
                              ) -> dict:
    """앵커 봉 기준 EOD/Nd forward return 들.

    closes/trading_days 는 `regular_closes_by_date` 산출로 **종목마다 한 번만** 만들어
    넘긴다(봉마다 재계산하면 O(n^2)).

    trading_days 는 closes 의 키를 정렬한 것 — 즉 "정규장 마감이 실제로 존재하는 날"만
    거래일로 센다. 데이터가 없는 날을 거래일로 세면 Nd 가 조용히 다른 날을 가리킨다.
    """
    anchor_price = anchor.close
    anchor_date = anchor.ts_exchange.date()
    out: dict = {}

    if not anchor_price:
        # 종가 0/None — 수익률 정의 불가. 모든 지평을 같은 사유로 미완결 처리.
        keys = ["eod"] + [f"{n}d" for n in day_horizons]
        return {k: _incomplete(k, anchor_price, "bad_anchor") for k in keys}

    # --- EOD: 같은 거래일의 정규장 마감 ---
    eod_bar = closes.get(anchor_date)
    if eod_bar is None:
        out["eod"] = _incomplete("eod", anchor_price, "no_regular_close")
    elif eod_bar.ts_utc <= anchor.ts_utc:
        # 앵커가 POST(또는 마감봉 자체) — 마감이 이미 지났다. 다음 날로 넘기지 않는다.
        out["eod"] = _incomplete("eod", anchor_price, "eod_passed")
    else:
        out["eod"] = _forward("eod", anchor, eod_bar)

    # --- Nd: N 거래일 뒤 정규장 마감 ---
    # bisect_right → anchor_date 보다 **뒤**인 첫 거래일의 위치. anchor_date 가 거래일
    # 목록에 없어도(그 날 정규장 마감이 없어도) 올바르게 '다음 거래일'을 가리킨다.
    nxt = bisect_right(trading_days, anchor_date)
    for n in day_horizons:
        key = f"{n}d"
        j = nxt + (n - 1)
        if j >= len(trading_days):
            out[key] = _incomplete(key, anchor_price, "no_future_day")
            continue
        exit_bar = closes[trading_days[j]]
        if exit_bar.ts_utc <= anchor.ts_utc:
            # 방어적: 거래일 순서상 일어나면 안 되지만, 일어나면 조용히 통과시키지 않는다.
            out[key] = _incomplete(key, anchor_price, "not_forward")
            continue
        out[key] = _forward(key, anchor, exit_bar)

    return out


def extended_horizon_keys(day_horizons: tuple[int, ...] = DEFAULT_DAY_HORIZONS) -> list[str]:
    return ["eod"] + [f"{n}d" for n in day_horizons]
