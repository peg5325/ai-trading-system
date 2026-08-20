# 사전등록 — P0-US30 일봉 박스 돌파 + 오더블록 리테스트 v1.1

상태: **FROZEN BEFORE THIS STRATEGY RESULTS** · 이슈 #140

이 전략은 사용자 요청과 영상 아이디어를 정량화한 별도 탐색 가설이다. 영상의 오더블록을 그대로
복제한다고 주장하지 않으며, 기존 박스·돌파 결과를 본 뒤 만든 적응적 가설이므로 같은 development
결과는 exploratory-only다. 어떤 양성 결과도 현재 blind OOS·Paper·live 승격을 승인하지 않는다.

## 1. 연구 질문

20거래일 박스에서 변동성이 축소된 뒤 추세·거래량을 동반해 상단을 돌파하고, 직전 bearish body
오더블록을 3거래일 안에 재확인한 종목은 다음 5거래일 동안 비용 후 양의 수익을 보이는가?

Primary는 `order_block_retest_fixed5` 하나다. 즉시 박스 돌파와 영상식 절반익절은 고정 진단 arm이며
primary를 대신해 승격할 수 없다.

## 2. 데이터와 범위

- P0-US30에서 레버리지·인버스 ETF를 제외한 26종목, long-only
- canonical 1d, SPY 거래일 달력
- full manifest content: `756e5bd7cd4cc393dc18f88792a2f56b83a02ca45f28afb3633b139abac07772`
- development content(2025-07-18까지):
  `96151eeb277528e1b8167e7c8c531155640cd52973c0c2f646bd23ed11018a7a`
- development: 2016-03-18~2025-07-18 inclusive
- blind OOS: 2025-07-21~2026-07-21 inclusive, 잠금 유지
- full identity는 추적 manifest receipt, live DB hash는 development 종료일까지 검증한다.
- 성과 로드는 `research_load(... purpose="performance", track=TRACK_SWING)`만 사용한다.

## 3. 박스와 돌파

신호일을 `t`라 한다.

- 박스: `t-20~t-1`의 high/low
- `box_width / close[t-1] <= 12%`
- ATR5와 ATR20은 `t-1`에서 끝나며 `ATR5 <= 0.80 × ATR20`
- `SPY close_t > SMA200_t`
- 종목 `close_t > EMA50_t`, 신호봉 양봉
- `box_high + 0.10×ATR20_pre < close_t <= box_high + 1.00×ATR20_pre`
- `volume_t >= 1.5 × median(volume[t-20:t-1])`
- volume이 0·결측이면 not-ready

동시 후보 순위는 `돌파폭/ATR` 내림차순 → volume ratio 내림차순 → symbol 오름차순이다.

지표 계산 관례:

- 유효 OHLC: 전 가격 양수, `high>=max(open,close)`, `low<=min(open,close)`, `high>low`
- TR: `max(high-low, |high-prev_close|, |low-prev_close|)`
- ATR5/20: TR의 단순 산술평균이며 신호일을 제외한 `t-5~t-1`, `t-20~t-1`
- EMA50: `alpha=2/51`, 첫 유효 close seed, 50개 연속 유효봉 전 not-ready, `close_t` 포함
- SPY SMA200: `t`까지 200개 연속 유효 close의 산술평균
- volume median20: 고정 `t-20~t-1` 20개 volume의 통상 중앙값(짝수는 가운데 둘 평균). 20개가
  모두 양수·비결측이어야 하며 결측을 건너뛰어 `t-20` 이전까지 거슬러 올라가지 않는다.
- 결측·비정상 일봉은 segment를 끊고 rolling/EMA 상태를 모두 초기화한다. 거래소 휴일은 SPY
  거래일 연속성에 포함되므로 segment를 끊지 않는다.

## 4. 오더블록과 리테스트

영상 개념을 다음처럼 기계 정의한다.

- `t-1`은 bearish: `close[t-1] < open[t-1]`
- 신호봉 bullish body가 직전 body를 감쌈:
  `open_t <= close[t-1]` 및 `close_t >= open[t-1]`
- zone: `[close[t-1], open[t-1]]`

돌파 다음 3거래일에서 최초로 아래를 모두 만족하는 `r`을 찾는다.

- `low_r <= zone_high`, `high_r >= zone_low`
- `close_r >= box_high`
- `close_r > open_r`

확정 리테스트 전 종가가 zone_low 아래면 후보를 폐기한다. 최초 리테스트 다음 거래일 open에 진입하고
초기 stop은 `zone_low - 0.10×ATR20_pre`다.

## 5. 체결·보유·비용

- entry day를 보유 1일째로 세고 5번째 거래일 close에 time exit
- entry/holding/exit 봉 결측·비정상은 후보 제외
- open이 stop 이하면 open 체결, 아니면 low가 stop 이하일 때 stop 체결
- stop은 time exit보다 우선하며 entry day부터 유효
- 초기자본 USD 100,000, 최대 5포지션
- 거래당 최대 USD 10,000, 초기 위험예산 USD 500
- `qty=floor(min(10000/entry,500/(entry-stop)))`; 1주 미만이면 skip
- 물타기·추가매수·scale-in 없음
- 왕복 비용 0.14/0.34/0.68%p, 실제 entry/exit notional에 절반씩 적용

## 6. 고정 arm

### Base diagnostic

박스·추세·거래량·변동성 축소 돌파까지만 요구하고 `t+1 open` 진입, stop은
`box_high-1.00×ATR20_pre`, 5거래일 보유다. 표본·진입 시각·stop이 primary와 다르므로 오더블록의
인과적 증분가치를 증명하는 gate로 사용하지 않는다. 넓은 맥락의 진단 수치일 뿐 승격 권한은 없다.

### Partial-exit sensitivity

Primary와 후보·진입·초기 stop이 동일하다. target은 `entry+initial_R`이고 +1R 도달 시 50%를
고정 target 가격에 청산한다. 유리한 gap open 가격을 사용하지 않는다. 나머지 stop은 다음
거래일부터 entry로 이동한다.

일별 우선순위는 `현재 stop gap → stop/target 동시 접촉 시 전량 stop → intraday stop → open/high
target 절반청산 → time exit`다. `low<=stop`이고 `max(open,high)>=target`이면 open이 target 이상이어도
target은 없었던 것으로 하고 전량 stop 처리한다. stop에 닿지 않은 경우에만 open 또는 high가 target
이상이면 절반을 target 고정 가격에 청산한다. BE 활성일 open이 entry 이하면 open에, 아니면 low가
entry 이하일 때 entry에 청산한다.
5번째 날 target만 닿으면 절반을 target에, 나머지를 같은 날 close에 청산한다. 같은 날 stop과
target이 모두 닿으면 시가 위치와 무관하게 target은 없었던 것으로 하고 전량 stop 처리한다.

## 7. fold와 판정

SPY development 2,347거래일을 기존 정본과 동일한 front-loaded remainder 방식의 10개 연속 fold로
나눈다. 폭은 `[235,235,235,235,235,235,235,234,234,234]`이며 경계는 다음과 같다.

| fold | start | end |
|---:|---|---|
| 1 | 2016-03-18 | 2017-02-22 |
| 2 | 2017-02-23 | 2018-01-29 |
| 3 | 2018-01-30 | 2019-01-04 |
| 4 | 2019-01-07 | 2019-12-10 |
| 5 | 2019-12-11 | 2020-11-13 |
| 6 | 2020-11-16 | 2021-10-21 |
| 7 | 2021-10-22 | 2022-09-28 |
| 8 | 2022-09-29 | 2023-09-05 |
| 9 | 2023-09-06 | 2024-08-09 |
| 10 | 2024-08-12 | 2025-07-18 |

각 fold는 빈 포지션으로 시작하고 entry와 exit가 모두 fold 안인 거래만 포함한다. 지표 warm-up은
fold 이전 development 데이터를 사용할 수 있다.

DQ 분류는 다음과 같다.

- warmup과 development/fold boundary skip은 별도 집계하며 분모에서 제외
- breakout·order-block·retest 부재와 pre-entry invalidation은 전략 필터이며 분모에서 제외
- `prepared_candidate`: portfolio 배정 전까지 신호·체결 전제조건을 만족한 경로
- `data_exclusion`: 준비된 경로에 필요한 decision/entry/holding/exit 봉이 결측·비정상
- capacity·중복 symbol·quantity<1은 portfolio skip이며 분모에서 제외
- `data_exclusion_rate = data_exclusions / (prepared_candidates + data_exclusions)`, 5% 포함 통과
- primary와 base의 제외율을 각각 계산하고, partial은 primary 후보를 공유한다. DQ gate에는
  `max(primary_rate, base_rate)`를 사용한다.

데이터 게이트:

- 제외율 `<=5%`, 신규 conflict 0, major defect 0
- full receipt/development hash·구간 일치
- 교정 실행의 blind OOS 접근 0

Primary는 다음을 모두 통과해야 한다.

- 거래 `>=100`, 거래일 `>=75`
- official expectancy `>0`, PF `>=1.20`
- stress 누적 net `>=0`
- 양수 fold `>=7/10`
- official MDD 절댓값 `<=10%`
- official 순손익이 양수일 때 최대 종목 기여 `<=35%`, 최대 진입일 기여 `<=20%`
- partial-exit sensitivity official expectancy `>0`

판정은 `INCONCLUSIVE_DATA`, `EXPLORATORY_NO_SELECTION`,
`EXPLORATORY_GO_REQUIRES_FRESH_VALIDATION` 중 하나다. Base와 partial 결과로 primary를 사후 교체하지
않으며 base는 primary의 증분가치 gate가 아니다.

## 8. 결과 전 필수 테스트

1. 26종목 exact universe와 full/development identity
2. 20일 박스가 t를 제외하고 t-20~t-1만 사용
3. ATR5/20이 t-1에서 끝나고 width·contraction 경계가 inclusive
4. SPY SMA200·EMA50·breakout·extension·volume strict 경계
5. bearish source와 bullish body engulf exact 조건
6. 최초 3일 리테스트, pre-entry invalidation, 다음 open 진입
7. base/primary/partial 후보와 체결 차이
8. gap/intraday stop, time exit, same-day worst-case partial ordering
9. 5일 보유와 fold entry/exit 포함
10. risk-budget quantity·max 5·동시 순위
11. 비용·부분청산 turnover·expectancy/R
12. DQ 제외율·conflict·major defect
13. 10-fold·PF·MDD·기여도·arm 비교
14. JSON/Markdown/trade digest 결정론
15. OOS grant 부재와 all-pass fresh-validation 차단

## 9. 변경 규율

결과 후 box 길이·폭·ATR·추세·거래량·engulfing·retest·stop·보유일·arm·비용·게이트를 바꾸지 않는다.
데이터·look-ahead·체결·손익 무효화 결함만 append-only ERRATUM으로 교정할 수 있다.

## 개정 이력

- **v1.1 — 구현·성과 실행 전:** ATR/EMA/SMA/median 준비·산식, 결측 reset, arm별 DQ 합성
  규칙을 명시했다. 결과를 보지 않은 구현 재량 제거이며 GO를 쉽게 하는 변경은 없다.
