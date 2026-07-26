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
   └── src/bars/resample.py              ← ★ 유일한 집계 권위(pure leaf, models 만 import)
         ├── indicator_engine.resample           (DataFrame ↔ Candle thin adapter)
         │     ├── live/paper_loop.py
         │     ├── history/candidate_stream.py   (연구 결정봉)
         │     └── replay/system.py
         ├── src/history/resample.py             (하위호환 re-export shim)
         └── (연구 직접 호출) resample_1m_to_5m / _15m / opening_range
```

core 를 `src/history` 가 아니라 **leaf `src/bars`** 에 둔 이유(독립 리뷰 R1): 라이브 결정
경로가 `src/history/__init__.py`(loader·store·quality·sqlite3)를 실행하게 되고,
`src/history/candidate_stream` 이 engines 를 되짚어 import 하므로 패키지 초기화 순환에
노출된다. 이 경계는 `test_live_import_path_does_not_pull_history_package` 로 고정했다.

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
- **frontier 는 symbol 별**이다 → `resample(A∪B) == resample(A) ∪ resample(B)` (합성 가능).
  라이브(`paper_loop`)가 **종목별로** 리샘플하므로, 연구가 하루치 30종목을 한 번에 로드해도
  결과가 라이브와 같아야 한다. frontier 를 입력 전체의 최대 ts 로 잡으면 신선한 종목이 stale
  종목의 버킷을 닫아버려 연구≠라이브가 재발한다(독립 리뷰 B1 가 실측: AAA 단독 73봉 vs
  신선한 BBB 와 함께 74봉). 유니버스 전체의 최신 봉은 라이브에서 **사이클 트리거**이지
  리샘플 경계가 아니다.
- 시장 전체 시계가 필요한 호출자는 `evaluation_time` 으로 **명시**한다(암묵적 cross-symbol 누출 금지).
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

### 4.1 계약 테스트 — `tests/test_resampler_parity.py` (47 tests, 전부 통과)

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
| 복수 symbol 혼합·합성 가능성 | `test_multi_symbol_input_grouped_and_sorted`, `test_multi_symbol_frontier_is_per_symbol_and_compositional`, `test_market_wide_clock_requires_explicit_evaluation_time`, `test_batched_multi_symbol_matches_per_symbol_live_path` |
| replay vs research | `test_replay_equals_live_path` |
| candidate_stream vs live | `test_candidate_stream_equals_live_path`(+`_kr`) |
| research core vs live | `test_research_core_equals_live_adapter`(+`_15m`) |
| full-session 회귀 | `test_full_regular_session_regression` |
| 빈 입력·잘못된 interval·rule fail-closed | `test_empty_input_is_empty_not_error`, `test_non_1m_input_fails_closed`, `test_unsupported_rule_fails_closed`, `test_unsupported_bucket_and_bad_args_fail_closed` |
| 커버리지축/시간축 분리 | `test_min_bars_is_coverage_axis_not_time_axis`, `test_opening_range_shares_the_same_axes` |
| opening range 라벨=개장 시각 | `test_opening_range_labels_window_start_even_when_first_minutes_missing`, `test_opening_range_closed_boundary_with_missing_first_minutes` |
| 레이어 경계(라이브가 history 를 안 끌어옴) | `test_live_import_path_does_not_pull_history_package`, `test_shim_reexports_are_the_same_objects` |
| **어댑터** dedup·상속·타입(핵심: core 만 보면 라이브가 다를 수 있다) | `test_adapter_dedup_matches_core_on_conflicting_duplicates`, `test_adapter_inherits_adjusted_false`, `test_adapter_inherits_session_column_over_reclassifying`, `test_adapter_ts_exchange_is_plain_datetime_like_core`, `test_adapter_default_interval_label_is_canonical`, `test_adapter_forwards_evaluation_time_to_core` |
| 프레임이 표현 못 하는 입력 fail-closed | `test_adapter_rejects_same_ts_multiple_buckets_instead_of_dropping` |
| tz-naive 거부(머신 TZ 의존 금지) | `test_adapter_rejects_timezone_naive_frames` |
| **pre-#76 차등 동치**(옛 동작 고정) | `tests/test_resampler_legacy_equivalence.py` — 수기 14케이스×2 rule×2 partial + 무작위 200건 + KR 오분류 회귀 + 부동소수 ULP |
| **checker 가 '아니오' 라고 말하는지** | `test_cli_actually_detects_each_divergence`(5개 탐지 분기 전부) |

차등 동치: `tests/test_resampler_legacy_equivalence.py` (59 tests).
CLI 계약: `tests/test_resampler_parity_cli.py` (13 tests — snapshot identity·OOS 거부·sanitized receipt fail-closed 포함).

### 4.2 실데이터 parity — `docs/research/RESAMPLER_PARITY_RECEIPT.json`

고정된 clean snapshot(`p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite`)에서 **coverage
목적**으로만 로드했다. CLI 는 basename 대조에 그치지 않고 **파일 SHA-256 을 직접 재계산**해
tracked receipt(`3c2c4751…`)와 대조하고 불일치 시 fail-closed 한다(독립 리뷰 B2 — 이전에는
이름만 맞으면 canonical hash 가 박힌 receipt 가 나올 수 있었다). receipt 의
`snapshot.identity_verified=true` 와 `file_sha256_recomputed` 가 그 증거다.

| 항목 | 값 |
| --- | ---: |
| 셀(종목×거래일) | 36 / 데이터 있음 36 |
| **불일치** | **0** |
| 1m 입력 봉 | 29,322 |
| 파생 5m / 15m 봉 | 6,087 / 2,041 |
| 15m opening range 봉 | 36 (전 셀, 09:30 15m 버킷과 OHLCV·라벨 일치) |
| snapshot file SHA-256 재계산 검증 | **일치**(`identity_verified: true`) |
| blind OOS 안쪽 거래일 | **0** |

- 종목: SPY, QQQ, NVDA, TSLA, SOXL, SOXS
- 거래일: 2025-03-10(DST 시작 직후), 2025-11-03(DST 해제 직후), 2025-11-28·2025-12-24(조기폐장),
  2026-01-02, 2026-01-20(blind OOS 직전)
- 비교 필드: `ts_utc`, `ts_exchange`, `symbol`, `interval`, `session`, OHLCV, `currency`,
  `adjusted`, `market`
- 비교 경로: research core / live adapter / candidate_stream / replay — **4경로 byte-equivalent**
- 추가 불변식(실데이터): 입력 순서 섞기 불변 · `opening_range(15m)` == 09:30 15m 버킷
  (OHLCV·라벨) — ORB 의 실제 결정 객체를 같은 core 에 묶는다(독립 리뷰 R4)

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

## 6. Stage 3(사전등록)이 반드시 정해야 할 것

리샘플 계약이 **답을 주지 않는** 항목이다. 사전등록 동결 후에는 바꿀 수 없으므로 prereg 본문에
명시해야 한다(독립 리뷰 R3).

- **세션 마지막 봉**: 보수적 frontier 때문에 정규장 마지막 버킷 `[15:55,16:00)` 은 POST 봉이
  없으면 닫히지 않는다. 390분 정규장 1분봉 → 닫힌 5m **77개, 마지막 라벨 15:50**. ORB 는 EOD
  청산이므로 prereg 는 둘 중 하나를 고정해야 한다:
  (a) POST 포함 로드로 자연히 닫히게 하거나, (b) `evaluation_time=16:00 ET`(조기폐장일은 13:00)
  를 명시. 조기폐장일에도 같은 규칙을 적용한다.
- **opening range 확정 시각**: `closed_only=True` 는 창 종료(09:45) 증거를 요구한다. 진입은
  그 다음 거래 가능 봉이므로, prereg 는 "OR 확정 → 최초 진입 가능 봉"의 정확한 라벨을 적어야 한다.

## 7. 알려진 한계 / 후속

- **stale 종목 EOD 지연(#44)**: 종목별 clock 에서는 마지막 REGULAR 버킷이 다음 세션 봉이
  올 때까지 닫히지 않는다. 이번 통일은 그 동작을 **보존**했다(고의). fail-safe 는 #44 에서 다룬다.
- **조기폐장**: 커버리지 판정(`quality.py`)의 390분 가정은 그대로다(계약 §2.4의 알려진 한계).
  리샘플 자체는 조기폐장일에도 parity 를 만족한다(2025-11-28·12-24 실측).
- **KR 실데이터 parity 미실측**: 이 스냅샷은 US-30 전용이라 KR 은 synthetic 테스트로만 검증했다.
- DataFrame 어댑터는 **단일 symbol 프레임** 전용이다(인덱스가 ts 뿐). 복수 symbol 은 Candle API.
- `src/backtest/engine.py` 는 여전히 ET 고정 `classify_session`/`et_dates` 를 쓴다(같은 결함
  분류, 이 PR 범위 밖). ORB stage 4~5 가 그 경로를 쓰므로 **후속 이슈 #88** 로 분리했다.
- `candidate_stream.extract` 의 US 전용 게이트는 유지한다 — 봉 생성은 market-aware 가 됐지만
  스크리너·거래일 경계·KR 캘린더가 미검증이다.

### 6.1 legacy A/B 대조 (독립 리뷰 R7 — 병합 후 재현 불가하므로 기록)

독립 아키텍처 리뷰가 pre-#76 구현(`git show e2e1c27:src/engines/indicator_engine.py`)을 복원해
신·구를 A/B 비교했다. **US 5m/15m 은 bar-for-bar 동일**(full session, PRE→POST, 13:00 조기폐장,
데이터 공백, DST fall-back), KR 세션 라벨만 신 구현에서 정정됐다. 즉 "라이브 관측 동치·
cadence 불변" 주장은 독립 재현으로 확인됐다.
