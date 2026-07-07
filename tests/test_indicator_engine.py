"""IndicatorEngine 테스트: 정렬, 빈/부족 데이터, 리샘플링 집계, 저장데이터 기반."""
from __future__ import annotations

from datetime import datetime, timedelta

from src.models import ET, Candle
from src.engines.indicator_engine import (
    OUTPUT_COLUMNS,
    IndicatorEngine,
    candles_to_frame,
    resample,
)
from src.storage.candle_store import CandleStore

ENGINE = IndicatorEngine()


def make_1m(n: int, start=datetime(2026, 7, 6, 9, 30, tzinfo=ET), base=100.0) -> list[Candle]:
    """정규장 시작(09:30 ET)부터 연속 1분봉 n개 (결정론적)."""
    candles: list[Candle] = []
    prev = base
    for i in range(n):
        moment = start + timedelta(minutes=i)
        close = base + (i % 20) * 0.5
        openp = prev
        high = max(openp, close) + 0.2
        low = min(openp, close) - 0.2
        candles.append(Candle.from_moment(
            "SOXL", "1m", moment, openp, high, low, close, 1000 + i, "USD", True))
        prev = close
    return candles


# ---- 정렬 / 컬럼 ----
def test_compute_alignment_and_columns():
    candles = make_1m(60)
    res = ENGINE.compute(candles)
    assert list(res.columns) == OUTPUT_COLUMNS
    assert list(res.index) == [c.ts_utc for c in candles]   # ts_utc 정렬 일치
    assert len(res) == 60


def test_compute_accepts_dataframe():
    candles = make_1m(30)
    df = candles_to_frame(candles)
    res = ENGINE.compute(df)                 # DataFrame 입력도 허용
    assert len(res) == 30
    assert list(res.index) == list(df.index)


# ---- 빈 데이터 ----
def test_compute_empty():
    res = ENGINE.compute([])
    assert len(res) == 0
    assert list(res.columns) == OUTPUT_COLUMNS


# ---- 부족한 데이터 ----
def test_compute_insufficient_data_is_nan_not_crash():
    candles = make_1m(3)
    res = ENGINE.compute(candles)
    assert len(res) == 3
    assert res["ema7"].isna().all()
    assert res["ema20"].isna().all()
    assert res["rsi14"].isna().all()
    assert res["bb_mid"].isna().all()
    # VWAP 는 누적이라 부족 데이터에서도 값이 있다
    assert res["vwap"].notna().all()


# ---- 리샘플 집계 정확성 ----
def test_resample_aggregation_ohlcv():
    # 6개 1m -> [09:30,09:35) 한 버킷(5개) + 09:35(부분, drop)
    specs = [
        # o,    h,     l,    c
        (100, 101, 99, 100),
        (100, 105, 98, 104),
        (104, 104, 100, 101),
        (101, 103, 97, 102),
        (102, 106, 101, 105),
        (105, 107, 104, 106),   # 09:35 -> 다음 버킷(부분) => drop
    ]
    start = datetime(2026, 7, 6, 9, 30, tzinfo=ET)
    candles = []
    for i, (o, h, l, c) in enumerate(specs):
        candles.append(Candle.from_moment(
            "SOXL", "1m", start + timedelta(minutes=i),
            float(o), float(h), float(l), float(c), (i + 1) * 10, "USD", True))
    df5 = resample(candles_to_frame(candles), "5min", drop_partial=True)
    assert len(df5) == 1
    row = df5.iloc[0]
    assert row["open"] == 100        # 첫 봉 open
    assert row["high"] == 106        # 5개 중 최고
    assert row["low"] == 97          # 5개 중 최저
    assert row["close"] == 105       # 마지막(09:34) close
    assert row["volume"] == 10 + 20 + 30 + 40 + 50   # 합


def test_resample_drop_partial_flag():
    candles = make_1m(12)            # 09:30..09:41 -> 버킷 [30,35),[35,40),[40,45)
    full = resample(candles_to_frame(candles), "5min", drop_partial=False)
    trimmed = resample(candles_to_frame(candles), "5min", drop_partial=True)
    assert len(full) - len(trimmed) == 1


# ---- 요구사항 9: 저장 데이터 기반 5분 리샘플 후 지표 계산 ----
def test_resample_from_store_then_compute(tmp_path):
    with CandleStore(tmp_path / "market.sqlite") as store:
        store.upsert_many(make_1m(60))       # SOXL 1분봉 60개 저장
        loaded = store.query("SOXL", "1m")   # 오름차순 조회
        assert len(loaded) == 60

        df = candles_to_frame(loaded)
        df5 = resample(df, "5min", drop_partial=True)  # 60 -> 12버킷 -> 11(마지막 부분 제거)
        assert len(df5) == 11

        res = ENGINE.compute(df5)
        assert list(res.index) == list(df5.index)        # 정렬 일치
        assert res["ema7"].notna().any()                 # warm-up 후 값 존재
        assert res["close"].notna().all()
