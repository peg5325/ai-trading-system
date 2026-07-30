# 사전등록 — P0-US30 Gap + RVOL Continuation v1

> 작성: 2026-07-30 · 이슈 #98
> 성격: **사전등록(pre-registration).** 이 문서는 결과 전에 규칙을 잠근다.
> 상태: **Gap+RVOL standalone 결과 미열람.** 구현·백테스트·성과·forward return·blind OOS는
> 이번 단계에서 실행하지 않는다.

근거 계약:
`DATA_CONTRACT_4STRATEGY_V1.md` · `PLAN_ONE_MONTH_STRATEGY_SELECTION_AI_SHADOW.md` ·
`DECISION_DAYTRADING_GAP_RVOL_NEXT.md`

## 0. 한 문장

**P0-US30에서 gap-up과 첫 15분 RVOL을 통과한 종목이 gap의 절반 이상을 유지하며 초기 눌림을
만든 뒤, 완료 5분봉으로 직전 봉 고가와 causal session VWAP을 회복할 때 다음 봉 시가로 진입하고
60분 또는 EOD 전에 청산하는 독립 당일 continuation 가설을 결과 전에 고정한다.**

## 1. 이전 결과 접근 공개와 독립성

### 1.1 이미 본 것

- ORB development 결과 `RESULT_P0_US30_ORB_DEVELOPMENT.md`를 읽었다.
- ORB primary 및 진단 변형이 development gate를 통과하지 못한 사실을 알고 있다.
- H4 및 이전 기술점수 실험 결과도 저장소 결정 문서에 공개돼 있다.

### 1.2 아직 보지 않은 것

- 이 문서의 pullback/reclaim 규칙으로 생성한 신호 수·거래 수·수익률·expectancy·PF·MDD.
- 이 전략의 development fold 결과와 모든 blind OOS 성과.

### 1.3 ORB와 구조적 분리

- 후보의 `gap 0.30%`, 15분 `RVOL 1.5`, target `2R`은 **ORB 결과 전 고정값을 그대로 재사용**한다.
- `OR_high`, `OR_low`, opening-range breakout/touch는 신호·stop·target 어디에도 쓰지 않는다.
- 진입은 `pullback armed → 직전 5분봉 고가 strict reclaim + causal VWAP 상회`다.
- stop은 opening range가 아니라 **pullback 구간 최저가**다.
- ORB 결과를 보고 gap/RVOL/target primary를 바꾸지 않는다.

## 2. 데이터 identity와 유니버스

| 항목 | 값 |
| --- | --- |
| snapshot basename | `p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite` |
| snapshot file SHA-256 | `3c2c475125594bd7fe8c59d994bdc6d4cb9f1f1e0b69005b9fbcbf5eb852ca33` |
| snapshot content SHA-256 | `d0d145518938686937895fb3c886a4c2fd996588f129e8234a5913b6be2597e4` |
| manifest data SHA-256 | `e270a1b56d2466a856ef92e32abb58730953b7b77ce1038d7b596f66f477bd0c` |
| raw content SHA-256 | `22c02118c7c8bffc225fc021e2c4eb9ad4bf7b1d8c66df8e927909c46e609ed4` |
| collection run_id | `p0-us30-1m-20260721-r82` |
| as_of | `2026-07-21` |
| 원천 | 1m, PRE/REGULAR/POST, OVERNIGHT 0행 |

4개 hash 중 하나라도 불일치하거나 raw artifact가 없으면 `NOT_RUN`이다. 원천 1분봉에서 canonical
리샘플러로 5분봉을 파생하며 원천 5분봉을 다운로드하지 않는다.

유니버스는 아래 30종목으로 고정한다.

- 시장 ETF: SPY, QQQ, IWM, DIA
- 섹터 ETF: XLK, XLF, XLE, XLV, XLI, XLY, XLU, SMH
- 대형 성장·반도체: AAPL, MSFT, NVDA, AMD, AVGO, AMZN, META, GOOGL, TSLA, MU, NFLX, CRM, PLTR, VST
- 레버리지·인버스: TQQQ, SQQQ, SOXL, SOXS

QQQ/TQQQ/SQQQ는 `QQQ_FAMILY`, SMH/SOXL/SOXS는 `SOX_FAMILY`로 묶는다. 고정 현재 유동성 표본이며
point-in-time 전체 시장·상장폐지 포함 모집단·뉴스 catalyst 전략으로 일반화하지 않는다.

## 3. 시간창과 blind OOS

| 구간 | 날짜(ET 거래일, 양끝 포함) | 거래일 |
| --- | --- | ---: |
| warm-up 포함 development data | 2023-04-20 ~ 2026-01-20 | 690 |
| development 평가 | 2023-05-18 ~ 2026-01-20 | 670 |
| blind OOS | 2026-01-21 ~ 2026-07-21 | 125 |

- development는 OOS 봉을 사용할 수 없다.
- OOS의 RVOL warm-up은 2026-01-20 이전 봉을 사용할 수 있다.
- 10개 chronological fold는 ORB 사전등록과 동일한 날짜 경계를 쓴다: F01 103일, F02~F10 각 63일.
- 파라미터를 학습하지 않으므로 fold 사이 embargo는 0일이다. fold는 시간 안정성만 측정한다.
- 모든 performance load는 `src.history.oos_guard.research_load(..., track="intraday")`를 쓴다.
- `purpose="coverage"`를 성과 경로에서 쓰거나 direct `load_candles`로 우회하면 분석 무효다.

## 4. 후보 피처 — 결과 전 고정

### 4.1 gap-up

`previous_close`는 직전 NYSE 거래일의 마지막 완료 REGULAR 1분봉 종가다. `today_open`은 당일
09:30 REGULAR 1분봉 시가다.

`gap = (today_open - previous_close) / previous_close`

primary는 **gap ≥ +0.30%**다. 절대값이 아니며 gap-down 개별주 short는 하지 않는다. SQQQ/SOXS도
자기 자신의 gap-up 조건을 만족할 때만 long으로 거래한다.

### 4.2 RVOL

`today_volume_15m`은 당일 REGULAR 09:30~09:44의 15개 완료 1분봉 거래량 합이다. 분모는 동일
시각창을 모두 가진 직전 20 거래일 합계의 median이며 최소 15개 유효 과거일이 필요하다.

`RVOL = today_volume_15m / median(prior valid 15m volumes)`

primary는 **RVOL ≥ 1.5**다. 오늘 거래량은 분모에 넣지 않고, 미래 09:45 이후 거래량은 쓰지 않는다.

### 4.3 causal session VWAP

각 완료 5분봉 `t`에서 VWAP은 당일 REGULAR 09:30부터 `t`의 마지막 1분까지의
`sum(typical_price × volume) / sum(volume)`이다. typical price는 `(high+low+close)/3`이고,
PRE/POST 및 `t` 이후 봉은 제외한다. 누적 volume이 0이면 그 종목-일은 데이터 배제다.

## 5. pullback과 continuation 신호

결정봉은 canonical 5분봉이며 라벨은 버킷 시작 시각이다. `min_bars=5`, `closed_only=True`로
완전하고 닫힌 REGULAR 버킷만 사용한다.

### 5.1 gap retention floor

`retention_floor = previous_close + 0.50 × (today_open - previous_close)`

09:45 이후 신호 전 완료봉의 종가가 이 바닥 **미만**이면 setup은 그 날 영구 무효다. 같은 날
재무장하지 않는다. equality는 유지로 본다.

### 5.2 pullback armed

- 09:45 시작 봉부터 관찰한다.
- 해당 완료봉 종가가 직전 완료 5분봉 종가보다 **strict 낮고**, retention floor 이상이면
  pullback이 armed된다.
- 첫 armed 봉부터 신호봉까지의 low 최솟값을 `pullback_low`로 계속 갱신한다.
- armed와 continuation 신호는 같은 봉에서 동시에 성립할 수 없다.

### 5.3 continuation trigger

armed 이후 처음으로 아래를 모두 만족한 완료 5분봉이 신호다.

1. `close > immediately_previous_completed_5m.high` (strict)
2. `close > causal_session_vwap` (strict)
3. `close > open` (green bar, strict)
4. `close >= retention_floor`

**OR high/low 및 OR breakout은 보지 않는다.** primary의 신규 진입 체결 시각은
**10:30 ET 이하**다. 신호는 다음 완료 5분봉 시가 체결이므로 10:25 시작 신호봉이 마지막이다. 다음 scheduled
봉이 없거나 10:30을 넘으면 `signal_expired`로 버린다.

신호봉 체결·touch fill·signal 재대기·같은 날 재진입·물타기·scale-in은 모두 금지한다.

## 6. 체결·risk·청산

- 방향: long-only.
- 진입: 신호 다음 완료 5분봉 open. gap-through entry도 가격 개선 없이 실제 open.
- `stop = pullback_low`(armed 첫 봉부터 신호봉까지의 low 최솟값).
- `1R = entry - stop`; 0 이하이면 거래하지 않고 `one_r_not_positive`.
- target: **entry + 2.0 × 1R**.
- risk budget: fixed initial capital 100,000 USD의 1%; running equity로 sizing하지 않는다.
- 심볼 notional 20%, 심볼 실질노출 30%, 포트폴리오 실질노출 50%, 동시 포지션 2를 cap으로 적용한다.
- 같은 봉 stop/target 동시 touch는 stop 우선. stop/target gap-through는 봉 open 체결.
- 진입봉도 stop/target 판정에 포함한다.
- **60분 time stop**: 진입 시각부터 12개 5분 버킷을 보유한 뒤에도 열려 있으면 다음 가능한
  5분봉 open으로 청산한다. stop/gap-through 판정을 time-stop보다 먼저 한다.
- EOD: 정규장 마감 10분 전 decision 후 다음 봉 open으로 청산하며, 조기폐장은 거래소 캘린더를 쓴다.
- time stop과 EOD가 같은 봉이면 EOD로 기록한다. 오버나잇은 금지다.
- 정규장 기대봉 3개 연속 결측이면 다음 존재 REGULAR 5분봉 open으로 stale exit한다.

청산은 두 단계로 판정한다.

1. 봉 open 판정: `open <= stop`이면 stop, 그 외 `open >= target`이면 target, 그 외
   stale/EOD/time 기한이 도래했으면 각각 stale/EOD/time으로 실제 open 체결한다.
2. intrabar 판정: open에서 끝나지 않은 경우 low가 stop 이하이면 stop을 먼저 체결하고, 그 외
   high가 target 이상이면 target을 체결한다.

따라서 같은 봉 stop/target touch는 stop 우선이고, gap-through target은 target보다 낮은 stop을
동시에 통과할 수 없으므로 실제 open 체결이다. EOD와 time 기한이 같은 봉이면 EOD로 기록한다.

## 7. 포트폴리오 경쟁

동일 체결 시각 후보는 다음 순서로 정렬한다.

1. RVOL 내림차순
2. gap 내림차순
3. symbol ASCII 오름차순

같은 봉에서는 기존 포지션 청산 후 신규 진입을 처리한다. 먼저 채택된 후보는 같은 봉의 slot·family·
실질노출 cap에 즉시 반영한다. capacity로 건너뛴 신호는 재대기하지 않는다. 심볼당 하루 최대 1회다.

## 8. 비용과 평가 정의

round-trip notional 비용은 low **0.14%p**, official **0.34%p**, stress **0.68%p**다. 각 절반을
진입·청산에 배분하며 commission/slippage/spread를 포함한다. 추가 fee를 중복 차감하지 않는다.
공식 판정은 0.34%p만 사용하고 나머지는 민감도다.

- net expectancy: 거래당 net PnL 산술평균(USD 및 per-1R 병기).
- PF: 양의 net PnL 합 / 음의 net PnL 절댓값 합.
- MDD: 100,000 USD t0를 포함한 daily realized official-cost equity curve의 running peak 대비 하락.
- 거래일: 1건 이상 거래가 있는 ET 거래일.
- 집중도: official-cost 총 net PnL이 양수일 때만 심볼-family/날짜 기여율을 계산하며, 총 net≤0이면 자동 실패.
- 중간 반올림 없음, 표시는 2자리.

## 9. primary와 진단 이웃

GO/NO-GO 권한은 primary 하나뿐이다. 아래 이웃은 development parameter cliff 진단용이며 primary를
구제할 수 없고, 현재 blind OOS에서 영구 실격이다.

| 축 | primary | 진단 이웃 |
| --- | ---: | --- |
| gap | 0.30% | 0.20%, 0.40% |
| RVOL | 1.5 | 1.3, 1.7 |
| gap retention | 0.50 | 0.40, 0.60 |
| target | 2.0R | 1.5R, 2.5R |
| time stop | 60분 | 고정(이웃 없음) |

각 이웃은 한 축만 바꾼 독립 simulation이다. 이웃 8개 중 official-cost expectancy 부호가 primary와
반대인 것이 2개 이상이면 parameter cliff 실패다. 모든 변형은 결과에 함께 공개한다.

## 10. development gate

실행 전 다음 process precondition을 모두 충족해야 한다.

- P1: 이 사전등록 merge commit 존재.
- P2: 별도 구현 commit과 사전등록↔구현 매핑 존재.
- P3: 독립 code-reviewer APPROVE + architect CLEAR.
- P4: snapshot file/content·manifest·raw 4-hash 일치.
- P5: OOS loader AST 경계 테스트 통과.

미충족은 `NOT_RUN`이며 전략 실패가 아니다.

development primary는 아래 **전부** 충족해야 OOS 단계로 갈 수 있다.

| 게이트 | 기준 |
| --- | --- |
| D1 | 거래 ≥ 536 |
| D2 | 거래일 ≥ 322 |
| D3 | official net expectancy > 0 |
| D4 | official PF ≥ 1.20 |
| D5 | official abs(MDD) ≤ 10% |
| D6 | 양수 fold ≥ 7/10(무거래 fold는 비양수) |
| D7 | stress 누적 net PnL ≥ 0 |
| D8 | 반대 부호 진단 이웃 ≤ 1 |
| D9 | major defect 0, 데이터 배제율 ≤ 5%, 운영 실패율 ≤ 20% |

하나라도 실패하면 이 전략은 `NO_SELECTION`, 규칙 수정 금지, blind OOS 미개봉이다. 이는 전체
연구계획을 즉시 종료한다는 뜻이 아니라 후보 2가 선택되지 않았다는 뜻이며 후보 3으로 이동할 수 있다.

## 11. blind OOS gate

development 전부 통과 후에만 #78의 tracked unlock artifact로 2026-01-21~2026-07-21을 **한 번**
연다. primary와 진단 이웃을 한 번 로드·한 번 계산하고 전량 공개한다. 나쁜 결과로 재실행하지 않는다.

| 게이트 | 기준 |
| --- | --- |
| O1 | official net expectancy > 0 |
| O2 | official PF ≥ 1.20 |
| O3 | 거래일 ≥ 60 |
| O4 | 거래 ≥ 100 |
| O5 | 양수 anchor-month ≥ 4/6 |
| O6 | official abs(MDD) ≤ 10% |
| O7 | stress 누적 net PnL ≥ 0 |
| O8 | 최대 단일 심볼-family 이익 기여 ≤ 25% |
| O9 | 최대 단일 거래일 이익 기여 ≤ 20% |
| O10 | 반대 부호 진단 이웃 ≤ 1 |
| O11 | major data defect 0 |

전부 통과해야 `SELECT_ONE`이다. 일부만 통과한 “덜 나쁜” 전략을 선택하지 않는다.

## 12. 결함·제외·감사

데이터 제외 사유는 `no_previous_regular_close`, `incomplete_rvol_window`,
`insufficient_rvol_lookback`, `zero_cumulative_volume`, `incomplete_decision_bar`다. threshold 미달,
retention 붕괴, pullback 없음, reclaim 없음은 **전략 필터**이며 결함 분자에 넣지 않는다.

운영 사유는 `stale_exit`, `eod_fallback`, `unclosable`이다. 데이터 제외율은
`고유 (symbol,day) 데이터 제외 / (30 × 평가 거래일)`이며 5% 초과면 `INCONCLUSIVE_DATA`.
운영 실패율은 `운영 사유 거래 / 진입 거래`이며 20% 초과면 `INCONCLUSIVE_DATA`.

snapshot hash mismatch, OHLC 불변식 위반, 새 timestamp value conflict, session/calendar 위반,
look-ahead, fill/cost 계약 위반은 invalidation-grade다. 성과 부진은 재실행 사유가 아니다.

## 13. 수정·중단 규율

- 결과 전 typo·모호성·내부 불일치만 고칠 수 있고 개정 이력을 남긴다.
- 결과 후 threshold·창·필터·entry·exit·비용·tie-break·통계·gate 수정은 금지한다.
- 결과 후 발견된 데이터/누수/체결 결함은 append-only erratum과 독립 리뷰를 요구한다.
- 성과 부진은 invalidation 사유가 아니다.
- 이 사전등록 PR에서는 전략 실행 코드·RESULT·unlock artifact를 만들지 않는다.

## 14. 다음 단계

이 문서와 JSON이 main에 병합된 뒤 별도 issue/branch에서만 구현한다. 구현은 최소한 다음을 테스트한다.

- gap/RVOL/VWAP 손계산과 causal 음성 테스트
- pullback armed와 same-bar trigger 금지
- retention floor equality/붕괴 경계
- 직전 봉 high strict reclaim과 next-bar fill
- pullback-low stop, 60분 time stop, EOD/조기폐장
- 입력 순서·중복 불변, 동시 후보 경쟁
- development가 OOS를 열 수 없는 동적/AST gate

이번 단계의 stop condition은 **사전등록 merge-ready**이며 성과 계산이 아니다.
