# src/universe — 종목 선별 (UniverseScreener)

> 설계 문서상 "Screener". 폴더명은 `universe/`, 테스트는 `tests/test_screener.py`.

## 1. 목적

사용자가 준 유니버스를 **결정론적 Rule 점수화**로 후보 리스트로 압축한다.
최종 BUY 결정이 아니라 **candidate list** 만 만든다 — 다음 단계는 AI entry filter + RiskManager.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `list[SymbolData]` (각각 `IndicatorEngine` 출력 DataFrame 을 감쌈) |
| 출력 | `list[Candidate]` — `symbol, score, rank, candidate_type, reasons[], risk_flags[]` |

## 3. 주요 클래스/함수

- `SymbolData.create(symbol, indicators, leverage_factor=None, market=MARKET_US)` — `screener.py:66`
  (레버리지는 `KNOWN_LEVERAGE` 에서 자동 해석: SOXL/TQQQ → 3, QLD/SSO → 2 …)
- `UniverseScreener(config: ScreenerConfig | None = None)` — `:120`
- `.screen(inputs) -> list[Candidate]` — `:124`. `LONG_SETUP` 만, `(-score, symbol)` 정렬(결정론적 tie-break), `rank` 1..n
- `.screen_all(inputs) -> list[Candidate]` — `:133`. `EXCLUDED` 포함, 랭킹 없음
- `ScreenerConfig` (frozen) — `:99`: `weights={trend .30, momentum .25, volume .20, vwap_position .15, volatility .10}`,
  `min_avg_volume=300.0`, `min_score=45.0`, `extreme_vol_bb_width=0.15` …
- `leverage_for(symbol, override=None) -> int` — `:51`

**하드 제외**(즉시 score 0): 데이터 부족, `avg_vol < min_avg_volume`, `bb_width > extreme_vol_bb_width`.
**소프트 감점**: 고변동성, 시간외, 마감임박. score 는 `[0,100]` clamp.

## 4. 다른 모듈과의 관계

`src/models.py` 만 import 하는 **leaf**.
사용처: `src/pipeline/pipeline.py`, `src/backtest/engine.py`, `src/live/paper_loop.py`(`leverage_for`).

## 5. 절대 하면 안 되는 것

- ❌ **AI 가 종목을 직접 고르지 않는다.** 도크스트링: *"결정론적 Rule 점수화로 후보를 압축한다."*
  선별은 코드가, 필터는 AI 가, 최종 승인은 RiskManager 가 한다.
- ❌ **Provider/Broker 에 접근하지 않는다.** *"입력은 IndicatorEngine 결과(+캔들)뿐."*
- ❌ **최종 BUY 를 내지 않는다.** *"candidate list 만 생성."*
- ❌ 난수·현재시각 사용 금지 — 같은 입력이면 같은 랭킹이 나와야 한다.

## 6. 사용 예시

```python
from src.universe.screener import ScreenerConfig, SymbolData, UniverseScreener

inputs = [
    SymbolData.create("SOXL", ind_df),                    # leverage 자동 → 3
    SymbolData.create("005930", ind_df_kr, market="KR"),
]
cands = UniverseScreener(ScreenerConfig(min_score=45.0)).screen(inputs)
rows  = UniverseScreener().screen_all(inputs)             # EXCLUDED 포함(디버깅용)
```

## 7. 테스트

`tests/test_screener.py` (주 테스트). `tests/test_krx.py` 가 KR 경로를 종단 검증.
