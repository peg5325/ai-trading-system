# 사전등록 — P0-US30 일봉 박스 돌파 + 오더블록 리테스트 v1.0

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
`box_high-1.00×ATR20_pre`, 5거래일 보유다. 오더블록 필터의 증분 가치를 비교할 뿐 승격 권한은 없다.

### Partial-exit sensitivity

Primary와 후보·진입·초기 stop이 동일하다. +1R 도달 시 50%를 청산하고 나머지 stop을 다음
거래일부터 entry로 이동한다. 같은 날 stop과 +1R이 모두 닿으면 전량 stop이 먼저 발생한 것으로
처리한다. 나머지는 primary와 같은 5번째 거래일 close에 청산한다.

## 7. fold와 판정

SPY development 2,347거래일을 기존 정본과 동일한 front-loaded remainder 방식의 10개 연속 fold로
나눈다. 각 fold는 빈 포지션으로 시작하고 entry와 exit가 모두 fold 안인 거래만 포함한다. 지표
warm-up은 fold 이전 development 데이터를 사용할 수 있다.

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
- official expectancy per initial R가 base보다 큼
- partial-exit sensitivity official expectancy `>0`

판정은 `INCONCLUSIVE_DATA`, `EXPLORATORY_NO_SELECTION`,
`EXPLORATORY_GO_REQUIRES_FRESH_VALIDATION` 중 하나다. Base와 partial 결과로 primary를 사후 교체하지
않는다.

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
