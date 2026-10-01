# P0-US30 Intraday Gross-Edge Screen — 사전등록 v2.0

상태: **DRAFT r4 — 사용자 승인 후 FROZEN BEFORE RESULTS로 전환**
작성: 2026-10-01 (r1 초안 → 독립 리뷰 REQUEST_CHANGES → r2 → 재리뷰 APPROVE_WITH_CHANGES → r3 반영 → r4 사용자 결정: 최근 1년 게이트 G8 추가, 공식 0.34%p 유지)
근거 문서:
- [후보 탐색](EXPLORATION_DAYTRADING_CANDIDATES_20261001.md)
- [실패 분석](ANALYSIS_DAYTRADING_FAILURES_AND_TOSS_DATA_QUALITY_20261001.md)
- [v1 screen](PREREG_P0_US30_GROSS_EDGE_SCREEN_V1.md)

목적: blind OOS를 열지 않고, 고정 development 구간에서 다음 정식 전략 사전등록으로 넘길 만한
일중 롱 규칙이 있는지만 선별한다. v1 결과(`NO_GROSS_EDGE`)를 재판정하거나 개정하지 않는다.
v2는 새 후보군에 대한 별도 screen이다.

## 1. 연구 질문과 금지 사항

> 고정된 4개 계열 8개 일중 롱 규칙 중, 비용 적용 전 거래당 gross return이
> Charter 공식 비용(왕복 0.34%p)을 여유 있게 넘고, 시간·종목·표본에 걸쳐 안정적인 후보가 있는가?

다음을 금지한다.

- **접근 금지 구간**
  - blind OOS(2026-01-21~2026-07-21)의 가격·성과.
  - 신규 holdout H2(2026-07-22~2026-09-30)의 가격·성과.
  - 2026-01-20을 넘는 어떤 날짜의 가격도 쓰지 않는다. overnight 보유나 다음 날 청산 진단도 포함한다.
- **결과 기반 조작**
  - arm별 임계값 탐색.
  - 결과를 본 뒤 규칙·판정 기준 변경.
  - 최선 arm 하나만 보고하기.
- **운영 변경:** live/order/broker/risk/config, 운영 `market_history.sqlite`, 진행 중인 freeze-paper 계약.
- **통계 단위:** 동일 날짜의 여러 거래를 독립 일수로 세기.

## 2. 데이터 identity와 구간

### 2.1 데이터 identity

v1과 동일한 frozen snapshot을 쓴다.

- snapshot: `p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite`
- collection run: `p0-us30-1m-20260721-r82`
- manifest: `docs/research/P0_US30_ORB_CLEAN_1M_MANIFEST.json`
- snapshot file SHA-256: `3c2c475125594bd7fe8c59d994bdc6d4cb9f1f1e0b69005b9fbcbf5eb852ca33`
- snapshot content SHA-256: `d0d145518938686937895fb3c886a4c2fd996588f129e8234a5913b6be2597e4`
- manifest data SHA-256: `e270a1b56d2466a856ef92e32abb58730953b7b77ce1038d7b596f66f477bd0c`
- raw content SHA-256: `22c02118c7c8bffc225fc021e2c4eb9ad4bf7b1d8c66df8e927909c46e609ed4`

**identity 검증 방법:** v1 erratum과 `src/research/gross_edge/run.py`와 같다.
- snapshot **파일** SHA-256을 실측한다.
- 나머지 해시는 tracked receipt와 대조한다.
- 의미 해시는 다시 계산하지 않는다. 다시 계산하면 blind OOS OHLCV를 디코드하게 되기 때문이다.
- 불일치하거나 검증할 수 없으면 `INCONCLUSIVE_DATA`이고 성과 계산을 시작하지 않는다.

**로딩:**
- 가격 로드는 `research_load(... purpose="performance", track="intraday")`를 경유한다.
- **일봉 지표도 같은 snapshot의 REGULAR 1분봉에서 만든다.** 운영 DB와 swing 트랙 DB는 읽지 않는다.
- 같은 날 진입·청산 규칙에 intraday 트랙 1분봉 유래 일봉을 쓰는 것은 선례를 따른다
  (`gap_down_fade/loading.py`, `spy_intraday_momentum/loading.py`).

### 2.2 구간

- **거래 평가:** 2023-05-18~2026-01-20(670 NYSE 거래일).
  - **2024-07-01~2024-08-30(44일)은 거래 평가에서 통째로 제외한다.**
  - 이 제외는 결과 전에 고정한 보수적 블록 제외다. 2024-07-25~08-02 Toss 수집 공백이 계기였다.
  - 2024-08-05 변동성 충격일을 포함한 정상 데이터도 함께 빠진다.
  - 제외 구간의 유효 일봉은 이후 지표 계산에 쓴다.
  - 조기폐장일은 거래하지 않는다. 그래서 평가 거래일은 619일이다.
- **일봉 지표 warm-up:** 2023-01-03 이후 REGULAR 봉을 쓴다. 저장소 NYSE 달력 artifact가 2023-01-01부터다.
  종목별 snapshot 시작이 늦은 SPY·QQQ·NFLX(2023-04), AVGO(2023-02)는 그만큼 warm-up이 늦다.
- **blind OOS:** 2026-01-21~2026-07-21. 계속 잠그며, `OOS_UNLOCK_VERIFICATION_CONTRACT.md`를 따른다.
- **신규 holdout H2:** 2026-07-22~2026-09-30.
  - 이 snapshot에는 없다.
  - 운영 DB에는 2026-10-01 복구로 핵심 20종 REGULAR가 결측 없이 들어 있다(봉 수만 확인, 가격 미열람).
  - 이 screen은 읽지 않는다. 통과 arm의 정식 사전등록이 H2 사용 여부를 결과 전에 정한다.

### 2.3 종목

- **핵심 20종:** SPY QQQ IWM TQQQ SQQQ SOXL SOXS AAPL MSFT NVDA AMD AVGO AMZN META GOOGL TSLA MU NFLX CRM PLTR.
  섹터 ETF 7종, VST, DIA, SMH는 D9 품질 사유로 제외한다.
- **단일종목 레버리지·인버스 ETF**(예: TSLL, NVDL)는 대상이 아니며 추가하지 않는다(2026-10-01 사용자 결정).
  레버리지·인버스 상품은 지수형 4종(TQQQ·SQQQ·SOXL·SOXS)뿐이다.
  A2는 단일종목 레버리지 ETF의 리밸런싱을 근거로 삼지만, 매매 대상은 그 기초 주식이다.
- **인버스 ETF:** SQQQ·SOXS는 역분할 소급조정 가격이다. %수익과 ATR/가격 비율은 유효하지만 주식 수 계산에는 쓰지 않는다.
  Charter §4에 따라 인버스 다리는 §5.3에서 따로 보고·판정한다.

### 2.4 데이터 품질

- **유효 symbol-day:** REGULAR 1분봉이 기대 분의 95% 이상(정상일 371/390)이어야 한다. 무효 symbol-day는 거래하지 않는다.
  - 유효성은 그날 전체 봉 수로 판정한다.
  - 거래 정지일 같은 극단일이 빠질 수 있는 약한 선택 편향이 있다. 무효 symbol-day 수는 보고한다.
- **필수 정확 시각 봉:** 아래 분봉이 없으면 그 symbol-day의 해당 arm은 거래하지 않는다.
  - 공통: 09:30, 15:55.
  - A: 14:44, 14:45. A1은 신호 종목과 매수 종목 **둘 다** 있어야 한다.
  - C2: 10:29.
  - D: 신호 다음 분봉.
- **주문 스캔:** buy stop·limit·손절·목표 스캔 중 빠진 분봉은 체결 없이 건너뛴다.
- **품질 게이트:** 무효 symbol-day가 평가 symbol-day(20 × 619 = 12,380)의 5%를 넘으면 `INCONCLUSIVE_DATA`다.

## 3. 공통 체결 모델

- **포지션:** 모두 **롱**이다. 인버스 ETF 매수도 롱 거래다.
  arm × symbol-day당 최대 1회이고, 청산 후 재진입하지 않는다. 단위 명목, 자본 경쟁·복리 없음.
- **look-ahead 금지:** 분봉 t의 종가로 확정한 신호는 t+1 분봉 시가 이후에만 체결한다.
- **buy stop:** 분봉 high ≥ stop이면 `max(stop, 그 분봉 open)`에 체결한다.
- **limit buy:** 분봉 low **<** limit이면 `min(limit, 그 분봉 open)`에 체결한다(닿기만 하면 체결하지 않는다).
- **손절**
  - **진입 분봉을 포함해** low ≤ 손절가면 손절한다.
  - 진입 분봉에서는 손절가에 청산한다(보수적).
  - 그 뒤 분봉에서는 `min(손절가, 그 분봉 open)`에 청산한다.
- **목표(D만)**
  - 진입 **다음** 분봉부터 매도 지정가 = 직전 분봉 종료 시점 VWAP을 건다.
  - high ≥ 지정가면 `max(지정가, 그 분봉 open)`에 청산한다.
  - 같은 분봉에서 손절과 목표가 모두 닿으면 손절이 먼저다.
- **시간 청산:** 15:55 ET 분봉 open.
- **gross return(%p):** `(exit / entry − 1) × 100`. 비용은 §6에서 진단만 한다.

### 3.1 일봉 정의와 지표

- **일봉:** 일봉 O·H·L·C는 REGULAR 1분봉의 첫 open, 최고 high, 최저 low, 마지막 close다.
- **유효 일봉:** §2.4 기준을 충족하고 조기폐장일이 아닌 날이다. 조기폐장일의 좁은 범위가 거짓 NR 신호를 만들지 않게 하기 위해서다.
- **lookback**
  - 직전 유효 일봉 k개를 쓴다. 무효일을 건너뛰므로 TR·ROC1이 인접하지 않은 날을 이을 수 있다(장애 구간 전후).
  - **t−1(직전 NYSE 거래일)이 무효면 B·C·D 계열은 t에 거래하지 않는다.**
- **range:** H − L.
- **NR7 / NR4:** t−1 range가 t−1 포함 최근 7개 / 4개 유효 일봉 중 최소다(동률 포함).
- **inside day:** H_{t−1} < H_{t−2}이고 L_{t−1} > L_{t−2}다. t−2는 직전 유효 일봉이다.
- **stretch10:** t−1 포함 최근 10개 유효 일봉의 `min(H − O, O − L)` 평균이다.
- **SMA_n:** 최근 n개 유효 일봉 종가 평균이다.
- **ATR_n (Wilder)**
  - TR = max(H−L, |H−C_prev|, |L−C_prev|). 첫 유효 일봉의 TR은 H−L이다.
  - 첫 ATR은 TR n개의 평균이고, 이후 (prev×(n−1) + TR)/n이다.
  - 첫 유효 일봉부터 **3n개 미만이면 미확정**이다.
- **RSI_n (Wilder)**
  - 평균 상승·하락은 위와 같은 방식으로 seed한다.
  - 평균 하락 = 0이면 100이다. 평균 상승과 평균 하락이 모두 0이면 50이다.
  - 3n개 미만이면 미확정이다.
- **RSI2:** 종가 기준.
- **RSI3(ROC1):** ROC1 = C/C_prev − 1 계열의 RSI(3)이다. 3×3 + 1개 미만이면 미확정이다.
- **지표 미확정:** 그날 거래하지 않는다.
- **VWAP:** 당일 09:30부터 해당 분봉까지 Σ((H+L+C)/3 × volume) / Σvolume이다. 누적 거래량이 0이면 미확정이고 신호·목표가 없다.

## 4. arm 8개

개정 이력:
- r1의 12개(A1~A5, B1~B3, C1~C2, D1~D2)를 결과 전에 8개로 줄였다.
- **근거는 §7.1의 조건 성립 횟수(count-only)다.** 손익과 체결은 계산하지 않았다.
  - A의 QQQ ±2.0%(20일), SOXL ±6.0%(91일), D의 k=1.0(43건)은 G1·G2 미달이 확실해 제외했다.
  - QQQ ±1.5%(36일)는 단독으로 미달이라 SOXL ±4.5%와 묶었다.

### A1. 레버리지 ETF 극단일 장 마감 지속

- 신호: QQQ와 SOXL 각각 09:30 open 대비 14:44 close 수익률 R.
- 매수 조건(각 다리 독립):
  - QQQ: R ≥ +1.5% → TQQQ, R ≤ −1.5% → SQQQ.
  - SOXL: R ≥ +4.5% → SOXL, R ≤ −4.5% → SOXS.
- 진입은 14:45 open, 청산은 15:55 open. 손절 없음.
- 같은 날 두 다리가 모두 서면 두 거래이고, 일 수익은 그 평균이다.
- 성격과 검정력:
  - 묶은 152일 중 149일이 SOXL 조건일이다. **A1은 사실상 SOXL/SOXS 시험이다.** 통과해도 QQQ/TQQQ 단독 우위를 뜻하지 않는다.
  - 거래 약 185건, 거래 표준편차 수 %p에서 Holm(8)을 통과하려면 평균이 0.34%p를 크게 웃돌아야 한다.
  - 문헌 효과(약 0.6%p)가 실제여도 G6에서 떨어질 수 있는 **저검정력 arm**이다.

### A2. 대형주 극단일 장 마감 지속

- 대상: AAPL MSFT NVDA AMD AVGO AMZN META GOOGL TSLA MU NFLX CRM PLTR 각각.
- 조건: R ≥ +3.0%이고 14:44 close > 14:44 VWAP.
- 진입은 14:45 open, 청산은 15:55 open. 손절 없음.

### B. 전일 압축 후 시가 기준 돌파

공통 규칙:
- 대상: 핵심 20종 각각.
- buy stop 유효 시간: 09:31~15:30 분봉.
- 손절: O_t(당일 09:30 open). 청산: 15:55 open.

| id | t−1 조건 | buy stop |
|---|---|---|
| B1 | NR7 | O_t + stretch10 |
| B2 | inside day이면서 NR4 | O_t + stretch10 |
| B3 | C_{t−1} > SMA20 | O_t + 0.6 × range_{t−1} (Williams) |

### C. 전일 과매도 후 장중 진입

공통 규칙: 대상은 핵심 20종 각각, 청산은 15:55 open.

| id | t−1 조건 | 진입 | 손절 |
|---|---|---|---|
| C1 | RSI2 < 5이고 C_{t−1} > SMA200 | limit = C_{t−1} − 0.5 × ATR10, 유효 09:30~15:30. 09:30 open이 limit 아래면 그 open에 체결 | 없음 |
| C2 | RSI3(ROC1) < 30 (Raschke Pinball) | buy stop = 09:30~10:29 고가, 유효 10:30~15:30 | 09:30~10:29 저가 |

- C1은 §7.1 기준 조건 성립이 282건뿐이다. 지정가 체결률을 감안하면 **SAMPLE 실패 가능성이 높다고 미리 표시한다.**
- r1의 C1 다음 날 시가 청산 진단은 OOS·제외 구간 침범 문제로 **삭제**했다.

### D1. VWAP/ATR 극단 회귀

- 대상: SOXL SOXS TQQQ SQQQ TSLA PLTR AMD MU AVGO 각각.
- 신호: 10:00~15:00 분봉 close ≤ VWAP − 0.6 × ATR14_{t−1}(첫 발생만).
- 진입: 다음 분봉 open.
- 목표: §3 VWAP 매도 지정가.
- 손절: 진입가 − 0.3 × ATR14_{t−1}.
- 청산: 15:55 open.

## 5. 통계와 판정

### 5.1 집계

- **거래 수익:** arm별 거래 gross return을 계산한다. G3·G4·G5·G7은 거래당 평균을 쓴다.
- **일 수익:** 같은 거래일 거래 수익의 산술평균이다(거래가 있는 날만). G6은 일 수익 계열로 검정한다.
  같은 날 거래를 독립 표본으로 세지 않기 위해서다.
- **거래 순서:** (거래일, 진입 시각, 종목) 오름차순.

### 5.2 arm별 `screen_pass` (전부 충족)

| 게이트 | 기준 |
|---|---|
| G1 | 거래 수 ≥ 100 |
| G2 | 거래가 있는 일수 ≥ 50 |
| G3 | 거래당 gross 평균 ≥ **0.45%p** |
| G4 | 거래 순서상 앞 ⌊n/2⌋건과 나머지 각각의 거래당 평균 > **0.34%p**. 같은 날 거래가 경계에 걸쳐도 순서대로 나눈다 |
| G5 | 거래 gross **합**이 가장 큰 달력월 하나를 뺀 거래당 평균 ≥ 0.34%p |
| G6 | 일 수익 평균에 대한 H0: 평균 ≤ 0.34%p 단측 검정. **Holm(8)** 조정 p < 0.05 |
| G7 | 거래 gross 합이 가장 큰 **종목** 하나를 뺀 거래당 평균 ≥ 0.34%p. 단일 종목 arm은 해당 없음 |
| G8 | **최근 1년**(체결일 2025-01-02~2026-01-20) 거래가 30건 이상이고 그 거래당 평균 > 0.34%p |

G8은 2026-10-01 사용자 결정으로 추가했다.
- 취지: 2025~2026년 AI·반도체 주도 국면 변화를 반영해, 오래된 구간에서만 통하는 규칙을 1차에서 거른다.
- 2026년 이후(blind OOS·H2)는 선택에 쓰지 않고 새 국면의 최종 시험으로 남긴다.

G6 bootstrap 설정:
- circular block bootstrap. 블록 길이 5(거래가 있는 날 계열의 인덱스 기준).
- 재표본 길이는 원 계열 길이 n이고, 블록 시작점은 균등 추출한다.
- 10,000회. `numpy.random.default_rng(20261001)`(PCG64)를 쓴다.
- p = (재표본 평균 ≤ 0.34인 수 + 1) / (10,000 + 1).
- Holm 단계 조정은 8개 arm의 p를 오름차순으로 정렬해 i번째에 (8 − i + 1)을 곱하고, 누적 최댓값을 취한다.

### 5.3 인버스 다리 (Charter §4)

- SQQQ·SOXS 거래가 있는 arm(A1, B1~B3, C1~C2, D1)은 전체 외에 **비인버스 다리**와 **인버스 다리** 통계를 따로 보고한다.
- `screen_pass` arm을 LONG 후보로 나열하려면 **비인버스 다리만으로도** 다음을 모두 충족해야 한다.
  - 거래 ≥ 50건.
  - 거래일 ≥ 25일.
  - G3·G4·G5·G7·G8(단일 종목만 남으면 G7 해당 없음).
- 인버스 다리만으로 통과한 결과는 `INVERSE_ONLY`로 별도 표시하고 LONG 후보로 승인하지 않는다.

### 5.4 진단 (판정 비반영)

- **무조건부 기준선** (모든 arm 공통 정의)
  - 실제 거래 i(종목 s, 진입 분 m)마다 baseline_i를 구한다.
    - baseline_i: s의 모든 평가 유효일 중 m과 15:55 분봉이 있는 날의 `(open_15:55 / open_m − 1) × 100` 평균.
    - 분봉 open을 쓰고 stop·limit 가격은 쓰지 않는다.
  - arm 기준선은 baseline_i의 평균이다. 거래 평균 − 기준선 평균을 보고한다.
  - D1은 목표가 청산이 있어서 기준선이 drift 참고값일 뿐, 같은 청산 조건으로 맞춘 비교가 아니다.
- **집중도:** 최선 거래일과 그날 기여분, 종목별 거래 수·평균.
- **품질:** 무효 symbol-day 수, 필수 시각 봉 결측 수.

### 5.5 전체 verdict

- `SHORTLIST`: §5.3 조건까지 충족하는 `screen_pass` arm이 하나 이상. 통과 arm을 **모두** 나열하고 하나를 고르지 않는다.
- `NO_GROSS_EDGE`: 그런 arm이 없다. G1·G2 미달은 `SAMPLE`, 인버스만 통과하면 `INVERSE_ONLY`로 표시한다.
- `INCONCLUSIVE_DATA`: identity, 구간, §2.4 품질, 결정론 결함으로 판정 불가.

어떤 verdict도 blind OOS 개봉, H2 접근, Paper 실행을 자동 승인하지 않는다.
`SHORTLIST` arm은 정식 사전등록에서 비용, 정수 주식 사이징, 위험 한도, 표본 기준을 다시 고정한 뒤 OOS unlock 계약을 거친다.

## 6. 비용 진단 (판정 비반영)

| 기준 | 왕복 %p | 구성 |
|---|---|---|
| commission_only | 0.20 | Toss 0.1%/편도 |
| realistic | 0.30 | 수수료 + 편도 5bp 슬리피지 |
| charter_official | 0.34 | Charter §3 공식 기준(G4~G7의 기준선) |
| stress | 0.51 | freeze-paper 계약 stress |

## 7. 이미 본 정보 (결과 전 공개)

- **이전 시도 결과:**
  - v1 screen 8가설 `NO_GROSS_EDGE`, H4 `NULL`.
  - ORB·Gap+RVOL `INCONCLUSIVE_DATA`.
  - gap-down fade, afternoon continuation, SPY intraday momentum, box RSI, DEMA/Stoch `NO_SELECTION`.
  - 이들의 거래당 gross 역산값(−0.10~+0.12%p).
  - 실패 분석 문서는 intraday 시도 9건(v1 screen·H4 포함)을 다룬다.
- **기회 크기:** development 2024-09-03~2026-01-20 핵심 20종의 무조건부 움직임 크기(탐색 문서 §1).
- **D9 결손률:** 종목별 결손률과 blind OOS 구간 봉 존재 여부(가격 아님).
- **H2 봉 수:** 2026-10-01 복구 후 운영 DB에서 핵심 20종 2026-07-22~09-30 REGULAR 봉 **수**를 확인했다(가격 미열람).
- **원 근거:** 공개 문헌과 실전 트레이더의 원 규칙·보고 효과.
  B·C 원 규칙은 2차 요약으로 확인했다. 이 문서의 정의가 정본이다.
- **누적 탐색:** development 구간은 9건 이상의 시도로 반복 탐색됐다.
  그래서 경제 기준(G3 0.45, G4·G5·G7·G8 0.34)과 Holm(8)을 함께 요구한다.

### 7.1 조건 성립 횟수 (count-only, 2026-10-01)

- snapshot을 `immutable=1`로 열고 `<= 2026-01-20`으로 거른 REGULAR 1분봉만 읽었다(최대 일자 단언).
- 평가 619일 기준 **조건 성립** 횟수다. 손익과 체결 여부는 계산하지 않았다.
- r1 arm 기준이며, 이 결과로 §4의 축소를 결정했다.
- 재현: `scripts/gross_edge_v2_count_only_pass.py`(SHA-256 `9254ab2190b325509396acdb271ee6feb4c2172306af90df0494b6a5d1156547`).
  lint 정리본으로 다시 실행해 아래 수치와 같음을 확인했다.

| r1 조건 | symbol-day | 거래일 |
|---|---:|---:|
| QQQ \|R\| ≥ 1.5% / 2.0% | — | 36 / 20 |
| SOXL \|R\| ≥ 4.5% / 6.0% | — | 149 / 91 |
| A1 묶음(QQQ 1.5 + SOXL 4.5) | 185 | 152 |
| A2(구 A5) 대형주 +3%·VWAP 위 | 418 | 234 |
| B1 NR7 | 1,854 | 461 |
| B2 inside+NR4 | 877 | 344 |
| B3 C>SMA20 | 7,299 | 612 |
| C1 RSI2<5·SMA200 위 | 282 | 119 |
| C2 RSI3(ROC1)<30 | 1,312 | 408 |
| D k=0.6 / 1.0 | 408 / 43 | 200 / 27 |

## 8. 구현 전 필수 테스트

- 8개 arm registry, 신호·진입·청산 시각, 파라미터 고정.
- 미래봉 미사용: 신호 확정 후 다음 분봉 체결, 일봉 지표는 t−1까지, D 목표는 직전 분봉 VWAP 지정가.
- buy stop, strict limit, 진입 분봉 포함 손절, 같은 분봉 충돌 시 손절 우선, 재진입 금지.
- 유효 일봉, 조기폐장 무효, lookback, 3n warm-up, RSI 0/0 처리. t−1 무효일 B·C·D 거래 금지.
- 필수 정확 시각 봉 결측 시 거래 제외. A1 양 종목 요구.
- 2024-07-01~08-30과 조기폐장일 제외. 2026-01-20 초과 데이터 미로드 단언.
- 일 수익 집계, circular block bootstrap 결정론(seed, PCG64), Holm(8).
- 인버스·비인버스 분리 통계와 `INVERSE_ONLY` 판정. G7 최선 종목 제외. G8 최근 1년 구간 경계. 무조건부 기준선.
- blind OOS·H2 접근 차단, identity fail-closed(파일 SHA + receipt), 운영 DB 미접근.
- 입력 순서 불변, 결과 JSON byte 결정론, JSON ↔ Markdown 정합.

## 9. 실행 규율

1. 이 문서와 동형 JSON을 사용자 승인 후 FROZEN으로 바꿔 별도 커밋한다.
2. 그 뒤 구현·테스트를 커밋하고 독립 리뷰를 받는다.
3. 공식 실행은 **한 번** 한다.
4. 결과 문서는 별도 커밋한다.

데이터 무효화 결함이 아닌 한 결과를 본 뒤 이 계약을 개정하지 않는다.
