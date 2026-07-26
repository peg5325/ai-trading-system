# src/engines — 지표 계산 (IndicatorEngine)

## 1. 목적

결정론적 지표 계산. **backtest 와 live 가 완전히 동일한 코드로 지표를 얻도록** 하는 단일 소스.
`indicators.py` 는 순수 pandas 함수, `indicator_engine.py` 는 `Candle` → 지표 DataFrame 오케스트레이터.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `list[Candle]` 또는 `pd.DataFrame` |
| 출력 | tz-aware UTC `DatetimeIndex`(name=`ts_utc`) 의 `pd.DataFrame` |

출력 컬럼은 항상 `OUTPUT_COLUMNS` (`indicator_engine.py:19`):
`open, high, low, close, volume, ema7, ema20, rsi14, vwap, bb_mid, bb_upper, bb_lower, stochrsi_k, stochrsi_d`

## 3. 주요 클래스/함수

- `IndicatorEngine(params: IndicatorParams | None = None)` — `indicator_engine.py:111`
- `.compute(data: list[Candle] | pd.DataFrame) -> pd.DataFrame`.
  인덱스 정렬 + **순서 무관** 중복 제거(`(o,h,l,c,v)` 최대 튜플, 이슈 #76). 빈 입력이면 빈 프레임(컬럼 유지).
- `IndicatorParams` (frozen) — `:98`: `ema_fast=7, ema_slow=20, rsi_period=14, bb_period=20,
  bb_std=2.0, stoch_rsi_period=14, stoch_period=14, stoch_k=3, stoch_d=3`
- `candles_to_frame(candles) -> pd.DataFrame` — `:34`
- `resample(df, rule="5min", drop_partial=True, *, evaluation_time=None) -> pd.DataFrame`
  — **`src/history/resample.py` canonical core 에 위임하는 thin adapter**(이슈 #76). 버킷은
  시작시각 라벨, 빈 버킷 없음. `drop_partial=True` 는 `iloc[:-1]` 이 아니라 **완료 경계**
  (버킷 종료 <= frontier) 판정이다. rule 은 `5min`/`15min`(`5m`/`15m`)만 허용 — 그 외 fail-closed.
- `frame_to_candles(df, symbol, *, interval=None) -> list[Candle]` — live/research/replay
  **공용 단일 변환**. session/`ts_exchange`/`market` 은 원본에서 상속한다.
- 순수 함수(`indicators.py`): `ema` `:21`, `rsi` `:27`(Wilder), `vwap` `:43`(세션 앵커),
  `bollinger` `:57`(모집단 std, ddof=0), `stoch_rsi` `:67`

## 4. 다른 모듈과의 관계

`src/models.py` 와 **집계 core `src/history/resample.py`** 만 import 한다(이슈 #76 단일 소스화 — 순환 없음: core 는 `src/models.py` 만 본다).
사용처: `src/backtest/engine.py`, `src/live/paper_loop.py`, `src/replay/system.py`, `src/pipeline/pipeline.py`.

## 5. 절대 하면 안 되는 것

- ❌ **Provider 에 접근하지 않는다.**
  `indicators.py:3`: *"Provider 에 접근하지 않는다. 입력은 pandas Series/DataFrame 뿐."*
  `indicator_engine.py:4`: *"Provider 에 직접 접근하지 않는다."*
- ❌ **진행 중(미완결) 캔들을 쓰지 않는다.** `resample` 은 닫히지 않은 버킷을 버린다(`drop_partial=True`).
  이걸 끄면 look-ahead 가 생긴다.
- ❌ **집계 로직을 이 모듈에 다시 만들지 않는다.** 권위는 `src/history/resample.py` 하나다(이슈 #76).
- ❌ backtest 전용 / live 전용 지표 분기 금지 — 두 경로가 갈라지는 순간 백테스트가 거짓말을 한다.
- ❌ 매매 판단·주문·저장 금지. 지표 계산만.

> ✅ 해소(이슈 #76): 과거 `_add_meta` 는 항상 ET 기준 `classify_session(t)` 을 불러 리샘플된 KR
> 데이터에 US 세션 라벨을 붙였다. 지금은 session/`ts_exchange`/`market` 을 **원본에서 상속**하고
> 재계산하지 않는다. 계약: `docs/research/RESAMPLER_PARITY_CONTRACT.md`.

## 6. 사용 예시

```python
from src.engines.indicator_engine import (
    IndicatorEngine, IndicatorParams, candles_to_frame, resample,
)

df5 = resample(candles_to_frame(candles), "5min", drop_partial=True)
ind = IndicatorEngine(IndicatorParams(ema_fast=7, ema_slow=20)).compute(df5)
```

## 7. 테스트

- `tests/test_indicators.py` — 순수 지표 함수(warm-up NaN 정책 포함)
- `tests/test_indicator_engine.py` — 엔진 + `resample`
- `tests/test_resampler_parity.py` — 연구↔라이브↔replay 동치 계약(이슈 #76)
