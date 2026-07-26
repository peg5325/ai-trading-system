"""pre-#76 리샘플러와의 **차등(differential) 동치** — 라이브 동작 불변을 코드로 고정한다.

이 PR 의 핵심 안전 주장은 "새 `closed_only`+frontier 계약이 기존 라이브
`resample(..., drop_partial=True)`(= pandas resample + `iloc[:-1]`)와 **관측 동치**이며
라이브 결정 시점이 바뀌지 않는다" 이다. 그런데 parity receipt 가 비교하는 네 경로는 전부
**새 코드**다 — 옛 동작을 붙잡아 두는 것이 저장소에 하나도 없었다(독립 리뷰 MEDIUM).

그래서 pre-#76 구현(merge `e2e1c27` 시점의 `src/engines/indicator_engine.py`)을 **원문 그대로**
여기에 박아두고 새 구현과 대조한다. 이후 누군가 frontier 계약을 바꾸면 이 테스트가 먼저 깨진다.

★ synthetic 입력만 쓴다(성과·blind OOS 미접근).
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

import pandas as pd
import pytest

from src.engines.indicator_engine import candles_to_frame, resample
from src.models import ET, KST, Candle, classify_session

# ---------------------------------------------------------------------------
# pre-#76 구현 (git show e2e1c27:src/engines/indicator_engine.py — 원문 보존)
# ---------------------------------------------------------------------------
_LEGACY_FRAME_COLUMNS = ["open", "high", "low", "close", "volume",
                         "currency", "session", "ts_exchange", "et_date"]


def _legacy_empty_frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz="UTC", name="ts_utc")
    return pd.DataFrame({c: pd.Series(dtype="object") for c in _LEGACY_FRAME_COLUMNS},
                        index=idx)


def legacy_candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    if not candles:
        return _legacy_empty_frame()
    rows = []
    for c in candles:
        rows.append({
            "ts_utc": c.ts_utc,
            "open": float(c.open), "high": float(c.high),
            "low": float(c.low), "close": float(c.close),
            "volume": float(c.volume),
            "currency": c.currency, "session": c.session,
            "ts_exchange": c.ts_exchange, "et_date": c.ts_exchange.date(),
        })
    df = pd.DataFrame(rows).set_index("ts_utc").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.index = pd.DatetimeIndex(df.index, name="ts_utc")
    return df


def _legacy_add_meta(out: pd.DataFrame, currency: str) -> pd.DataFrame:
    out = out.copy()
    if len(out):
        et = out.index.tz_convert(ET)
        out["currency"] = currency
        out["session"] = [classify_session(t) for t in et]
        out["ts_exchange"] = list(et)
        out["et_date"] = [t.date() for t in et]
    return out


def legacy_resample(df: pd.DataFrame, rule: str = "5min",
                    drop_partial: bool = True) -> pd.DataFrame:
    if df.empty:
        return _legacy_empty_frame()
    agg = {"open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum"}
    out = df.resample(rule, label="left", closed="left").agg(agg)
    out = out.dropna(subset=["open"])
    currency = df["currency"].iloc[0] if "currency" in df.columns and len(df) else ""
    out = _legacy_add_meta(out, currency)
    if drop_partial and len(out):
        out = out.iloc[:-1]
    return out


# ---------------------------------------------------------------------------
# 비교 도구
# ---------------------------------------------------------------------------
def ohlcv_rows(df: pd.DataFrame) -> list[tuple]:
    """두 구현이 **공통으로** 보장하는 필드만 비교(옛 구현은 market/adjusted 를 안 만든다)."""
    return [(ts.isoformat(), float(r["open"]), float(r["high"]), float(r["low"]),
             float(r["close"]), float(r["volume"]))
            for ts, r in df.iterrows()]


def mk(symbol: str, start: datetime, count: int, *, rng=None,
       currency: str = "USD", tz=ET) -> list[Candle]:
    """1분봉 생성. 거래량은 **정수 주식수**(실데이터와 같은 성질)로 만든다 — 이유는

    `test_float_volume_sums_differ_only_within_ulp` 참조."""
    out = []
    for i in range(count):
        base = 100.0 + (rng.uniform(-5, 5) if rng else i)
        vol = float(rng.randint(1, 99999)) if rng else 10.0 * (i + 1)
        out.append(Candle.from_moment(
            symbol, "1m", start + timedelta(minutes=i),
            base, base + 2, base - 2, base + 1, vol, currency, True))
    return out


def assert_same(candles: list[Candle], rule: str, drop_partial: bool, label: str):
    new = resample(candles_to_frame(candles), rule, drop_partial=drop_partial)
    old = legacy_resample(legacy_candles_to_frame(candles), rule,
                          drop_partial=drop_partial)
    assert ohlcv_rows(new) == ohlcv_rows(old), f"{label} (rule={rule}, drop={drop_partial})"


# ---------------------------------------------------------------------------
# 수기 엣지 케이스
# ---------------------------------------------------------------------------
D = (2026, 3, 3)
CASES = {
    "정규장 full session": mk("SPY", datetime(*D, 9, 30, tzinfo=ET), 390),
    "단일 봉": mk("SPY", datetime(*D, 9, 30, tzinfo=ET), 1),
    "경계에서 정확히 끝남": mk("SPY", datetime(*D, 9, 30, tzinfo=ET), 5),
    "경계 직후 한 봉": mk("SPY", datetime(*D, 9, 30, tzinfo=ET), 6),
    "PRE→REGULAR 연속": (mk("SPY", datetime(*D, 9, 20, tzinfo=ET), 10)
                          + mk("SPY", datetime(*D, 9, 30, tzinfo=ET), 10)),
    "REGULAR→POST 연속": (mk("SPY", datetime(*D, 15, 50, tzinfo=ET), 10)
                           + mk("SPY", datetime(*D, 16, 0, tzinfo=ET), 10)),
    "PRE 전체": mk("SPY", datetime(*D, 4, 0, tzinfo=ET), 30),
    "OVERNIGHT 포함": mk("SPY", datetime(*D, 20, 0, tzinfo=ET), 30),
    "첫 분 결측": mk("SPY", datetime(*D, 9, 31, tzinfo=ET), 9),
    "복수 버킷 공백": (mk("SPY", datetime(*D, 9, 30, tzinfo=ET), 3)
                       + mk("SPY", datetime(*D, 10, 15, tzinfo=ET), 3)),
    "DST spring 직후": mk("SPY", datetime(2026, 3, 9, 9, 30, tzinfo=ET), 40),
    "DST fall 직후": mk("SPY", datetime(2026, 11, 2, 9, 30, tzinfo=ET), 40),
    "KR 정규장": mk("005930", datetime(*D, 9, 0, tzinfo=KST), 40, currency="KRW"),
    "장중 다일자": (mk("SPY", datetime(*D, 9, 30, tzinfo=ET), 30)
                    + mk("SPY", datetime(2026, 3, 4, 9, 30, tzinfo=ET), 30)),
}


@pytest.mark.parametrize("label", sorted(CASES))
@pytest.mark.parametrize("rule", ["5min", "15min"])
@pytest.mark.parametrize("drop_partial", [True, False])
def test_new_matches_pre76_on_edge_cases(label, rule, drop_partial):
    assert_same(CASES[label], rule, drop_partial, label)


def test_new_matches_pre76_on_randomized_inputs():
    """무작위(고정 seed) 입력 200건 — 결측·중복·순서 섞임 포함."""
    rng = random.Random(76)
    for i in range(200):
        n = rng.randint(1, 90)
        start_min = rng.choice([4 * 60, 9 * 60 + 28, 9 * 60 + 30, 15 * 60 + 50, 19 * 60])
        start = datetime(*D, start_min // 60, start_min % 60, tzinfo=ET)
        candles = mk("SPY", start, n, rng=rng)
        if candles and rng.random() < 0.5:            # 임의 결측
            keep = [c for c in candles if rng.random() > 0.2]
            candles = keep or candles
        if rng.random() < 0.3:                        # 중복(동일 값)
            candles = candles + [rng.choice(candles)]
        if rng.random() < 0.3:                        # 순서 섞기
            rng.shuffle(candles)
        rule = rng.choice(["5min", "15min"])
        assert_same(candles, rule, rng.choice([True, False]), f"random#{i}")


def test_legacy_would_fail_the_kr_session_contract():
    """옛 구현이 실제로 KR 을 틀렸음을 남겨둔다(이 PR 이 고친 것의 증거).

    옛 경로는 세션을 ET 로 재분류해 09:00 KST 를 `POST` 로 라벨했다. 새 경로는 원본을
    상속해 `REGULAR` 다. OHLCV 는 같아도 **세션 라벨은 달랐다**.
    """
    c = mk("005930", datetime(*D, 9, 0, tzinfo=KST), 20, currency="KRW")
    old = legacy_resample(legacy_candles_to_frame(c), "5min")
    new = resample(candles_to_frame(c), "5min")
    assert ohlcv_rows(old) == ohlcv_rows(new)          # 값은 동일
    assert set(old["session"]) == {"POST"}             # 옛: ET 재분류 → 오분류
    assert set(new["session"]) == {"REGULAR"}          # 새: 원본 상속
    assert set(new["market"]) == {"KR"}


def test_float_volume_sums_differ_only_in_summation_order():
    """비정수 거래량에서 두 구현의 합이 **부동소수 누적 순서** 때문에 갈릴 수 있다.

    옛 경로는 pandas/numpy 의 pairwise 합, 새 core 는 파이썬 순차 `sum` 이다. 오차는
    순차 O(n·ε) vs pairwise O(log n·ε) 라 **버킷 크기에 따라 커진다** — 독립 리뷰가 n=60
    에서 최대 5 ULP 를 실측했으므로 "1 ULP" 라고 못박지 않는다(여유 8 ULP 로 고정).

    실거래량은 정수 주식수라 두 합이 모두 정확하고, 실데이터 대조(29,322봉·14,536봉)는
    불일치 0 이었다. 경로 간 parity(core/adapter/candidate_stream/replay)는 **같은 core**
    를 쓰므로 이 뉘앙스와 무관하게 bit-identical 이다.
    """
    rng = random.Random(760)
    start = datetime(*D, 9, 30, tzinfo=ET)
    candles = [
        Candle.from_moment("SPY", "1m", start + timedelta(minutes=i),
                           100.0 + i, 102.0 + i, 99.0 + i, 101.0 + i,
                           rng.uniform(1, 999), "USD", True)
        for i in range(40)
    ]
    new = ohlcv_rows(resample(candles_to_frame(candles), "15min"))
    old = ohlcv_rows(legacy_resample(legacy_candles_to_frame(candles), "15min"))
    assert len(new) == len(old)
    for a, b in zip(new, old):
        assert a[:5] == b[:5]                       # ts·OHLC 는 선택 연산 → 정확히 일치
        assert abs(a[5] - b[5]) <= 8 * math.ulp(b[5])   # rel 허용치는 주장보다 4000배 느슨했다
