# 4전략 시장데이터 수집 계약 v1 (DATA CONTRACT)

> 상태: **FROZEN (결과 확인 전 고정)** · 이슈 #68 · Phase 1A
> 목적: 대량 backfill 이전에 **수집 계약을 전략 성과·forward return·blind OOS 를 보지 않고 고정**한다.
> 이 문서는 계약이다. 여기 고정된 규칙은 결과를 본 뒤 완화·변경하지 않는다(불변식 §6).
> 정본 계획: `.omx/plans/one-month-strategy-selection-ai-shadow.md` (Phase 1).
> 근거 실측: `docs/research/PROBE_REPORT_TOSS_CAPABILITY_V1.md`.
> 기존 설계: `docs/MARKET_HISTORY_BACKFILL_DESIGN.md`.

이 계약은 4개 전략을 **모두** 지원하도록 데이터를 넓게 확보하되, 이번 달 연구 실행은
**VCP-lite 일봉 스윙 1개**에 우선 집중한다(나머지 3개는 데이터만 선확보).

대상 전략:
1. VCP/Cup-style Swing Breakout (VCP-lite) — 이번 달 우선
2. Liquid-universe Opening Range Breakout (ORB)
3. Gap + Relative Volume Continuation
4. VWAP/ATR Extreme Mean Reversion

---

## 1. 유니버스

### 1.1 P0 고정 유니버스 (US 30종목)

기계 판독본: **`config/universe_p0_us30.yaml`** (tracked, 이 계약의 정본 심볼 목록).
아래 표는 그 사본이며 충돌 시 YAML 을 우선한다.

| # | class | symbols |
|---|---|---|
| 시장 벤치마크(4) | `benchmark` | SPY, QQQ, IWM, DIA |
| 섹터 ETF(8) | `sector_etf` | XLK, XLF, XLE, XLV, XLI, XLY, XLU, SMH |
| 대형 성장·반도체(14) | `large_cap` | AAPL, MSFT, NVDA, AMD, AVGO, AMZN, META, GOOGL, TSLA, MU, NFLX, CRM, PLTR, VST |
| 레버리지·인버스(4) | `leveraged_etf` / `inverse_etf` | TQQQ, SQQQ, SOXL, SOXS |

합계 **30종목**. 이 목록은 `.omx/plans/one-month-strategy-selection-ai-shadow.md §3` 및
`.omx/handoffs/2026-07-21-one-month-strategy-ai-handoff.md §2` 에서 합의된 **공통 엔지니어링
유니버스**와 동일하다.

포함 이유:
- **benchmark**: SPY/QQQ 는 상대강도·국면·비교 기준선. IWM(소형)·DIA(대형가치)로 스타일 분산.
- **sector_etf**: 섹터 로테이션·상대강도·gap/ORB 의 섹터 편중 진단. SMH 는 반도체(개별 반도체주와 쌍).
- **large_cap**: 4전략의 신호가 실제로 발생하는 고유동성 종목. 반도체(NVDA/AMD/AVGO/MU)·
  빅테크·성장(PLTR/VST) 포함으로 gap·ORB·VCP·MR 신호 다양성 확보.
- **leveraged/inverse**: 3x(TQQQ/SOXL)·역방향(SQQQ/SOXS)은 **자연실험**(동일 기초자산의
  변동성 증폭). 결과는 1x 와 **분리 보고**하고 독립 종목 증거로 중복 계산하지 않는다
  (`plan §3 공통 체결·리스크`).

### 1.2 구분 라벨

각 심볼은 `config/universe_p0_us30.yaml` 에서 `class ∈ {benchmark, sector_etf, large_cap,
leveraged_etf, inverse_etf}` 과 `beta_ref`(레버리지의 기초자산, 예 SOXL→SMH, TQQQ→QQQ)를 갖는다.
레버리지/인버스는 `leverage`(±1/±2/±3)로 실질노출 계산과 분리 보고에 쓴다.

### 1.3 상장폐지·ticker 변경·신규 상장

- **Survivorship**: P0-30 은 **현재 고유동성 생존 종목 표본**이다. 결과를 이 표본 밖으로
  일반화하지 않는다(`plan §4 Phase1 중단조건`). 상장폐지 종목은 P0-30 에 없다(survivorship
  편향을 **명시적으로 인지**하고 그 범위로만 결론).
- **신규 상장(짧은 이력)**: 일부 종목은 상장일이 늦어 일봉 이력이 5년 미만일 수 있다.
  본 계약의 5년 일봉 요건은 "상장일 이후 가능한 전 구간"으로 해석하며, backfill 시
  **심볼별 실제 earliest 를 manifest 에 기록**한다(PLTR 2020-09 IPO, VST 2016-10 스핀오프
  등 — 둘 다 2026-07 기준 ≥5년). 5년 미만 종목이 있으면 그 종목은 스윙 공식 판정에서
  `INCONCLUSIVE_DATA` 로 격리한다(기준 완화 금지).
- **Ticker 변경**: P0-30 창(window) 내 변경 예정 종목은 없다. 발생 시 `source` provenance 와
  별도 매핑 테이블로 연결하고 조용히 병합하지 않는다.

### 1.4 P1 확장 (이번 작업 범위 밖 — 후속)

횡단면 선별(ORB/Gap/VCP)의 공식 일반화에는 point-in-time 100~300종목이 필요하다
(`plan §3`). **P1 100~300 확장은 별도 후속 이슈**로 분리하며 이번 Phase 1A 범위가 아니다.
P1 은 별도 manifest·별도 survivorship 처리(상장폐지 포함 point-in-time 유니버스)를 요구한다.

---

## 2. 시간 범위와 봉(bar)

### 2.1 필수 범위 (실측 근거 §PROBE)

| track | interval | 최소 범위 | blind OOS | 공급자 실측 |
|---|---|---|---|---|
| Swing | `1d` (일봉) | **최소 5년** 수정 일봉 | 최근 **12개월** | Toss ≥10년(2016-03~) 확인 |
| Intraday | `1m` (1분봉) | **최소 3년**, 세션 구분 | 최근 **6개월** | Toss ≥3년(<5년) 확인 |
| Intraday | `5m` (5분봉) | 1분봉에서 **결정론적 파생** | (1m 상속) | 원천 다운로드 안 함 |

### 2.2 세션 구분 (US, 거래소 로컬 = America/New_York)

`src/models.py:classify_session` 을 정본 규칙으로 사용한다(변경 없음):

| session | US 로컬(ET) 구간 |
|---|---|
| `PRE` | 04:00 ≤ t < 09:30 |
| `REGULAR` | 09:30 ≤ t < 16:00 |
| `POST` | 16:00 ≤ t < 20:00 |
| `OVERNIGHT` | 그 외(20:00~익일 04:00) — Toss 미국주식 24h |

**수집 범위**: 1분봉은 **PRE + REGULAR + POST 를 필수**로 저장한다. `OVERNIGHT`(20:00~04:00 ET)는
Toss 가 제공하나 거래가 희박하고 저장량을 ~30~50% 늘리므로 **선택(optional)** 으로 둔다
(§4 저장 정책). 4전략(ORB/Gap/VWAP-MR/VCP)은 PRE/REGULAR/POST 만 사용한다.

### 2.3 저장 기준 시간대 = UTC + DST

- 정렬·조인·PK 키는 **`ts_utc`(ISO8601 UTC)**. 거래소 로컬 `ts_exchange`(ET)를 함께 저장.
- **DST 는 `zoneinfo` 로 자동 처리**된다(`ET = ZoneInfo("America/New_York")`, `src/models.py:15`).
  ET↔UTC 오프셋은 EDT(-04:00)/EST(-05:00)로 계절에 따라 자동 전환된다.
- **일봉 timestamp 의 DST(실측 검증됨)**: Toss 는 일봉을 **거래일 00:00 ET 에 고정**해서 준다.
  KST offset 표기로는 여름(EDT) `13:00+09:00` = **`04:00Z`** = 00:00 EDT, 겨울(EST)
  `14:00+09:00` = **`05:00Z`** = 00:00 EST. 즉 **UTC 시각은 DST 로 1시간 다르지만(04:00Z vs 05:00Z)
  거래소 로컬은 둘 다 00:00 ET**.
  → `astimezone(ET).date()` 는 **연중 항상 올바른 거래일**을 준다(날짜 밀림 없음, §PROBE 검증).
  단 00:00 ET 일봉은 `classify_session` 상 `OVERNIGHT` 로 라벨된다 — **일봉의 session 라벨은
  무의미**하며, 일봉의 "날짜"는 `ts_exchange` 의 date 로 해석한다.

### 2.4 조기폐장·휴장일

- **정규장 창**은 `src/history/quality.py:REG_WINDOW`(US 09:30–16:00 = 390분) 기준으로 커버리지를
  집계한다. **조기폐장일**(예: 추수감사절 다음날·크리스마스이브 13:00 ET 마감, 연 ~3일)은
  현재 도구가 **정규 390분 기준으로 결측을 과대계상**한다. 이는 **알려진 한계**이며, blind OOS
  구간의 조기폐장일은 **사전 정의된 예외일 목록**으로 설명한다(coverage 미달을 결측이 아니라
  예외로 처리; `plan §Phase1 합격기준`). 거래소 휴장일 캘린더(NYSE)는 Phase 1B 에서 도입한다.
- 현재 결측일 판정은 **주말만 제외**(공휴일 미반영, `quality.py:_weekdays`). 공휴일을 결측으로
  오탐하지 않도록 blind OOS 판정 시 **NYSE 휴장/조기폐장 캘린더**를 예외 목록으로 적용한다(Phase 1B).

### 2.5 5분봉 파생 규칙 (결정론)

5분봉은 **원천 다운로드하지 않고 1분봉에서 결정론적으로 파생**한다
(`src/history/resample.py:resample_1m_to_5m`, 이 작업에서 신설·TDD).

- **bucket 경계**: 거래소 로컬(ET) 분(minute) 을 5로 내림(floor) → `[00,05), [05,10), …`.
  즉 09:30 봉은 09:30~09:34 의 1분봉으로 만든다. bucket 라벨 timestamp 는 **bucket 시작 시각**.
- **집계**: open=첫 1분봉 open, high=max, low=min, close=마지막 1분봉 close, volume=합.
  `ts_utc`·`ts_exchange`·session 은 bucket 시작 1분봉에서 상속.
- **불완전 bucket 처리**: bucket 에 1분봉이 **1개 이상**이면 생성한다(존재하는 분만으로 집계).
  **세션 경계를 넘는 bucket 은 만들지 않는다**(같은 session 라벨의 1분봉만 묶음 — PRE/REGULAR/POST
  경계에서 봉이 섞이지 않게). 소비 측이 "완전 bucket 만" 원하면 `min_bars=5` 로 필터.
- **중복 제거**: 입력 1분봉은 (ts_utc) 기준 유일해야 한다(저장소 PK 로 보장). 만약 값이 다른
  ts_utc 중복이 들어오면 **(open,high,low,close,volume) 전순서(lexicographic)의 최대 튜플**을
  채택한다 — 입력 순서와 무관한 결정론적 tie-break(`resample.py`). 정상 데이터에선 발생하지 않는다.
- **결정론·순서 불변**: 입력 순서와 무관하게 동일 결과(내부에서 ts_utc 정렬). look-ahead 없음
  (각 bucket 은 자기 구간의 1분봉만 사용).

파생 5분봉은 **저장하지 않고 조회 시 생성**하는 것을 기본으로 한다(단일 진실원천 = 1분봉).
필요 시 별도 파생 테이블로 물질화할 수 있으나 **원천 1분봉에서 재생성 가능**해야 한다(§4).

**⚠ 이중 구현 주의(라이브 vs 연구)**: 저장소에는 이미 라이브 결정 경로용 5분봉 리샘플러
`src/engines/indicator_engine.resample`(pandas, `label/closed="left"`, `drop_partial=True`,
US 하드코딩)가 있고 paper_loop/candidate_stream/replay 등이 이를 사용한다. 본 계약이 정의하는
`src/history/resample.py:resample_1m_to_5m` 는 **오프라인 연구/저장소/품질용**이며 세 가지가 다르다:
(a) 부분버킷 — 라이브는 진행 중 마지막 버킷만 drop, 신규는 `min_bars` 로 필터(마지막 버킷을
자동 drop 하지 않음), (b) dedup — 라이브는 순서의존(keep=last), 신규는 순서독립(최대 튜플),
(c) 세션 — 라이브는 US 하드코딩, 신규는 market-aware(US/KR). 따라서 **"연구 5분봉 = 라이브
5분봉" 은 아직 보장되지 않는다.** 두 구현의 통일(단일 소스화)은 **Phase 1B/2 과제**이며, 그
전까지 §4.4 의 재현성 주장은 **연구 파생(resample.py)에 한정**한다.

---

## 3. 가격 계약 (adjusted / corporate action)

### 3.1 원천 저장 = 수정주가(split-adjusted), 실측 검증

- Toss `candles` 응답에는 **`adjusted` 메타 필드가 없다**(raw keys: timestamp, open/high/low/closePrice,
  volume, currency — §PROBE). 따라서 "adjusted=true 를 공급자가 준다"는 **주장 불가**였다.
- **실증 검증(§PROBE)**: 일봉·1분봉 모두 **split 기준으로 back-adjusted** 되어 있음을 확인.
  4건 분할(AAPL 4:1, NVDA 10:1, AMZN 20:1, TSLA 3:1)에서 분할 전후 종가비 ≈ 0.97~1.00
  (선언된 3~20배 점프 없음). NVDA 1분봉도 분할 전(2024-06-07) ≈ 121(수정가), ≠ 1210(원가).
- 결론: `candle_history.adjusted = 1` 저장은 **실증 근거가 있는 참**이다(일봉·1분봉 split 기준).
  기존 코드 `toss_market_data.py:41` 의 `adjusted=True` 하드코딩은 **이 실증으로 사후 정당화**된다.

### 3.2 반영 방식과 point-in-time 한계

- **split·reverse split**: 공급자가 **전 구간 소급 수정(back-adjusted)** 한다 → 시계열 연속.
  OHLC 모두 수정된다(1분봉 분할 전 가격이 수정 스케일). Volume 수정 여부는 미확정(§3.4 잔여).
- **배당(dividend) 조정**: **미검증(잔여)**. split 만 실증했다. 배당 조정 여부는 daily bulk 시
  고배당 ETF(예 XLU)의 ex-div 갭으로 **저비용 확인**한다(§5 manifest 잔여항목 R1).
- **point-in-time 재현성의 한계**: 공급자가 주는 것은 **"현재 시점 기준으로 back-adjusted 된"
  시계열**이다. 즉 과거 임의 시점(as-of date)에서 그 시점 이후 분할이 반영되지 않은 원가
  시계열은 **재현 불가**하다. 이는 back-adjusted 데이터의 본질적 한계이며, 백테스트에서
  **가격 레벨 자체를 규칙에 쓰지 말고**(예 "종가 $100 이상") **정규화 지표**(ATR 배수, %,
  상대강도)로 규칙을 정의해 완화한다(`plan §3 지표`). 절대가 임계 규칙은 계약상 금지.

### 3.3 통화

- P0-30 은 전부 **USD**. `currency=USD`, `market=US`. KR(KRW) 혼재 없음. 통화 혼재 방지는
  `loader.load_candles(market="US")` 로 강제(`src/history/loader.py:33`).

### 3.4 잔여(미검증) 항목

- **R1 배당 조정**: 미검증 → daily bulk 시 XLU ex-div 로 확인.
- **R2 volume 수정 여부**: 분할 시 volume 이 수정되는지 미확정 → R1 과 함께 확인.
- 두 항목 모두 **capability 결정을 바꾸지 않는다**(가격 연속성은 split-adjusted 로 확보).

---

## 4. 보존 구조 (provenance / immutable / 재생성)

### 4.1 계층 분리

1. **원천 스냅샷(raw, immutable) — ⚠ 현재 미구현(Phase 1B)**: 목표는 공급자 원본 payload 를
   재요청 없이 재현 가능한 immutable snapshot 으로 보존하는 것이다. **그러나 현 provider
   `TossMarketDataProvider._parse`(`toss_market_data.py:32-42`)는 응답을 즉시 canonical `Candle` 로
   변환하고 raw dict 를 버린다** → 별도 raw 계층이 **아직 없다**. 지금의 불변성 보장은 **정규화
   저장소의 append-only·멱등 + provenance 컬럼**(아래 2·§4.2)뿐이며, 진짜 raw immutable snapshot
   (또는 "canonical append-only 로 대체" 명시 결정)은 **1m bulk 전 Phase 1B 선행 항목**이다.
   (기존 세션 DB `data/market_*.sqlite` 는 import 시 `mode=ro` 로만 읽어 원본 불변, `store.py:232`.)
2. **정규화 DB(현재 유일한 지속 계층)**: `data/market_history.sqlite` — canonical `candle_history`
   (원천 1분봉 + 일봉). append/UPSERT only, 삭제/덮어쓰기 없음(§4.3).
3. **파생(5분봉)**: 1분봉에서 **재생성 가능**(§2.5). 물질화하더라도 원천에서 byte-동일 재생성.

### 4.2 provenance 컬럼 (기존 스키마, 변경 없음 — `store.py:19-81` SCHEMA)

`candle_history`: `source`(예 `toss_backfill`, `import:<db>`), `created_at`, `updated_at`,
`adjusted`, `market`, `currency`. 감사 테이블: `ingest_log`(run_id, rows_seen/inserted/updated/
duplicate/conflict, earliest/latest, note), `import_conflict`(field 단위 충돌 로그),
`backfill_cursor`(symbol, interval, oldest_ts_utc, next_before, exhausted).

기록 필수: **source, request range(earliest/latest), cursor(next_before/oldest), fetched_at
(created/updated), schema(=`WITHOUT ROWID` PK(symbol,interval,ts_utc)), config/run_id**.

### 4.3 append/UPSERT 및 source 우선순위 충돌 규칙 (기존, `store.py:201`)

- PK `(symbol, interval, ts_utc)` 충돌 = 동일 봉. 값 동일이면 **no-op(멱등)**.
- 값 불일치 시 우선순위 **(adjusted desc, source_rank desc)**; `toss_backfill`(rank 2) >
  `import:*`(rank 1). **동률이면 기존 유지(first-write-wins)**.
- 덮어쓰기 전 **`import_conflict` 에 field 단위 기록**(조용한 손실 금지). `created_at` 은 최초값
  보존, `updated_at` 만 갱신.

### 4.4 snapshot / manifest / hash (재현성)

- 동일 입력 → 동일 파생: 5분봉 재표본화는 **결정론**(§2.5, 테스트 `test_history_resample.py`).
- **snapshot ID/hash 고정**: bulk 완료 후 `(symbol, interval, earliest, latest, row_count)` 요약과
  각 심볼 정렬된 `(ts_utc, o,h,l,c,v)` 의 해시를 **manifest 로 고정**한다(Phase 1B 산출물;
  이 Phase 1A 에서는 규칙만 고정, 대량 backfill 미실행이므로 hash 미생성).
- 연구 재현: `loader.load_candles` 는 read-only·tz-aware·결정론(현재시각/난수 미사용,
  `loader.py:30-63` `load_candles`) → 동일 (db, symbol, interval, [start,end)) 는 동일 `list[Candle]`.

---

## 5. 연구 격리 (train / validation / blind OOS)

### 5.1 시간 경계 고정 (결과 확인 전)

blind OOS 경계는 **이 계약에서 고정**하며 backfill/품질 확인 단계에서 **coverage·schema·결측만**
검사하고 **수익률·전략결과·forward return 은 계산·열람하지 않는다**(§6 불변식).

| track | blind OOS 구간 (as-of 2026-07-21) | train/validation |
|---|---|---|
| Swing(1d) | 최근 **12개월**: **2025-07-21 ~ 2026-07-21** | 그 이전 전 구간(≥4년) |
| Intraday(1m) | 최근 **6개월**: **2026-01-21 ~ 2026-07-21** | 그 이전 전 구간(≥2.5년) |

- 공식 split 은 `src/history/evaluation.py:official_split`(chronological/embargo/loso 만 허용,
  random 금지)로 수행한다. embargo 로 train/test 경계 누수를 차단한다.
- blind OOS 는 **전략 사전등록·walk-forward·독립리뷰를 통과한 뒤 단 한 번** 개봉한다
  (`plan §Phase4`). 이 Phase 1A 는 blind OOS 데이터의 **존재·coverage·schema 만** 확인한다.
  경계일(2026-07-21)은 **계약 고정일 기준**이며 probe 재실행 시각과 무관하게 이 값으로 잠근다.
- **⚠ 코드 강제 아님(문서 규율)**: 위 경계는 **문서상 규율**이지 코드가 강제하지 않는다.
  `loader.load_candles` 는 임의 `[start,end)` 를 받으므로(경계 자체를 막지 않음), blind OOS 조기
  개봉 방지는 **연구 절차(사전등록·독립리뷰·단일 개봉)**로 보장한다. 코드 게이트화는 후속 고려.
- **⚠ 1분봉 마진 주의(near-zero)**: intraday train(≥2.5년) + blind OOS(6개월) = **정확히 3.0년**
  이고, 공급자 1m 보존은 §PROBE 에서 **SPY·AAPL 2종목의 3년 지점**만 실측됐다(3~5년 구간 미측정).
  bulk 후 품질 리포트(§PROBE §7 실행순서 7)에서 **P0-30 각 종목의 실제 1m earliest 를 확인**하고,
  **1m 보존이 3.0년 미만인 종목은 그 종목의 intraday 판정을 `INCONCLUSIVE_DATA` 로 격리**한다
  (train 구간을 임의 단축해 기준을 완화하지 않는다). 일봉(스윙)은 ≥10년으로 이 위험이 없다.

### 5.2 이 작업(Phase 1A)에서 하지 않는 것

수익률·전략 성과·forward return·blind OOS 성과를 **계산하거나 열람하지 않는다**. 데이터 계약과
공급자 capability 만 고정한다.

---

## 6. 불변식 (계약 위반 금지)

1. 결과(전략 성과·forward return·blind OOS)를 **본 뒤** 이 계약을 완화·변경하지 않는다.
2. 매매 경로와 연구/데이터 경로 분리. 이 계약 이행은 **주문·전략·config·가중치를 변경하지 않는다**.
3. 기존 `data/market_history.sqlite`·세션 DB 무변경(probe 는 임시/조회 전용).
4. 데이터 부족은 실패가 아니라 `INCONCLUSIVE_DATA`; **기준 완화 금지**.
5. 자격증명·토큰을 문서·로그·PR 에 남기지 않는다.
6. 미추적 `.codex/`·사용자 파일 무접촉.

---

## 7. 기존 자산 재사용 vs 신규 (요약)

| 기능 | 상태 | 위치 |
|---|---|---|
| 통합 저장소 스키마·멱등 UPSERT·충돌정책·provenance | **재사용** | `src/history/store.py` |
| Toss cursor pagination·resume·429 backoff·gap-skip | **재사용** | `src/ingest/backfill_history.py`, `src/providers/*` |
| 일봉/1분봉 backfill CLI(`--probe`, `--interval 1d`, `--gap-skip-minutes`) | **재사용** | `scripts/history_backfill.py` |
| read-only 품질 리포트(세션 커버리지·결측·gap) | **재사용** | `src/history/quality.py` |
| read-only 로더·결정론 split(chrono/embargo/loso) | **재사용** | `src/history/loader.py`, `evaluation.py` |
| UTC-경계 누락 boundary checker | **재사용** | `scripts/backfill_boundary_check.py` |
| **1분봉→5분봉 결정론 파생(연구/저장소용)** | **신규(TDD)** | `src/history/resample.py` (라이브 경로는 별도 `indicator_engine.resample` — §2.5 이중구현 주의) |
| **세션 분류·DST 특성화 테스트** | **신규(테스트만)** | `tests/test_session_classification.py` |
| **capability probe(재현 스크립트 + 실측 스냅샷+SHA-256)** | **신규** | `scripts/phase1a_probe.py`, `docs/research/phase1a_probe_result.json` |
| **1m 세션 필터**(PRE/REG/POST 만 저장), **immutable raw 스냅샷 계층** | **Phase 1B 선행(1m bulk 전 필수)** | `backfill_history`/`toss_market_data` 확장(문서화만) |
| NYSE 휴장/조기폐장 캘린더, corp-action discontinuity 스캐너, snapshot hash, 5m 연구↔라이브 통일 | **Phase 1B 로 연기** | (문서화만) |

---

## AMENDMENT A1 — 이번 달 첫 검증 전략 우선순위 교정 (2026-07-23, 이슈 #75)

> **성격: append-only 부기.** 위 v1 본문(§1~§7)은 **조용히 다시 쓰지 않는다** — Git 이력으로 원문 보존.
> 이 A1 은 **전략 우선순위 사실만** 부기하며, **데이터 계약(기간·세션·adjusted·OOS 경계·비용·품질)은 무변경**이다.

- **교정 사실**: §1(그리고 문서 상단 서문)의 "이번 달 연구 실행은 **VCP-lite 일봉 스윙 1개**에 우선 집중" 표현은 **전략 우선순위**였다. 사용자 목표가 **오버나잇 없는 당일청산 데이트레이딩**으로 확정됨에 따라, 이번 달 **첫 검증 전략을 Liquid-universe Opening Range Breakout(ORB, 당일청산)** 로 교정한다. VCP-lite 스윙은 **폐기가 아니라 후속 연구 후보로 보류**한다.
- **바뀌지 않는 것(무완화 재확인)**: 유니버스(P0-US30 30종목), 1m ≥3.0년·세션 PRE/REGULAR/POST(§2.1·§2.2), 1d ≥5년(§2.1), blind OOS 경계(1m 2026-01-21~2026-07-21·1d 최근 12개월, §5.1), 비용 민감도(0.14/0.34/0.68%p), 품질 게이트(3.0년 미달 = `INCONCLUSIVE_DATA`, 기준 완화 금지 §6-4). **전부 v1 원문 그대로.**
- **변경 시점**: 어떤 전략의 성과·수익률·forward return·blind OOS 도 **열람하기 전**. GO 를 쉽게 만드는 변경이 아니라 사용자 목표에 맞춘 우선순위 교정이다.
- **상세**: `docs/research/DECISION_DAYTRADING_ORB_PRIORITY.md` (신규 결정 문서). 실행순서 현행화는 `docs/research/PLAN_ONE_MONTH_STRATEGY_SELECTION_AI_SHADOW.md`(정본 계획의 **Git 추적본**) revision history.
- **정본 계획의 Git 보존(PR #77 독립 리뷰 차단 2 반영)**: 정본 계획은 `docs/research/PLAN_ONE_MONTH_STRATEGY_SELECTION_AI_SHADOW.md` 로 저장소에 추적 보존한다. v1 원문(서문·§1.1)의 `.omx/plans/…` 참조는 **로컬 실행용 mirror** 를 가리키는 것으로 읽는다 — 원문은 append-only 원칙에 따라 다시 쓰지 않고, 이 A1 부기로 해석을 고정한다.

### A1 부기 — §7 "Phase 1B 선행/연기" 항목의 실제 구현 (이슈 #75)

§4.1(immutable raw)·§2.2(세션 필터)·§2.4(NYSE 캘린더)·§4.4(snapshot hash)·§2.5(5m 파생) 이 "문서화만/연기"로 남겨둔 항목을 이슈 #75 에서 **구현**한다(실제 30종목×3년 bulk 는 여전히 미실행). 대응:

| §  참조 | Phase 1B 구현물 (신규) |
|---|---|
| §2.2 1m 세션 필터(저장 전) | `src/history/session_filter.py` + `backfill_history`/CLI `--sessions` |
| §4.1 immutable raw 스냅샷 | `src/history/raw_snapshot.py` + provider raw_sink(parse 전) + ADR `docs/adr/ADR-0001-immutable-raw-snapshot.md` |
| §2.4 NYSE 휴장/조기폐장 캘린더 | `src/history/exchange_calendar.py`(무의존, 3년 구간 추적 artifact) |
| §4.4 snapshot hash / coverage manifest | `src/history/intraday_manifest.py` + `scripts/history_intraday_manifest.py` |
| §5.1 blind OOS 코드 게이트("후속 고려") | `src/history/oos_guard.py`(연구 로더 fail-closed, coverage 허용) + 공식 스크립트 배선(`history_eval_slices` → guarded loader) + 직접 `load_candles` import 금지 경계 테스트(`tests/test_oos_guard_wiring.py`) |
| §2.5 1m→5m + **1m→15m**·opening-range | `src/history/resample.py` 확장(15m·09:30–09:44 ET 완료봉만) |
| §2.5 연구↔라이브 5m 통일 | **별도 Phase 2 blocking-gate 이슈 #76**(ORB 공식 연구 전 필수, 이 PR 미포함) |

---

## AMENDMENT A2 — §2.5 이중 구현 해소 (2026-07-26, 이슈 #76)

> **성격: append-only 부기.** §2.5 본문과 그 "⚠ 이중 구현 주의" 단락은 **다시 쓰지 않는다**
> (Git 이력으로 원문 보존). A2 는 그 경고가 **해소된 사실**과 새로 확정된 세부 계약만 부기한다.
> **데이터 계약(기간·세션·adjusted·OOS 경계·비용·품질)은 무변경.**

### A2-1. 해소된 사실

§2.5 는 연구용 `src/history/resample.py` 와 라이브용 `src/engines/indicator_engine.resample`
의 **이중 구현**을 경고하며 "연구 5분봉 = 라이브 5분봉 은 아직 보장되지 않는다", "§4.4 재현성
주장은 연구 파생에 한정한다"고 적었다. 이슈 #76 에서 **단일 소스화**했다:

- 유일한 집계 권위 = `src/history/resample.py`(pure·market-aware·순서독립·세션 버킷키).
- `indicator_engine.resample` 과 `frame_to_candles` 는 **DataFrame ↔ Candle thin adapter** 로
  축소됐고, `paper_loop`·`candidate_stream`·`replay` 가 **같은 core·같은 변환**을 호출한다.
- pandas `resample` 은 권위 경로에서 제거됐다.

→ §2.5 의 (a) 부분버킷 / (b) dedup / (c) 세션 market-aware **세 차이가 모두 사라졌다.**
따라서 "연구 5분봉 = 라이브 5분봉" 은 이제 보장되며, §4.4 재현성 주장의 "연구 파생 한정"
단서도 **파생 리샘플에 한해** 해제된다.

### A2-2. 새로 확정한 세부 계약 (기존 조항 완화 아님)

1. **두 축 분리** — `closed_only`(시간축: 버킷 구간이 끝났는가) 와
   `min_bars`/`require_complete`(커버리지축: 1분봉이 몇 개인가)는 **독립**이며 호출자마다
   다르게 쓰지 않는다.
2. **완료 경계(frontier)** — `closed_only=True` 는 `버킷 종료 <= frontier` 만 방출한다.
   frontier = `evaluation_time` 또는 **입력 마지막 1분봉의 `ts_utc`**(형성 중일 수 있어 +1분
   하지 않음). 기존 라이브 `drop_partial=True` 와 **관측 동치** — 라이브 결정 시점 불변.
   버킷 종료는 **UTC 산술**로 계산한다(DST 전환 구간 길이 왜곡 방지).
3. **dedup** — 프레임 경로의 `keep="last"`(순서 의존)를 제거하고 §2.5 의 최대 튜플 규칙으로
   통일했다. 중복은 frontier 를 앞당기지 못한다.
4. **세션/market** — 버킷 키에 session 포함(구조적), 버킷 경계는 거래소 로컬 분 floor,
   session/`ts_exchange`/`market` 은 원본에서 상속. **ET 하드코딩 제거**(KR 정확).
   VWAP 일일 리셋 키 `et_date` 는 **거래소 로컬 날짜**로 정정(컬럼명은 하위호환 유지).
5. **DST 가을 fold** — §2.5 가 "라이브 통일 시 처리"로 미뤘던 항목: 로컬 라벨이 같아도 UTC
   offset 이 달라 **버킷이 합쳐지지 않는다**(테스트로 고정). 해당 구간이 OVERNIGHT 이라
   4전략이 쓰지 않는다는 §2.2 판단은 그대로다.
6. **fail-closed** — 파생봉 재리샘플·지원 밖 rule(5min/15min 외)·무시되는 인자
   (`evaluation_time` without `closed_only`)·`min_bars<1` 은 `ValueError`.
7. **interval 라벨** — 파생봉은 `"5m"`/`"15m"`. `candidate_stream` 이 rule 문자열 `"5min"` 을
   라벨로 쓰던 불일치를 제거했다.

### A2-3. 증거와 범위

- 계약 테스트 `tests/test_resampler_parity.py` 33건 + CLI `tests/test_resampler_parity_cli.py` 6건.
- 실데이터 parity: 고정 clean snapshot(file SHA `3c2c4751…`)에서 6종목 × 6거래일 = **36셀,
  불일치 0**(1m 29,322봉 → 5m 6,087 / 15m 2,041). DST 전환일·조기폐장일 포함,
  **blind OOS 거래일 0**. receipt: `docs/research/RESAMPLER_PARITY_RECEIPT.json`.
- 정본 계약 문서: `docs/research/RESAMPLER_PARITY_CONTRACT.md`.
- **성과·forward return·blind OOS 미열람.** 로드는 `oos_guard.research_load(purpose="coverage")`
  경유. 주문/broker/risk/Paper/전략/runtime config·canonical DB **무변경**.
- 남은 한계: stale 종목의 마지막 버킷 지연(**#44** 범위, 이번에 의도적으로 동작 보존),
  조기폐장 커버리지 390분 가정(§2.4 알려진 한계 유지), KR 실데이터 parity 미실측(US-30 스냅샷).
