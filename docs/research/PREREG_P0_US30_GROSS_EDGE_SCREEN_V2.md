# P0-US30 Intraday Gross-Edge Screen — 사전등록 v2.0

상태: **DRAFT — 사용자 승인·독립 리뷰 후 FROZEN BEFORE RESULTS로 전환**
작성: 2026-10-01
근거 문서:
- [후보 탐색](EXPLORATION_DAYTRADING_CANDIDATES_20261001.md)
- [실패 분석](ANALYSIS_DAYTRADING_FAILURES_AND_TOSS_DATA_QUALITY_20261001.md)
- [v1 screen](PREREG_P0_US30_GROSS_EDGE_SCREEN_V1.md)

목적: blind OOS를 열지 않고, 고정 development 구간에서 다음 정식 전략 사전등록으로 넘길 만한
일중 롱 규칙이 있는지만 선별한다. v1 결과(`NO_GROSS_EDGE`)를 재판정하거나 개정하지 않는다.
v2는 새 후보군에 대한 별도 screen이다.

## 1. 연구 질문과 금지 사항

> 고정된 4개 계열 12개 일중 롱 규칙 중, 비용 적용 전 거래당 gross return이
> 실제 비용 장벽(왕복 약 0.30%p)을 여유 있게 넘고, 시간과 표본에 걸쳐 안정적인 후보가 있는가?

다음을 금지한다.

- **접근 금지 구간**
  - blind OOS(2026-01-21~2026-07-21)의 가격·성과.
  - 신규 holdout(2026-07-22~2026-09-30)의 가격·성과.
- **결과 기반 조작**
  - arm별 임계값 탐색.
  - 결과를 본 뒤 규칙·판정 기준 변경.
  - 최선 arm 하나만 보고하기.
- **운영 변경:** live/order/broker/risk/config, 진행 중인 freeze-paper 계약.
- **통계 단위:** 동일 날짜의 여러 거래를 독립 일수로 세기.

## 2. 데이터 identity와 구간

### 2.1 데이터 identity

v1과 동일한 frozen snapshot을 쓴다. 해시 4개는 v1 §2와 같다.

- snapshot: `p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite`
- snapshot file SHA-256: `3c2c475125594bd7fe8c59d994bdc6d4cb9f1f1e0b69005b9fbcbf5eb852ca33`
- snapshot content SHA-256: `d0d145518938686937895fb3c886a4c2fd996588f129e8234a5913b6be2597e4`
- manifest data SHA-256: `e270a1b56d2466a856ef92e32abb58730953b7b77ce1038d7b596f66f477bd0c`
- raw content SHA-256: `22c02118c7c8bffc225fc021e2c4eb9ad4bf7b1d8c66df8e927909c46e609ed4`

규칙:
- 해시 불일치나 검증 불가면 `INCONCLUSIVE_DATA`로 판정하고 성과 계산을 시작하지 않는다.
- 가격 로드는 `research_load(... purpose="performance", track="intraday")`를 경유한다.
- **일봉 지표도 같은 snapshot의 REGULAR 1분봉에서 만든다.** 운영 `market_history.sqlite`와 swing(1d) 트랙 DB는 읽지 않는다.

### 2.2 구간

- **development 평가:** 2023-05-18~2026-01-20(670 NYSE 거래일). 그중 **2024-07-01~2024-08-30(44일)을 통째로 제외**해 626일을 평가한다.
  - 제외 사유: 2024-07-25~08-02 Toss 수집 공백과 lookback 전파. 근거는 실패 분석 §5.2다.
  - 제외 결정은 결과를 보기 전에 데이터 품질만으로 내렸다.
- **일봉 지표 warm-up:** 2026-01-20 이전 snapshot 전체 REGULAR 봉을 쓸 수 있다.
  2023-05-18 이전 봉은 지표 계산에만 쓰고 거래 평가에는 쓰지 않는다.
- **blind OOS:** 2026-01-21~2026-07-21. 계속 잠그며, `OOS_UNLOCK_VERIFICATION_CONTRACT.md`를 따른다.
- **신규 holdout H2:** 2026-07-22~2026-09-30. 이 screen은 읽지 않는다.
  통과 arm의 정식 사전등록이 H2 사용 여부와 방법을 결과 전에 정한다.

### 2.3 종목

- **핵심 20종:** SPY QQQ IWM TQQQ SQQQ SOXL SOXS AAPL MSFT NVDA AMD AVGO AMZN META GOOGL TSLA MU NFLX CRM PLTR.
  섹터 ETF 7종, VST, DIA, SMH는 D9 품질 사유로 제외한다.
- **SQQQ·SOXS 가격:** 역분할 소급조정 가격이다. %수익과 ATR 비율은 유효하지만 주식 수 계산에는 쓰지 않는다.
- **레버리지 ETF:** TQQQ·SOXL과 기초 지수는 같은 신호의 확대본이다. 독립 표본으로 세지 않는다.

### 2.4 데이터 품질 게이트

- 정규장 기대 분 대비 1분봉이 95% 미만인 symbol-day는 그날 거래하지 않는다.
  기준은 정상일 390분 중 371분, 조기폐장일 210분 중 200분이다.
- 그런 symbol-day가 평가 symbol-day의 5%를 넘으면 `INCONCLUSIVE_DATA`다.

## 3. 공통 체결 모델

- **포지션:** 모두 **롱**이다. 인버스 ETF 매수도 롱 거래로 본다.
  arm × symbol-day당 최대 1회, 단위 명목, 자본 경쟁·복리 없음.
- **세션:** REGULAR만 쓴다. 현재 거래일이 조기폐장일이면 그 symbol-day를 제외한다.
- **look-ahead 금지:** 분봉 t의 종가로 확정한 신호는 t+1 분봉 시가 이후에만 체결한다.
- **주문 체결 규칙:**
  - **buy stop:** 해당 분봉 high ≥ stop이면 `max(stop, 그 분봉 open)`에 체결한다.
  - **limit buy:** 해당 분봉 low ≤ limit이면 `min(limit, 그 분봉 open)`에 체결한다.
  - **손절:** 진입 분봉 다음 분봉부터 low ≤ 손절가면 `min(손절가, 그 분봉 open)`에 청산한다.
  - **충돌:** 같은 분봉에서 손절과 목표가 모두 닿으면 손절로 본다(보수적).
- **시간 청산:** 15:55 ET 분봉 open.
- **gross return(%p):** `(exit / entry − 1) × 100`. 비용은 §6에서 진단만 한다.

### 3.1 일봉 정의와 지표

- **일봉:** 일봉 O·H·L·C는 해당 거래일 REGULAR 1분봉의 첫 open, 최고 high, 최저 low, 마지막 close다.
- **유효 일봉:** 그날 1분봉 수가 §2.4의 95% 기준 이상이어야 한다. 무효 일봉은 일봉 계열에서 빠진다.
  lookback은 직전 유효 일봉 k개를 쓴다. **t−1이 무효면 t에 B·C 계열을 거래하지 않는다.**
- **range:** H − L.
- **NR7 / NR4:** t−1 range가 t−1 포함 최근 7개 / 4개 유효 일봉 중 최소(동률 포함)다.
- **inside day:** H_{t−1} < H_{t−2}이고 L_{t−1} > L_{t−2}다.
- **stretch10:** t−1 포함 최근 10개 유효 일봉의 `min(H − O, O − L)` 평균이다.
- **SMA_n:** 최근 n개 유효 일봉 종가 평균이다.
- **ATR_n:** Wilder ATR(n)이고, 첫 값은 TR n개의 평균이다.
- **RSI2:** 종가 기준 Wilder RSI(2)다.
- **RSI3(ROC1):** ROC1 = C_t/C_{t−1} − 1 계열의 Wilder RSI(3)다.
- **지표 미확정:** lookback이 부족한 지표는 미확정이고, 그날 거래하지 않는다.
- **VWAP:** 당일 09:30부터 해당 분봉까지 Σ(typical × volume)/Σvolume, typical = (H+L+C)/3이다.
  누적 거래량이 0이면 미확정이다.

## 4. arm 12개

### A. 극단일 장 마감 지속 (5)

공통 규칙:
- 신호: 09:30 open 대비 14:44 분봉 close의 수익률 R.
- 진입: 14:45 분봉 open. 청산: 15:55 open. 손절 없음.

| id | 신호 종목 | 조건 → 매수 종목 |
|---|---|---|
| A1 | QQQ | R ≥ +1.5% → TQQQ, R ≤ −1.5% → SQQQ |
| A2 | QQQ | R ≥ +2.0% → TQQQ, R ≤ −2.0% → SQQQ |
| A3 | SOXL | R ≥ +4.5% → SOXL, R ≤ −4.5% → SOXS |
| A4 | SOXL | R ≥ +6.0% → SOXL, R ≤ −6.0% → SOXS |
| A5 | 대형주 13종 각각 | R ≥ +3.0%이고 14:44 close > VWAP → 그 종목 |

### B. 전일 압축 후 시가 기준 돌파 (3)

공통 규칙:
- 대상: 핵심 20종 각각.
- buy stop 유효 시간: 09:31~15:30 분봉.
- 손절: O_t(당일 시가). 청산: 15:55 open.

| id | t−1 조건 | buy stop |
|---|---|---|
| B1 | NR7 | O_t + stretch10 |
| B2 | inside day이면서 NR4 | O_t + stretch10 |
| B3 | C_{t−1} > SMA20 | O_t + 0.6 × range_{t−1} (Williams) |

### C. 전일 과매도 후 장중 진입 (2)

공통 규칙: 대상은 핵심 20종 각각, 청산은 15:55 open.

| id | t−1 조건 | 진입 | 손절 |
|---|---|---|---|
| C1 | RSI2 < 5이고 C_{t−1} > SMA200 | limit = C_{t−1} − 0.5 × ATR10, 유효 09:30~15:30 | 없음 |
| C2 | RSI3(ROC1) < 30 (Raschke Pinball) | buy stop = 첫 1시간(09:30~10:29) 고가, 유효 10:30~15:30 | 첫 1시간 저가 |

C1 진단(판정 비반영): 같은 체결로 다음 거래일 09:30 open 청산 수익도 보고해 우위가 장중에 있는지 overnight에 있는지 본다.

### D. VWAP/ATR 극단 회귀 (2)

| id | 대상 | 신호 | 진입 | 목표 | 손절 |
|---|---|---|---|---|---|
| D1 | SOXL SOXS TQQQ SQQQ TSLA PLTR AMD MU AVGO | 10:00~15:00 분봉 close ≤ VWAP − 0.6 × ATR14_{t−1} | 다음 분봉 open | 진입 후 분봉 close ≥ 그 분봉 VWAP이면 그 close에 청산 | 진입가 − 0.3 × ATR14_{t−1} |
| D2 | 동일 | k = 1.0 | 동일 | 동일 | 진입가 − 0.5 × ATR14_{t−1} |

손절폭은 0.5 × k × ATR이다. 시간 청산은 15:55 open이다.

## 5. 통계와 판정

### 5.1 집계

- **거래 수익:** arm마다 거래별 gross return을 계산한다.
- **일 수익:** 같은 거래일의 거래 수익을 산술평균한 값이다(거래가 있는 날만).
- **통계 단위:** 유의성 검정은 일 수익 계열로 한다. 같은 날 거래를 독립 표본으로 세지 않는다.

### 5.2 arm별 `screen_pass` (전부 충족)

| 게이트 | 기준 |
|---|---|
| G1 | 거래 수 ≥ 100 |
| G2 | 거래가 있는 일수 ≥ 50 |
| G3 | 거래당 gross 평균 ≥ **0.45%p** |
| G4 | 거래를 시간순으로 반분(거래 수 기준)했을 때 두 절반의 거래당 평균이 각각 > **0.30%p** |
| G5 | 가장 좋은 달력월 하나를 뺀 거래당 평균 ≥ 0.30%p |
| G6 | 일 수익 계열의 moving-block bootstrap으로 H0: 평균 ≤ 0.30%p를 단측 검정한다. Bonferroni 12를 적용한 p < 0.05 |

G6 bootstrap 설정:
- 블록 길이 5(거래일 계열 기준), 10,000회, 고정 seed 20261001.
- p = (평균 ≤ 0.30인 재표본 수 + 1) / (10,000 + 1).
- adjusted_p = min(12 × p, 1).

### 5.3 전체 verdict

- `SHORTLIST`: `screen_pass` arm이 하나 이상. 통과 arm을 **모두** 나열하고 하나를 고르지 않는다.
- `NO_GROSS_EDGE`: 통과 arm이 없다. G1·G2 미달 arm은 `SAMPLE` 실패로 따로 표시한다.
- `INCONCLUSIVE_DATA`: identity, 구간, §2.4 품질, 결정론 결함으로 판정 불가.

어떤 verdict도 blind OOS 개봉, H2 접근, Paper 실행을 자동 승인하지 않는다.
`SHORTLIST` arm은 정식 사전등록에서 비용, 정수 주식 사이징, 위험 한도, 표본 기준을 다시 고정한다.
그 뒤 OOS unlock 계약을 거친다.

## 6. 비용 진단 (판정 비반영)

gross 평균에서 아래 왕복 비용을 단순 차감해 보고한다.

| 기준 | 왕복 비용 | 구성 |
|---|---|---|
| commission_only | 0.20%p | Toss 0.1%/편도 |
| realistic | 0.30%p | 수수료 + 편도 5bp 슬리피지 |
| stress | 0.51%p | freeze-paper 계약 stress |

## 7. 이미 본 정보 (결과 전 공개)

- **이전 시도 결과:**
  - v1 screen 8가설 `NO_GROSS_EDGE`.
  - ORB·Gap+RVOL `INCONCLUSIVE_DATA`.
  - gap-down fade, afternoon continuation, SPY intraday momentum, box RSI, DEMA/Stoch `NO_SELECTION`.
  - 이들의 거래당 gross 역산값(−0.10~+0.12%p).
- **기회 크기:** development 2024-09-03~2026-01-20 핵심 20종의 **무조건부** 움직임 크기.
  일중 범위, 갭, 첫 30분, 마지막 60분 분포를 봤다(탐색 문서 §1). 어떤 규칙의 손익이나 조건부 선행 수익도 계산하지 않았다.
- **D9 결손률:** 종목별 결손률과 blind OOS 구간 봉 **존재 여부**(가격 아님).
- **원 근거:** 공개 문헌과 실전 트레이더의 원 규칙·보고 효과.
  B·C의 원 규칙은 2차 요약으로 확인했다. 이 문서의 정의가 정본이며, 원전과 다를 수 있다.
- **누적 탐색:** development 구간은 v1을 포함해 9건 이상의 시도로 반복 탐색됐다.
  그래서 G3·G4의 경제 기준(0.45 / 0.30)과 Bonferroni 12를 함께 요구한다.

## 8. 구현 전 필수 테스트

- 12개 arm registry, 신호·진입·청산 시각, 파라미터 고정.
- 미래봉 미사용: 신호 확정 후 다음 분봉 체결, 일봉 지표는 t−1까지만 사용.
- buy stop / limit / 손절 / 목표 체결 규칙, 같은 분봉 충돌 시 손절 우선.
- 유효 일봉, lookback, 지표 미확정 처리. t−1 무효일 거래 금지.
- 2024-07-01~08-30 제외와 조기폐장일 제외.
- 일 수익 집계와 bootstrap 결정론(seed). Bonferroni 12.
- blind OOS·H2 접근 차단, identity fail-closed. 운영 DB 미접근.
- 입력 순서 불변, 결과 JSON byte 결정론, JSON ↔ Markdown 정합.

## 9. 실행 규율

1. 이 문서와 동형 JSON을 사용자 승인과 독립 리뷰 후 FROZEN으로 바꿔 별도 커밋한다.
2. 그 뒤 구현·테스트를 커밋하고 공식 실행을 **한 번** 한다.
3. 결과 문서는 별도 커밋한다.

데이터 무효화 결함이 아닌 한 결과를 본 뒤 이 계약을 개정하지 않는다.
