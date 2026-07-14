# market_context — US 최소 시장 국면 관측 (v2.1 Phase 2 · PR 2-B)

벤치마크(기본 **QQQ**)의 완료봉으로 시장 국면 v1 스냅샷(`BenchmarkContext`)을 만든다.
**관측 전용**이며 매매 행동을 바꾸지 않는다. 계약: `docs/DESIGN_DELTA_V2_1.md` §2-3(trailing·as-of·완료봉), §3-6(간접 행동변경 방지).

## 공개 API

- `build_benchmark_context(candles, *, decision_time, symbol, timeframe="5m", ema_period=20, trailing_min=(30,60)) -> BenchmarkContext | None`
  — 순수·결정론적. 완료봉(`ts_utc ≤ decision_time`)만 사용. 완료봉이 없으면 `None`.
- `BenchmarkContext` — v1 스냅샷(아래 필드) + `to_dict()`.
- `MarketContextConfig(enabled=False, benchmark_symbol="QQQ", timeframe="5m", ema_period=20)`.
- `MARKET_CONTEXT_VERSION`, `TREND_UP/DOWN/NEUTRAL`, `iso_z(dt)`.

## v1 필드

`benchmark_symbol / benchmark_close / benchmark_ema20 / benchmark_vs_ema20 /
benchmark_ema20_slope / benchmark_return_trailing_30m / benchmark_return_trailing_60m /
benchmark_context_asof / benchmark_source_bar_end / benchmark_timeframe / benchmark_trend /
market_context_version`

- `vs_ema20 = (close - ema20) / ema20`, `ema20_slope = (ema20_t - ema20_{t-1}) / ema20_{t-1}`.
- `return_trailing_Nm` = `N / timeframe` 봉 전 완료봉 대비 수익률(**과거** trailing, 미래 아님).
- `trend` = 종가 vs EMA20 + slope 로 UP/DOWN/NEUTRAL(지표 결측 시 None).
- 시각은 ISO-8601 UTC('...Z') 문자열.

## 불변식 (테스트로 강제)

- `source_bar_end ≤ context_asof ≤ decision_time` — 완료봉만, look-ahead 금지.
- `decision_time`/`source_bar_end` 은 cycle_id(=candidate decision) 와 동일한 `ts_utc` 그리드에
  앵커링 → `source_bar_end ≤ candidate_decision_time` 성립.
- warm-up/결측 지표는 `None`(관측이 매매를 막지 않는다).
- 같은 입력 → 같은 출력(정렬 무관, now()/난수 미사용) ⇒ **Paper=Backtest 패리티**.

## 하지 말 것 (금지)

- **매매를 게이팅하지 않는다.** 어떤 결정(AI/Risk/Order)에도 되먹임 금지. paper_loop 는
  `run_once` **이후**에만 관측을 만든다.
- **추가 API/fetch 금지.** 이미 universe 에서 가져온 candle 만 재사용한다. benchmark 가
  universe 에 없으면 관측을 생략한다(→ KRX benchmark 는 별도 예산 전 자연히 비활성).
  - **v1 트레이드오프**: 관측을 켜려면 `benchmark_symbol` 이 universe 에 있어야 하고, 그러면
    그 심볼도 매매 후보가 된다. "국면 지표 전용(매매 제외)" 벤치마크는 별도 fetch 경로가 필요해
    이후 단계로 미룬다(추가 API 0 이라는 §3-6 제약과 맞바꾼 의도된 결합).
- **미완료봉/미래봉 사용 금지.** `ts_utc ≤ decision_time` 필터 밖의 봉은 쓰지 않는다.
- Provider/Broker/Order/Risk 에 접근하지 않는다(순수 계산 + config 뿐).
