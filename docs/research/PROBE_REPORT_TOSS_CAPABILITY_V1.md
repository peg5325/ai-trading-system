# Toss 공급자 Capability Probe 리포트 v1 + 판정

> 이슈 #68 · Phase 1A · 실행일 2026-07-22 (재현본 데이터 as-of 2026-07-22)
> 계약: `docs/research/DATA_CONTRACT_4STRATEGY_V1.md`
> **재현(독립 검증용)**: 스크립트 `scripts/phase1a_probe.py`(요청 파라미터 고정·자격증명 미출력),
> 실측 스냅샷 **추적** `docs/research/phase1a_probe_result.json`
> (SHA-256 `99ef96c124c8ff3505bae2980b4df306cf2d651537e87587be3a0a54c988f817`).
> **재현 대상 = capability 불변식만**(일봉 ≥10년·cursor 미소진 / split-adjusted / 1m ≥3년·<5년 /
> PRE·REGULAR·POST·OVERNIGHT 네 라벨 **모두 존재**). 아래 값은 **라이브라 재실행마다 달라진다**:
> `generated_utc`(생성 시각), daily `latest`/`earliest`(최근 시장데이터 전진), **라이브 세션
> 카운트**(`sessions_live` — 실행 시점 6페이지 창), `latencies_ms`/`latency_summary`(호출 지연).
> 따라서 본 리포트의 개별 카운트·시각·지연 수치는 이 스냅샷(as-of 2026-07-22)의 값이며,
> 재현 검증은 **위 불변식 일치**로 한다(추적 JSON 이 그 스냅샷의 정본).
> (임시 ad-hoc 산출물은 gitignore `reports/phase1a_probe/` 에 둘 수 있음.)
> **판정: `DATA_GO` (scoped)** — §6.
>
> 안전: probe 는 **read-only 시장데이터 조회만**. 주문 0. 기존 `data/market_history.sqlite`·세션 DB
> **무변경**(별도 임시 조회, DB 쓰기 없음). 자격증명·토큰은 문서·로그에 없음.

---

## 1. 방법

- 공급자: 기존 Toss Open API (`openapi.tossinvest.com`) — 현재 유일 연결 경로.
- 클라이언트: 기존 `src/providers/toss_client.py` / `toss_market_data.py`(변경 없음).
  자격증명은 main checkout `.env` 에서 로드(출력·기록 안 함).
- 표본: benchmark(SPY, QQQ), 대형주(AAPL, MSFT, NVDA, AMZN, TSLA), 레버리지(SOXL, TQQQ).
  최근 분할주(AAPL/NVDA/AMZN/TSLA)로 adjusted 실증. spacing 0.4s, 총 **132 API 호출**(429 0건).
- 측정: 일봉/1분봉 실 보존 깊이, cursor pagination 도달 범위, 세션 라벨, split 연속성,
  호출당 latency, 429/backoff.
- **DB 무변경 증거**: probe 는 Toss API 만 호출하고 로컬 DB 에 쓰지 않는다. inventory 는 `mode=ro`
  로만 열었다. 실행 전후 `data/market_history.sqlite` 의 **크기(506,720,256 bytes)·mtime 동일**,
  `-wal` 0 bytes 확인(계약 §6-3).

## 2. 일봉(1d) 결과 — **5년 요건 충족(≥10년 확인)**

| 항목 | 결과 |
|---|---|
| 제공 여부 | **제공됨** (`interval=1d`, 200봉/페이지, `nextBefore` cursor) |
| 도달 깊이(표본 9종목) | **2016-03-18 ~ 2026-07-22** (13페이지=2600 거래일에서 page cap; wall 미도달) |
| 실제 wall | **미도달** — cursor 가 여전히 열려 있어 실보존은 **10년보다 깊다** |
| 5년 요건 | **충족** (필요 2021-07, 확보 2016-03 이하) |
| 페이지당 봉 | 200 (≈ 9.5개월/페이지) |

일봉은 **UTC 일자 경계 문제 없음**(1봉/일, 13페이지 연속 확인) → gap-skip 불필요.

## 3. 1분봉(1m) 결과 — **3년 요건 충족(<5년), 세션 구분 제공**

targeted `before`-jump 으로 "특정 날짜 이전 1m 존재?" 측정:

| target(과거) | SPY rows | AAPL rows | 판정 |
|---|---|---|---|
| ~1년(2025-07-22) | 91 | 157 | 존재 |
| ~2년(2024-07-22) | 155 | 178 | 존재 |
| ~3년(2023-07-22) | 200 | 200 | **존재**(표본 창 ET 14:40–18:00 = REGULAR/POST) |
| ~5년(2021-07-22) | **0** | **0** | **없음** |

- **1m 보존 ≈ 3~5년 사이; 최소 3년 요건 충족**(공급자 레벨).
- 세션 라벨(라이브 6페이지 SPY, 추적 JSON `sessions_live`): `PRE 310 · REGULAR 390 · POST 237 ·
  OVERNIGHT 263` → **PRE/REGULAR/POST/OVERNIGHT 모두 제공**(Toss 미국주식 24h). 계약 필수
  (PRE/REG/POST) 충족. (개별 카운트는 실행 시점의 최근 6페이지 창에 따라 달라진다 — 불변 주장은
  "네 라벨 모두 존재"뿐이며 이는 재실행마다 재현된다; §1 재현성 주석.)
- **주의(coverage vs capability)**: 공급자가 3년 1m 을 **제공**한다는 것과 우리가 **수집 완료**했다는
  것은 다르다. 현재 DB 는 최대 ~8개월(QQQ 205일)만 수집됨(§5). 3년 bulk 는 §7 계획으로 실행.
- **주의(마진·표본)**: 1m 3년 지점은 **SPY·AAPL 2종목**만 실측했고 3~5년 구간은 미측정이다.
  계약의 intraday 요건(2.5년 train + 6개월 OOS)은 **정확히 3.0년**으로 마진이 거의 없다. bulk 후
  품질 리포트에서 **P0-30 각 종목의 실제 1m earliest** 를 확인하고, 3.0년 미만 종목은 그 종목의
  intraday 판정을 `INCONCLUSIVE_DATA` 로 격리한다(계약 §5.1). 이는 스윙(일봉 ≥10년)과 무관하다.

## 4. Corporate action / adjusted — **split-adjusted 실증(일봉+1분봉)**

| symbol | split | 전 종가 | 후 종가 | 전/후 비 | 선언비 | 판정 |
|---|---|---|---|---|---|---|
| AAPL | 4:1 2020-08-31 | 124.81 | 129.04 | 0.967 | 4 | **adjusted** |
| NVDA | 10:1 2024-06-10 | 120.89 | 121.79 | 0.993 | 10 | **adjusted** |
| AMZN | 20:1 2022-06-06 | 122.35 | 124.79 | 0.980 | 20 | **adjusted** |
| TSLA | 3:1 2022-08-25 | 297.10 | 296.07 | 1.003 | 3 | **adjusted** |

- 분할 전후 종가비가 **선언 비율의 점프 없이 ≈1.0** → **일봉은 전 구간 split back-adjusted**.
- **1분봉도 adjusted**: NVDA 1m 2024-06-07(분할 전) 종가 ≈ **121**(수정가), ≠ 1210(원가).
- 공급자 응답에 **`adjusted` 메타 필드는 없음** → 의미는 **실증으로 확정**(계약 §3.1).
- **DST(일봉 timestamp)**: 여름 `13:00+09:00`=**04:00Z**=00:00 EDT, 겨울 `14:00+09:00`=**05:00Z**
  =00:00 EST. UTC 시각은 1시간 다르지만(04:00Z vs 05:00Z) **거래소 로컬은 둘 다 00:00 ET** →
  `astimezone(ET).date()` 는 연중 올바른 거래일(날짜 밀림 없음). 계약 §2.3 근거,
  `test_session_classification.py` 로 UTC hour 4/5 회귀 고정.

## 5. 현재 수집 상태(inventory, read-only) — bulk 필요량의 기준선

`data/market_history.sqlite`(506MB, 925,146행, 전부 `1m`, **일봉 0행**):

- US 12종목 1m: QQQ 205일(2025-11~), SPY 125일, XLU 115일, VST 72일, 나머지(AMD/AVGO/GOOGL/
  MSFT/NVDA/SOXL/SOXS/SOXX) ~47~52일(~2개월). KR 3종목은 레거시(4일).
- 12종목 backfill_cursor 전부 `exhausted=0` → **max_pages 로 중단**(공급자 wall 아님) → 재개 시 더 과거로 진행 가능.
- **P0-30 대비 미수집**: 일봉 30/30 미수집, 1m 은 18/30 종목 미수집 + 수집분도 대부분 <3년.

## 6. 판정: `DATA_GO` (scoped)

세 판정(DATA_GO/PARTIAL_GO/NO_GO) 중 **DATA_GO** 가 문언에 부합한다. 근거:

1. **5년 수정 일봉 + 3년 세션 1분봉을 모두 확보 가능** — 공급자가 둘 다 실제 제공(≥10년 일봉,
   ≥3년 1m). PARTIAL_GO 의 전제("intraday 는 공급자 추가 선정 필요")는 **해당 없음** — Toss 로 충분.
2. **기업행사·시간대·provenance 검증** — split-adjusted 실증(일봉·1m 4건), DST 일봉 날짜 정합
   실증, provenance/충돌/cursor 인프라 존재(계약 §4).
3. **요청량·저장·비용·rate limit 실행 가능** — §7 추정. 일봉은 자명(수분/수십MB). 1m 3년은
   무겁지만(≈20~24h·~14GB 전 세션) **resume 가능·429 0건(@0.4s)** 으로 실행 가능.
4. **안전한 bulk manifest/config 구성 가능** — 기존 안전 인프라 + 이 계약/manifest.

### scoped 의 정확한 의미 (★ 오독 방지 — DATA_GO 는 "공급자 capability GO"이지 "1m bulk 즉시 GO"가 아니다)

- **일봉/스윙 트랙 = 즉시 실행 GO**: 공급자 ≥10년 split-adjusted 일봉 검증 완료 + 저비용·저위험
  (수분, <50MB) → **일봉 bulk 를 지금 실행하고 VCP-lite 스윙 연구를 시작할 수 있다.**
- **1분봉/intraday 트랙 = 공급자 capability GO, 그러나 "bulk 즉시 실행 GO"는 아직 아님.** 공급자
  깊이(≥3년)·세션·adjusted 는 충족하나, **대량 수집 전 아래를 먼저 해결**해야 한다(§7 실행순서 4):
  - **세션 필터 부재** — 현 CLI 로는 PRE/REG/POST 만 저장 불가(전 세션 ~14GB만 가능).
  - **immutable raw 미보존** — provider 가 raw 를 버림(계약 §4.1 대비 미충족).
  - **종목별 3.0년 커버리지 미검증** — 1m 3년은 SPY/AAPL 2종목만 실측(마진 near-zero); bulk 후
    종목별 확인, 3.0년 미만은 `INCONCLUSIVE_DATA`.
  - **R1 배당 조정**(XLU ex-div) / **R3 gap-skip 장기휴장 예산** 확인.
- 정리: **일봉 bulk = 지금 실행 / 1m bulk = 위 선행 해결 후 실행.** 세 판정 중 DATA_GO 가 문언에
  부합하는 이유는 **공급자가 두 요건을 실제 제공**하기 때문이며(PARTIAL_GO 의 "intraday 공급자
  추가 선정 필요" 전제 해당 없음), 위 항목은 **우리 파이프라인/스케줄 작업**이지 공급자 capability
  결함이 아니다 → 판정을 낮추지 않는다.

### 판정을 바꿀 수 있었던 반례(해당 없음)

- 일봉 미제공 → 스윙 데이터원 붕괴(NO/PARTIAL): **반증됨**(≥10년 제공).
- adjusted 아님(원가) → 가격 연속성 붕괴: **반증됨**(split back-adjusted 실증).
- 1m <3년 → intraday 미달(PARTIAL): **반증됨**(≥3년 제공).

## 7. bulk 수집 실행 순서 + batch/resume 계획 (GO)

기존 스크립트 재사용(신규 엔진 불필요). **이 Phase 1A 에서는 실행하지 않음** — 다음 bulk 작업용.

### 추정 (실측 기반: 755 1m-bars/거래일, 548 bytes/row)

| 대상 | rows | 저장 | API 호출 | 시간(@0.5s) |
|---|---|---|---|---|
| 일봉 30종목 ×5년 | ~37,800 | ~21 MB | ~210 | ~2분 |
| 일봉 30종목 ×10년 | ~78,000 | ~43 MB | ~390 | ~3.5분 |
| 1m 30종목 ×3년 **(현 CLI 실현치 = 전 세션)** | ~25.5M | **~14 GB** | ~110k~130k+ | **~20~24h** |
| 1m 30종목 ×3년 (PRE+REG+POST 만, 세션 필터 필요) | ~17.1M | ~9.4 GB | (동일 호출) | (동일) |

- **⚠ 세션 필터 부재(현 CLI 한계)**: `backfill_symbol` 은 `fetch_page` 가 돌려준 **모든 세션 봉을
  그대로 upsert** 한다(`backfill_history.py` 에 session 필터 없음). 따라서 `--gap-skip` 로 3년을
  긁으면 **OVERNIGHT 포함 전 세션(~14GB)** 이 실현되며, 위 **9.4GB(PRE+REG+POST 만) 경로는 현재
  명령으로 실행 불가**다. 9.4GB 를 실현하려면 **세션 필터(backfill 저장부 또는 사후 prune)를 먼저
  추가**해야 한다(Phase 1B 선행). 그때까지 API 호출·시간은 전 세션 기준으로 잡는다.
- **⚠ immutable raw 미보존(현 provider 한계)**: `TossMarketDataProvider._parse` 는 응답을 즉시
  canonical `Candle` 로 변환하고 **raw payload 를 버린다**. 계약 §4.1 의 "원천 immutable raw
  snapshot" 은 **아직 구현이 아니다** — 현재 불변성은 canonical 저장소의 append-only·멱등 + provenance
  컬럼(source/created_at/run_id)뿐이다. 진짜 raw 스냅샷 계층은 **Phase 1B 선행 항목**(계약 §4.1 갱신).
- **디스크 여유 권장: ≥20 GB**(전 세션 1m ~14GB + 일봉 + WAL + 작업 여유; 현재 DB 507MB).
- 1m 호출 수 = 데이터 페이지(bars/200) + gap-skip 경계/빈 호출(~30~50% 추가).

### 실행 순서(권장)

1. **스키마 확인**: `scripts/history_init.py --db data/market_history.sqlite` (멱등, 무위험).
2. **일봉 bulk(즉시)**: `history_backfill.py --interval 1d --symbols <P0-30> --max-pages 13
   --spacing 0.5` → 30종목 ≥10년. 완료 후 **R1 배당 검증**(XLU) + 심볼별 earliest 를 manifest 기록.
3. **품질 리포트(일봉)**: `history_quality_report.py --interval 1d` → 스윙 5년 커버리지 확인.
   → **VCP-lite 스윙 연구 착수 가능**.
4. **(1m bulk 선행 — Phase 1B)**: 아래 3건은 **1분봉 대량 수집 전에** 해결한다(§7 경고).
   - **세션 필터** 추가(9.4GB PRE/REG/POST 경로를 원하면) 또는 전 세션(~14GB) 수용 명시.
   - **immutable raw 스냅샷** 계층(또는 "canonical append-only 로 대체" 명문 결정) — 계약 §4.1.
   - **R3** `--max-consecutive-empty` 를 연말연시(≥5거래일 휴장) 커버로 상향.
5. **(사용자 결정) 1m 3년 bulk**:
   `history_backfill.py --interval 1m --symbols <P0-30> --gap-skip-minutes 200 --spacing 0.5
   --max-pages <대> --max-consecutive-empty <상향>`. **종목별 batch + cursor resume**(중단/재개
   안전). 백그라운드·야간 실행 권장(장외 시간대, rate-limit 여유).
6. **boundary check**: `backfill_boundary_check.py --symbols <P0-30> --interval 1m` →
   UTC-경계 누락 0 확인(exit 0).
7. **품질 리포트(1m) + 종목별 3.0년 검증**: `history_quality_report.py --interval 1m` →
   세션 커버리지 + missing. **P0-30 각 종목의 1m earliest 를 확인**하고 **3.0년 미만 종목은
   그 종목 intraday 판정을 `INCONCLUSIVE_DATA` 로 격리**(계약 §5.1, 표본 SPY/AAPL 만 3년 실측이므로).
8. **snapshot hash 고정**(Phase 1B): 심볼별 정렬 (ts_utc,o,h,l,c,v) 해시 manifest.

### resume/idempotent 보장(기존)

- `backfill_cursor` 로 중단 지점 재개. PK UPSERT 로 재실행 안전(중복 무해).
- 종목 격리(한 종목 실패가 나머지 중단 안 함), 429 지수 backoff.

## 8. bulk 착수 전 남은 사용자 결정

1. **1m OVERNIGHT 처리**: 4전략은 PRE/REG/POST 만 사용하나 **현 CLI 는 세션 필터가 없어 전 세션
   (~14GB)만 저장 가능**하다(§7). 선택: (a) 전 세션 수용(~14GB, 지금 실행 가능) 또는 (b) **세션
   필터를 먼저 추가**해 9.4GB 실현(Phase 1B 선행). — 권장 (b), 급하면 (a).
2. **1m 3년 bulk 실행 승인 + 시점**: ~20~24h(전 세션)·~14GB·수만 API 호출. 장외/야간 배치 권장.
3. **일봉 깊이**: 5년(요건) vs 10년(여유, 거의 무비용) — **10년 권장**.
4. **R3 `max_consecutive_empty` 목표값**: 연말연시 최장 휴장(예 ~5거래일) 커버 기준 설정.

*이 4건은 데이터량/시간/비용 트레이드오프이며 계약/판정을 바꾸지 않는다.*
