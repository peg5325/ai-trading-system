# Toss 공급자 Capability Probe 리포트 v1 + 판정

> 이슈 #68 · Phase 1A · 실행일 2026-07-22 (as-of 데이터 2026-07-21)
> 계약: `docs/research/DATA_CONTRACT_4STRATEGY_V1.md`
> raw 산출물(gitignore): `reports/phase1a_probe/toss_capability_probe.json`
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
  최근 분할주(AAPL/NVDA/AMZN/TSLA)로 adjusted 실증. spacing 0.4s, 총 **131 API 호출**.
- 측정: 일봉/1분봉 실 보존 깊이, cursor pagination 도달 범위, 세션 라벨, split 연속성,
  호출당 latency, 429/backoff.
- **DB 무변경 증거**: probe 는 Toss API 만 호출하고 로컬 DB 에 쓰지 않는다. inventory 는 `mode=ro`
  로만 열었다. 실행 전후 `data/market_history.sqlite` 의 **크기(506,720,256 bytes)·mtime 동일**,
  `-wal` 0 bytes 확인(계약 §6-3).

## 2. 일봉(1d) 결과 — **5년 요건 충족(≥10년 확인)**

| 항목 | 결과 |
|---|---|
| 제공 여부 | **제공됨** (`interval=1d`, 200봉/페이지, `nextBefore` cursor) |
| 도달 깊이(표본 9종목) | **2016-03-17 ~ 2026-07-21** (13페이지=2600 거래일에서 page cap; wall 미도달) |
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
| ~3년(2023-07-22) | 200 | 200 | **존재**(PRE/OVERNIGHT 포함) |
| ~5년(2021-07-22) | **0** | **0** | **없음** |

- **1m 보존 ≈ 3~5년 사이; 최소 3년 요건 충족**(공급자 레벨).
- 세션 라벨(라이브 6페이지 SPY): `PRE 310 · REGULAR 207 · POST 238 · OVERNIGHT 445` →
  **PRE/REGULAR/POST/OVERNIGHT 모두 제공**(Toss 미국주식 24h). 계약 필수(PRE/REG/POST) 충족.
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
- **DST(일봉 timestamp)**: 여름 `13:00+09:00`, 겨울 `14:00+09:00` = **둘 다 00:00 ET** →
  `astimezone(ET).date()` 는 연중 올바른 거래일(날짜 밀림 없음). 계약 §2.3 근거.

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
   무겁지만(≈15~24h·9~14GB) **resume 가능·429 0건(@0.4s)** 으로 실행 가능.
4. **안전한 bulk manifest/config 구성 가능** — 기존 안전 인프라 + 이 계약/manifest.

### scoped 의 의미 (과대주장 방지)

- **일봉/스윙 트랙 = 즉시 GO**: 공급자 ≥10년 split-adjusted 일봉 검증 완료 → **VCP-lite 스윙
  연구를 지금 시작 가능**. 일봉 bulk 는 저비용·저위험(수분, <50MB).
- **1분봉/intraday 트랙 = GO(무거움)**: 공급자 capability 는 충족. bulk 는 **다중 세션 resume**
  (≈15~24h, 9~14GB)로 실행. bulk 착수 전 **저비용 잔여 확인 2건**을 manifest 에 포함:
  - **R1 배당 조정 검증**(고배당 XLU ex-div 갭) — daily bulk 중 확인.
  - **R3 gap-skip 장기휴장 예산**(`max_consecutive_empty`)을 연말연시(≥5일 휴장) 커버하도록
    상향(1m bulk 전) — 미상향 시 장기휴장 이전 구간 미도달(`backfill_history.py:169` 한계).
- 이 잔여는 **capability 결함이 아니라 실행 스케줄 항목**이므로 판정을 낮추지 않는다.

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
| 1m 30종목 ×3년 (PRE+REG+POST) | ~17.1M | **~9.4 GB** | ~110k~130k | **~15~18h** |
| 1m 30종목 ×3년 (+OVERNIGHT) | ~25.5M | **~14 GB** | 더 많음 | ~24h |

- **디스크 여유 권장: ≥20 GB**(1m ~14GB + 일봉 + WAL + 작업 여유; 현재 DB 507MB).
- 1m 호출 수 = 데이터 페이지(bars/200) + gap-skip 경계/빈 호출(~30~50% 추가).

### 실행 순서(권장)

1. **스키마 확인**: `scripts/history_init.py --db data/market_history.sqlite` (멱등, 무위험).
2. **일봉 bulk(즉시)**: `history_backfill.py --interval 1d --symbols <P0-30> --max-pages 13
   --spacing 0.5` → 30종목 ≥10년. 완료 후 **R1 배당 검증**(XLU) + 심볼별 earliest 를 manifest 기록.
3. **품질 리포트(일봉)**: `history_quality_report.py --interval 1d` → 스윙 5년 커버리지 확인.
   → **VCP-lite 스윙 연구 착수 가능**.
4. **(사용자 결정) 1m 3년 bulk**: R3(`--max-consecutive-empty` 상향) 반영 후
   `history_backfill.py --interval 1m --symbols <P0-30> --gap-skip-minutes 200 --spacing 0.5
   --max-pages <대> --max-consecutive-empty <상향>`. **종목별 batch + cursor resume**(중단/재개
   안전). 백그라운드·야간 실행 권장(장외 시간대, rate-limit 여유).
5. **boundary check**: `backfill_boundary_check.py --symbols <P0-30> --interval 1m` →
   UTC-경계 누락 0 확인(exit 0).
6. **품질 리포트(1m)**: `history_quality_report.py --interval 1m` → 3년·세션 커버리지 + missing.
7. **snapshot hash 고정**(Phase 1B): 심볼별 정렬 (ts_utc,o,h,l,c,v) 해시 manifest.

### resume/idempotent 보장(기존)

- `backfill_cursor` 로 중단 지점 재개. PK UPSERT 로 재실행 안전(중복 무해).
- 종목 격리(한 종목 실패가 나머지 중단 안 함), 429 지수 backoff.

## 8. bulk 착수 전 남은 사용자 결정

1. **1m OVERNIGHT 포함 여부**: PRE/REG/POST(필수, ~9.4GB) vs +OVERNIGHT(~14GB). 4전략은
   PRE/REG/POST 만 사용 → **기본 제외 권장**.
2. **1m 3년 bulk 실행 승인 + 시점**: ~15~24h·~14GB·수만 API 호출. 장외/야간 배치 권장.
3. **일봉 깊이**: 5년(요건) vs 10년(여유, 거의 무비용) — **10년 권장**.
4. **R3 `max_consecutive_empty` 목표값**: 연말연시 최장 휴장(예 ~5거래일) 커버 기준 설정.

*이 4건은 데이터량/시간/비용 트레이드오프이며 계약/판정을 바꾸지 않는다.*
