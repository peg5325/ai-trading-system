# 분석 — 과거 데이트레이딩 시도 실패 원인과 Toss 1분봉 품질

> 작성: 2026-10-01 · 성격: **informational / read-only 분석**<br>
> 과거 판정(`NO_SELECTION`, `INCONCLUSIVE_DATA`, `NO_GROSS_EDGE`, `NULL`)을 재판정하지 않는다.
> 이 문서는 새 데이트레이딩 탐색 계획의 후보·데이터 경계를 고르기 위한 입력이다.
> 진행 중인 `premarket_freeze_long_intraday_v1` Paper(2026-10-01~10-14)와 무관하며 그 계약·설정을 바꾸지 않는다.

## 1. 결론 요약

1. **이 universe에서 비용 전 엣지가 비용에 닿은 데이 전략이 없다.** P0-US30(초대형주·지수/섹터/레버리지 ETF 30종)
   2023-05-18~2026-01-20 development 구간에서 거래당 gross는 −0.10%~+0.12%, 대부분 +0.01~+0.06%였다.
   Toss 실제 왕복 수수료 0.20%의 0.05~0.31배다. 같은 universe에서 거래를 늘리는 방향으로는 닿지 않는다.
2. **데이터로 판정 불가였던 ORB·Gap+RVOL도 경제적으로 살아날 가능성은 낮다.** 역산 gross는 각각
   +0.032%, −0.095%/거래다(재판정 아님, 참고용).
3. **D9 실패는 대부분 설계·일회성 장애 문제였다.** 결손의 약 절반이 2024-07-25~08-02 Toss 수집 공백과
   그 20일 lookback 전파에서 나왔고, 나머지는 얇은 섹터 ETF 7종과 2022~23년 VST다.
   핵심 20종목은 개별적으로 모두 D9 ≤ 5%이고, 2024-09-03 이후에는 0.26% 수준이다.
4. **한 번도 시도하지 않은 계열이 남아 있다.** 계획만 하고 사전등록하지 않은 VWAP/ATR 극단 회귀(후보 3),
   P0 밖 "in-play" 고변동 종목, 실적·이벤트일 셋업 등. 1의 크기 문제를 감안하면
   **거래당 움직임이 큰 셋업**이어야 한다.

## 2. 공통 조건 (US30 intraday 시도)

- snapshot: `data/research_snapshots/p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite`
- development 평가 2023-05-18~2026-01-20(670일), blind OOS 2026-01-21~2026-07-21(125일). **어떤 시도도 OOS를 열지 않았다.**
- 비용 0.14/0.34/0.68 %p 왕복, 공식 판정 0.34. gross-edge screen만 0.204 floor 추가.
- 공통 게이트: D3 기대값 > 0, D4 PF ≥ 1.2, D5 MDD ≤ 10%, D6 양수 fold ≥ 7/10, D7 stress 순손익 ≥ 0, D9 결손 ≤ 5%.

**역산 방법.** 결과 파일은 같은 거래에 세 비용을 적용한 순손익을 보고한다. 순손익 = gross − 비용% × 명목합이므로
low·official 값으로 명목합과 gross를 구했고, 이 값으로 stress 순손익을 다시 예측하면 7개 전략 모두 보고값과 $0.1 이내로 일치했다.
아래 "gross%/거래"는 이 문서의 역산값이며 프로젝트가 보고한 수치가 아니다.

## 3. 시도별 실패 원인

| # | 시도 | 판정 · 실패 게이트(관측값) | gross%/거래(역산) | ÷0.20 | 원인 분류 |
|---|---|---|---|---|---|
| 1 | ORB (`RESULT_P0_US30_ORB_DEVELOPMENT.md`) | `INCONCLUSIVE_DATA`; D3 −56.76$/거래, PF 0.605, D6 1/10, D9 6.35% | +0.032 | 0.16 | DATA(판정). 아래는 COST+약한 SIGNAL — 0.14 비용에서도 음수 |
| 2 | Gap+RVOL (`RESULT_P0_US30_GAP_RVOL_DEVELOPMENT.md`) | `INCONCLUSIVE_DATA`; PF 0.278, D6 0/10, D9 6.4478%; 진단 9 spec 전부 PF 0.26~0.29 | −0.095 | <0 | DATA(판정). 아래는 SIGNAL(비용 전 음수) |
| 3 | Gross-edge screen 8가설 (`RESULT_P0_US30_GROSS_EDGE_SCREEN.md`) | `NO_GROSS_EDGE`; 최고 first_hour_continuation +0.062890%p, 9/10 fold, Bonferroni p 0.0504 | +0.063 | 0.31 | SIGNAL. PROCESS: OOS identity erratum |
| 4 | Gap-down fade (`RESULT_P0_US30_GAP_DOWN_FADE_DEVELOPMENT.md`) | `NO_SELECTION`; D1 26건(≥300 필요), D2, D3, D4 0.721, D6 3/10, D7 | +0.119 | 0.60 | SAMPLE(n=26) + COST |
| 5 | Afternoon continuation | `NO_SELECTION`; 110건, PF 0.398, D6 0/10; D9 4.79% 통과 | +0.035 | 0.17 | SAMPLE + COST/약한 SIGNAL |
| 6 | SPY intraday momentum | `NO_SELECTION`; PF 0.024, D6 0/10, 롱·숏 모두 음수 | −0.008 | ≈0 | SIGNAL |
| 7 | Box+RSI+거래량 평균회귀 5m | `NO_SELECTION`; 92건, official 비용에서 PF 0.000(목표 도달 50/92건도 비용 후 전부 손실) | +0.011 | 0.05 | 설계상 목표폭 < 비용 + SAMPLE |
| 8 | DEMA60+Stoch 5m | `NO_SELECTION`; 3,715건, PF 0.358, D6 0/10 | +0.019 | 0.10 | SIGNAL + COST. PROCESS: erratum |
| 9 | H4 스크리너 랭킹 (`RESULT_screener_ranking_h4.md`) | `NULL` 0/9; top−random −0.056~+0.056%p | ≤ +0.05 | ≤0.26 | SIGNAL + SAMPLE(94일) |

참고: close-pressure reversal은 일봉·3일 보유 스윙이다. 그 D8 감도(익일 시가→종가) arm은 4,086건,
gross PF 1.089, 역산 +0.058%/거래로 비용 전에도 PF 게이트 1.2에 못 미친다.

## 4. 시도 지형

- **이 universe·구간에서 사실상 소진:** 시간대 방향 모멘텀/반전(8 screen, SPY 마지막 30분, 오후 지속),
  단순 갭 지속·반전, 대형 갭다운 VWAP fade, 5m 박스 평균회귀, 5m DEMA/Stoch, 점수 랭킹.
- **판정 불가(DATA):** ORB, Gap+RVOL.
- **한 번도 시도 안 함:**
  - VWAP/ATR 극단 회귀(후보 3). `RESULT_ONE_MONTH_STRATEGY_SELECTION_CLOSEOUT.json`의
    `vwap_atr_candidate_started: false`. Gap+RVOL D9 실패로 다음 후보 이동이 막혀 멈췄다. 회전율·비용 부담이 가장 큰 후보로 지적됐었다.
  - P0 30종 밖 universe(시점 기준 고RVOL "in-play" 종목, 중소형주). 공개 연구의 ORB/갭 엣지는 이쪽에 몰려 있다.
  - 실적·뉴스·매크로 이벤트일 셋업(촉매 데이터 없음).
  - 장전·시간외 신호 기반 진입, 횡단면 상대강도, 레버리지 ETF vs 기초지수 스프레드.

**크기 문제.** 0.20% 왕복을 넘으려면 지금까지 관측된 gross의 대략 3~10배가 필요하다.
0.51 stress까지 버티려면 더 크다. 초대형주에서 거래 수를 늘리는 방향은 맞지 않고,
넓은 목표폭, 고변동 종목, 이벤트일처럼 **거래당 움직임이 큰 셋업**이어야 한다.

## 5. Toss 1분봉 품질 (D9)

### 5.1 D9 정의 (코드)

- 단위: 결손 사유가 하나 이상인 symbol-day. 분모는 universe 종목 수 × 평가 거래일 = 30 × 670 = 20,100.
  **전략 이벤트가 아니라 전체 symbol-day 기준**이다. 전략 필터 미충족은 결손이 아니다.
- Gap+RVOL: `src/research/gap_rvol/features.py:16-22`(사유), `:65-107`(5m 연속성), `gates.py:54-81,128-139`(D9), 한도 `PREREG_P0_US30_GAP_RVOL_V1.json` `data_exclusion_ratio_lte` 0.05.
  - 사유: `no_previous_regular_close`, `incomplete_rvol_window`(09:30~09:44 결측),
    `insufficient_rvol_lookback`(직전 20일 중 15일 미만 완전), `zero_cumulative_volume`, `incomplete_decision_bar`.
  - `incomplete_decision_bar`의 연속성 검사는 그날 모든 결정 봉에 대해 돈다. 그래서 정규장 중간에 1분만 비어도 그날이 제외된다.
    사실상 **정규장 전체 완전성 검사**다.
- ORB: `src/research/orb/features.py:26-31`, `gates.py:125-198`, 한도 `PREREG_P0_US30_ORB_V1.json` escalation_ratio 0.05.
- 저장소 코드로 재현: Gap+RVOL 1,296/20,100 = 6.4478%, ORB 1,277/20,100 = 6.3532%로 공식값과 일치.
  사유 단위 coverage-only 복제본도 동일 symbol-day·동일 사유로 차이 0건.

### 5.2 종목별 결손률

- **Off**: 공식 Gap+RVOL dev 구간(n/670).
- **≥24-09**: 2024-09-03~2026-07-21(471일).
- **Compl**: 정규장 분봉이 모두 있는 세션 비율.

| 그룹 | 종목 | Off | ≥24-09 | Compl |
|---|---|---|---|---|
| ≤2% | PLTR, AMD | 0.75%, 1.04% | 0.42%, 0.21% | .99+ |
| 핵심(≤5%) | SOXL, SOXS, META, TQQQ, TSLA, AMZN, GOOGL, NVDA, SQQQ, AAPL, MSFT, IWM, CRM, MU, SPY, QQQ, NFLX, AVGO | 3.13~3.58% | 0~0.42% | .97~.99 |
| 경계 | DIA, SMH, XLE | 5.37%, 6.12%, 7.46% | 1.49%, 2.97%, 0.64% | .95~.97 |
| 구조적 실패 | XLU, XLV, XLI, XLK, XLF, XLY | 13.0~15.4% | 5.7~14.7% | .90~.93 |
| 체제 변화 | VST | 26.9% | 1.06% | .77 |

- 핵심 20종 합산 dev 구간: 423/13,400 = 3.16%.
- **시간 집중:** 월별 전체 결손률은 2024-06 21%, 2024-07 40%, 2024-08 73%이고, 그 밖은 약 0.5~5%다.
  - 2024-07-25~08-02에 19~30종이 하루 0봉이었다. 데이터가 돌아온 뒤에도 20일 lookback 때문에 08-23까지 제외가 전파됐다.
  - 이 장애와 전파가 공식 1,296건의 약 절반이다.
  - 그 밖의 단발성 장애일: 2022-04-13, 2023-01-24, 2025-01-27.
- **깨끗한 구간:** 2024-09-03~2026-01-20(346 거래일, 약 1.4년, intraday blind OOS 시작 전)에서
  - 핵심 20종: Gap 0.26%, ORB 0.03%
  - 30종 전체: Gap 1.60%
  - 장애 전 2023-05~2024-05도 핵심 20종은 연 0.4% 수준이다. 장애 구간을 통째로 빼는 설계라면 약 2.7년을 쓸 수 있다.
- **섹터 ETF(XL\*)** 는 장애와 무관하게 흩어진 분 결측으로 실패한다. Toss가 체결 없는 분을 내보내지 않는 것으로 추정되며, 검증하지 않았다.
- **설계 교훈:** gap-down fade는 직전 종가를 일봉에서 가져와 해당 사유 결손이 0이었다.
  직전 종가·lookback을 일봉에서 가져오고, 결정에 필요한 구간만 완전성을 요구하면 D9 부담이 크게 준다.
  이 선택은 결과를 보기 전 사전등록에서 고정해야 한다.

### 5.3 지수 ETF와 live 수집 현황

- `market_history.sqlite`의 US 1분봉은 이 30종(+ SOXX 2026-05-18~07-20)뿐이다. VOO·SPXL 등은 없다.
  - SPY는 2023-04-03, QQQ는 2023-04-11부터 있다.
  - SPY·QQQ·IWM·SMH·AVGO·CRM·NFLX는 backfill cursor `exhausted=0`이라 더 과거까지 수집될 여지가 있다.
- **2026-07-22~09-24 US 1분봉이 전혀 없다.**
  - 2026-09-25부터 post-close job이 REGULAR 세션만, 하루 4~5종목만 수집한다(`max_pages` 제한, 첫날은 장중부터 잘림).
  - 완전한 390봉일은 SPY 3일, 그 밖 소수 종목의 1~2일뿐이다.
  - 30종 forward 검증이나 장전·갭 특성용 live 데이터 기반은 아직 없다.

## 6. 다음 탐색 계획에 주는 함의 (제안)

1. **데이터 경계:** Toss 단독으로 핵심 20종 + development 2024-09-03 이후(또는 장애 구간만 통째 제외)를 쓰면 D9 ≤ 5%를 여유 있게 만족한다.
   섹터 ETF 7종과 VST는 제외한다. 외부 원천 없이 가능하다.
2. **후보 선택 기준:** 거래당 기대 gross가 0.20% 왕복의 수 배가 될 수 있는 셋업만 1차 선별에 올린다.
   예: VWAP/ATR 극단 회귀(후보 3, 목표폭을 ATR로 키움), 레버리지 ETF(TQQQ/SOXL 등)의 큰 갭·추세일, 변동성 상위일 한정 진입.
3. **OOS 경계 재검토:** 기존 intraday blind OOS(2026-01-21~07-21)는 아직 봉인되어 있다. 그대로 쓸지,
   새 미래 구간을 쓸지는 새 계획에서 결과를 보기 전에 고정한다.
4. **수집 보강:** 07-22~09-24 공백 복구와 핵심 20종 일일 완전 수집이 없으면 새 전략의 forward/Paper 검증 기반이 없다.
   이것은 전략 탐색과 독립적으로 먼저 할 수 있는 작업이다.

## 7. 불확실성과 접촉 범위

- 역산 gross%는 비용이 체결 명목에 비례 부과된다는 가정에 기반한다. stress 값 교차검증으로 지지된다.
- 2021-12~2022 NYSE 휴장일은 저장소 달력이 2023부터라 수작업으로 넣었다(전체 기간 복제본에만 해당).
- blind OOS 구간(2026-01-21~07-21)은 봉 **존재 여부(종목·시각·세션)**로만 접촉했다.
  가격·수익률은 읽지 않았으며, 이는 `src/history/oos_guard.py`가 허용하는 coverage 용도다.
- live `market_history.sqlite`는 읽기 전용으로 짧은 조회만 했고, 고정 snapshot은 `immutable=1`로 열었다.
- 분석 스크립트·중간 산출물은 세션 임시 디렉터리에만 두었고 저장소에 커밋하지 않았다(재현 필요 시 위 코드 경로로 다시 계산).
