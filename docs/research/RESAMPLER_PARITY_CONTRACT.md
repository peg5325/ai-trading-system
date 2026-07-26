# 리샘플러 동치 계약 (연구 ↔ 라이브 ↔ replay) — 이슈 #76

**상태: gate 해소.** DATA_CONTRACT §2.5 가 경고한 **이중 구현**이 제거됐고, "연구 5분봉 =
라이브 5분봉" 이 테스트와 실데이터로 증명된다. 이 문서가 그 계약의 정본이다.

- 관련: DATA_CONTRACT_4STRATEGY_V1.md §2.5 / AMENDMENT A1(#75) / **A2(#76)**
- 선행: #84/#85 clean snapshot 고정(merge `e2e1c27`)
- 후행: ORB 사전등록(3/6) — 이 gate 해소 전에는 ORB 공식 연구를 시작하지 않는다

## 1. 무엇이 문제였나

| 축 | 연구 `src/history/resample.py` | 라이브 `indicator_engine.resample` |
| --- | --- | --- |
| (a) 부분 버킷 | `min_bars` 필터(마지막 버킷 자동 drop 안 함) | `iloc[:-1]` 로 **마지막 행 무조건 제거** |
| (b) dedup | 순서 무관 `(o,h,l,c,v)` 최대 튜플 | `keep="last"` — **입력 순서 의존** |
| (c) 세션/market | 버킷 키에 session 포함, market-aware | 버킷 키에 session 없음, `classify_session` 을 **ET 고정** 호출 |
| 집계 엔진 | 수동 집계(pure) | `pandas.resample` |

두 구현이 공존하는 한 연구 결과를 Paper 로 옮길 때 **같은 1분봉에서 다른 결정봉**이 나올 수
있었다. 특히 (c)는 KR 캔들에 US 세션 라벨(예: 09:00 KST → `POST`)을 붙였다.

## 2. 통일 후 구조

```
1분봉(단일 진실원천)
   └── src/history/resample.py           ← ★ 유일한 집계 권위(pure, market-aware)
         ├── indicator_engine.resample           (DataFrame ↔ Candle thin adapter)
         │     ├── live/paper_loop.py
         │     ├── history/candidate_stream.py   (연구 결정봉)
         │     └── replay/system.py
         └── (연구 직접 호출) resample_1m_to_5m / _15m / opening_range
```

- `indicator_engine` 은 **집계·dedup·세션·버킷 경계를 스스로 계산하지 않는다.**
- `frame_to_candles` 도 **단일 구현**이다(`indicator_engine`). `paper_loop.frame_to_candles`
  는 하위호환 re-export 이고 `candidate_stream`·`replay` 가 같은 함수를 쓴다.
- pandas `resample` 은 권위 경로에서 제거됐다(빈 버킷 제거·left label 동작은 core 가 동일하게 보장).

## 3. 확정된 계약

### 3.1 두 축을 분리한다 — 이 계약의 핵심

| 축 | 파라미터 | 질문 | 위반 시 증상 |
| --- | --- | --- | --- |
| **시간축** | `closed_only` (+`evaluation_time`) | 이 버킷의 **구간이 끝났는가** | 진행 중 봉으로 결정 → look-ahead |
| **커버리지축** | `min_bars` / `require_complete` | 그 구간에 **1분봉이 몇 개** 있는가 | 데이터 공백을 완결로 오인 |

둘은 독립이다. 닫혔지만 결측이 있을 수 있고, 분이 다 찼지만 아직 안 닫혔을 수 있다.
호출자마다 다른 축을 몰래 쓰지 않는다.

### 3.2 완료 경계(frontier)

- `closed_only=True` → 버킷 `[t, t+Δ)` 는 **`t+Δ <= frontier`** 일 때만 방출한다.
- `frontier` = `evaluation_time`(명시) **또는 입력의 마지막 1분봉 `ts_utc`**.
- **`+1분` 하지 않는다.** 마지막 1분봉은 형성 중일 수 있으므로, 그 봉의 시작 시각까지만
  "시간이 진행됐다"고 인정한다(보수적). 결과적으로 기존 라이브 `drop_partial=True`
  (`iloc[:-1]`)와 **관측 동치**이며, 이번 변경으로 라이브 결정 시점(cadence)은 바뀌지 않는다.
- 버킷 종료는 **UTC 산술**(`시작 + Δ분`)이다 → DST 전환을 포함하는 버킷에서도 길이 왜곡 없음.
- frontier 는 입력 **전체**(복수 symbol 포함)의 최대 ts 다. 라이브 루프도 유니버스 전체의
  최신 봉으로 사이클을 판정하므로 같은 계약이다. 라이브는 종목별로 호출하므로 종목별 clock 이고,
  **stale 종목의 마지막 버킷 지연은 이 이슈에서 바꾸지 않는다 → #44 의 범위**다.
- wall clock·난수 미사용. 동일 입력 → 동일 출력.

### 3.3 dedup / 순서

- 같은 `(symbol, ts_utc)` 중복은 **`(open, high, low, close, volume)` 사전식 최대 튜플**을
  채택한다(입력 순서 무관). 값이 같은 정상 중복에서는 결과가 동일하다.
- 중복은 **frontier 를 앞당기지 못한다**(dedup 후 최대 ts 사용).
- 출력 정렬은 `(ts_utc, symbol, session)` 오름차순.
- 프레임 경로(`candles_to_frame`, `IndicatorEngine.compute`)도 같은 규칙을 쓴다
  (기존 `keep="last"` 제거).

### 3.4 세션 · market · DST

- 버킷 키 = `(symbol, session, 버킷 시작(거래소 로컬, offset 포함))`.
  → 한 버킷이 PRE/REGULAR/POST 를 걸치지 않는다. 그리드 정렬에 의존하지 않는 **구조적** 보장.
- 버킷 경계는 **거래소 로컬 분**을 Δ로 floor 한다(US=ET, KR=KST). US 하드코딩 없음.
- session/`ts_exchange`/`market`/`currency`/`adjusted` 는 원본 1분봉에서 **상속**하고 재계산하지
  않는다. 컬럼이 없는 프레임에서만 통화로 market 을 추정한 뒤 그 거래소 tz/세션 규칙을 쓴다.
- DST 가을 fold(로컬 01:00 이 두 번): 로컬 라벨은 같아도 UTC offset 이 달라 **버킷이 분리**된다.
- `et_date`(VWAP 일일 리셋 키)는 이제 **거래소 로컬 날짜**다(KR 은 KST 날짜). 컬럼명은 하위호환 유지.

### 3.5 interval 라벨

- 파생봉 라벨은 `"5m"`/`"15m"` 로 통일한다. `candidate_stream` 이 rule 문자열 `"5min"` 을
  라벨로 쓰던 불일치를 제거했다(`DECISION_INTERVAL = "5m"`).
- 리샘플 입력은 **1분봉만** 허용한다(파생봉 재리샘플 fail-closed).
- rule 은 `5min`/`15min`(및 `5m`/`15m`)만 허용, 그 외는 `ValueError`.
- `evaluation_time` 을 `closed_only=False` 와 함께 주면 조용히 무시하지 않고 `ValueError`.

## 4. 증거

### 4.1 계약 테스트 — `tests/test_resampler_parity.py` (33 tests, 전부 통과)

| 요구 항목 | 테스트 |
| --- | --- |
| 09:30~09:34 완전 5m | `test_complete_5m_bucket_0930_0934` |
| 09:30~09:44 완전 15m | `test_complete_15m_bucket_0930_0944` |
| 진행 중 마지막 버킷 | `test_in_progress_last_bucket_is_withheld` |
| 정확히 완료된 경계 | `test_exact_close_boundary_needs_evidence_past_end` |
| 첫 분 결측 / 중간 분 결측 | `test_missing_first_minute_labels_bucket_start` / `test_missing_middle_minute_does_not_shift_bucket` |
| 중복 ts 동일값 / 충돌값 | `test_duplicate_ts_same_value_is_idempotent` / `test_duplicate_ts_conflicting_value_uses_max_tuple_regardless_of_order` |
| 입력 reverse/shuffle | `test_input_order_invariance_reverse_and_shuffle` |
| PRE→REGULAR / REGULAR→POST 경계 | `test_pre_to_regular_boundary_not_merged` / `test_regular_to_post_boundary_not_merged` |
| 세션 버킷키(구조적) | `test_session_in_bucket_key_is_structural_not_grid_luck` |
| US DST 전환 전후 | `test_us_dst_spring_forward_labels_stay_local`, `test_us_dst_fall_back_repeated_hour_does_not_merge`, `test_bucket_end_uses_utc_arithmetic_across_transition` |
| KR market session | `test_kr_market_session_and_timezone_not_et_hardcoded`, `test_kr_pre_and_regular_sessions_split` |
| 복수 symbol 혼합 | `test_multi_symbol_input_grouped_and_sorted`, `test_multi_symbol_frontier_is_market_wide` |
| replay vs research | `test_replay_equals_live_path` |
| candidate_stream vs live | `test_candidate_stream_equals_live_path`(+`_kr`) |
| research core vs live | `test_research_core_equals_live_adapter`(+`_15m`) |
| full-session 회귀 | `test_full_regular_session_regression` |
| 빈 입력·잘못된 interval·rule fail-closed | `test_empty_input_is_empty_not_error`, `test_non_1m_input_fails_closed`, `test_unsupported_rule_fails_closed`, `test_unsupported_bucket_and_bad_args_fail_closed` |
| 커버리지축/시간축 분리 | `test_min_bars_is_coverage_axis_not_time_axis`, `test_opening_range_shares_the_same_axes` |

CLI 계약: `tests/test_resampler_parity_cli.py` (6 tests).

### 4.2 실데이터 parity — `docs/research/RESAMPLER_PARITY_RECEIPT.json`

고정된 clean snapshot(`p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite`,
file SHA `3c2c4751…`, content SHA `d0d14551…`)에서 **coverage 목적**으로만 로드했다.

| 항목 | 값 |
| --- | ---: |
| 셀(종목×거래일) | 36 / 데이터 있음 36 |
| **불일치** | **0** |
| 1m 입력 봉 | 29,322 |
| 파생 5m / 15m 봉 | 6,087 / 2,041 |
| blind OOS 안쪽 거래일 | **0** |

- 종목: SPY, QQQ, NVDA, TSLA, SOXL, SOXS
- 거래일: 2025-03-10(DST 시작 직후), 2025-11-03(DST 해제 직후), 2025-11-28·2025-12-24(조기폐장),
  2026-01-02, 2026-01-20(blind OOS 직전)
- 비교 필드: `ts_utc`, `ts_exchange`, `symbol`, `interval`, `session`, OHLCV, `currency`,
  `adjusted`, `market`
- 비교 경로: research core / live adapter / candidate_stream / replay — **4경로 byte-equivalent**

재현:

```bash
python scripts/resampler_parity_check.py \
  --db data/research_snapshots/p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite \
  --out reports/RESAMPLER_PARITY_RECEIPT.json
```

## 5. 연구 규율 (이 작업에서 하지 않은 것)

- 전략 성과·forward return·expectancy·PF/MDD/Sharpe·leaderboard **미계산·미열람**.
- blind OOS(intraday 2026-01-21~2026-07-21) 표본 **미사용**(receipt `days_inside_window=0`).
  CLI 는 OOS 날짜를 기본 거부하고 `--allow-oos-coverage` 명시가 있어야 coverage 로만 연다.
- 로드는 공식 게이트 `oos_guard.research_load(purpose="coverage")` 만 경유한다
  (`load_candles` 직접 사용 금지 — AST 경계 테스트).
- 주문·broker·risk·Paper·전략·runtime config **변경 없음**. canonical DB **변경 없음**.

## 6. 알려진 한계 / 후속

- **stale 종목 EOD 지연(#44)**: 종목별 clock 에서는 마지막 REGULAR 버킷이 다음 세션 봉이
  올 때까지 닫히지 않는다. 이번 통일은 그 동작을 **보존**했다(고의). fail-safe 는 #44 에서 다룬다.
- **조기폐장**: 커버리지 판정(`quality.py`)의 390분 가정은 그대로다(계약 §2.4의 알려진 한계).
  리샘플 자체는 조기폐장일에도 parity 를 만족한다(2025-11-28·12-24 실측).
- **KR 실데이터 parity 미실측**: 이 스냅샷은 US-30 전용이라 KR 은 synthetic 테스트로만 검증했다.
- DataFrame 어댑터는 **단일 symbol 프레임** 전용이다(인덱스가 ts 뿐). 복수 symbol 은 Candle API.
