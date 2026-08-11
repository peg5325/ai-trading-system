# P0-US30 52주 고점·상대강도 돌파 사전등록 v1.0

> 상태: **결과 미열람·구현 미실행.** 이 문서와 동형 JSON을 먼저 커밋·원격 고정한 뒤에만
> 구현과 development 백테스트를 시작한다. swing blind OOS(2025-07-21~2026-07-21)는 열지 않는다.

## 1. 연구 질문과 범위

장기 상승추세 안에서 52주 고점에 근접하고, 63거래일 상대강도가 상위권이며, 처음으로 직전
20거래일 고가를 종가 돌파한 종목은 다음 3~5거래일에도 상승을 지속하는가? 이를 P0-US30 중
비레버리지·비인버스 26종목의 long-only 포트폴리오로 검증한다.

- 전략 id: `p0_us30_high52_rs_breakout_v1`
- Primary: 다음 거래일 시가 진입, **5번째 보유 거래일 종가** time exit
- Sensitivity: 다음 거래일 시가 진입, **3번째 보유 거래일 종가** time exit
- naked short, scale-in, 물타기, 결과 기반 임계 변경은 금지한다.
- 고정 P0 표본의 development 판정이며 시장 전체 일반화를 주장하지 않는다.
- 52주 고점·모멘텀의 학술 근거는 주로 월 단위다. 5일 보유는 그 결론의 복제가 아니라 별도
  단기 지속 가설이다.

배경 근거:

- George & Hwang (2004), https://doi.org/10.1111/j.1540-6261.2004.00695.x
- Jegadeesh & Titman (1993), https://doi.org/10.1111/j.1540-6261.1993.tb04702.x
- Moskowitz, Ooi & Pedersen (2012), https://doi.org/10.1016/j.jfineco.2011.11.003

## 2. 데이터 identity와 시간 경계

- 정본: `config/universe_p0_us30.yaml` v1
- 데이터 증거: `docs/research/P0_US30_DAILY_10Y_MANIFEST.json`
- 고정 일봉 지문: `756e5bd7cd4cc393dc18f88792a2f56b83a02ca45f28afb3633b139abac07772`
- interval/market: `1d` / `US`
- development: **2016-03-18~2025-07-18**(inclusive)
- blind OOS: **2025-07-21~2026-07-21**(inclusive, 미개봉)
- 성과 로드는 `research_load(..., track=TRACK_SWING, purpose="performance")`를 통하며 end는
  `2025-07-21T00:00:00 ET`보다 앞선 반개구간으로 고정한다. OOS grant를 전달·생성하지 않는다.

공식 유니버스는 `class in {leveraged_etf,inverse_etf}`인 `TQQQ,SQQQ,SOXL,SOXS`를 제외한
26종목이다. PLTR·VST 등은 실제 가용 구간에서만 횡단면 모집단에 참여한다.

거래일 달력은 development 구간의 SPY 완성 일봉 날짜다. 신호일부터 time exit까지 필요한 모든
시장 거래일에 해당 종목 일봉이 있어야 한다. 결측·비정상 OHLC는 보간하지 않고 후보 제외 및 DQ1에
집계한다.

## 3. 지표와 인과성

모든 지표는 신호일 `t`의 완성 일봉까지만 사용한다.

```text
TR_t = max(high_t-low_t, abs(high_t-close_{t-1}), abs(low_t-close_{t-1}))
ATR14_t = 최근 14개 TR 산술평균(현재 t 포함)
SMA200_t = 최근 200개 close 산술평균(현재 t 포함)
high252_t = 최근 252개 high 최대값(현재 t 포함)
high52_proximity_t = close_t / high252_t
return63_t = close_t / close_{t-63} - 1
prior20_high_t = max(high_{t-20}, ..., high_{t-1})
breakout_strength_t = close_t / prior20_high_t - 1
```

`first_breakout_t`는 다음 두 조건을 모두 만족한다.

1. `close_t > prior20_high_t` (strict)
2. `close_{t-1} <= max(high_{t-21}, ..., high_{t-2})`

따라서 연속 신고가 매일을 새 독립 신호로 세지 않고, 20일 범위 밖에서 범위 위로 전환된 첫날만
신호로 인정한다.

## 4. 횡단면 상대강도와 후보

각 SPY 거래일에 유효 OHLC와 `return63`이 있는 공식 종목을 횡단면 모집단으로 삼는다. 모집단이
20종목 미만이면 그 날은 후보를 만들지 않는다.

- 정렬: `return63` 내림차순 → symbol 오름차순
- 상위 집합 크기: `ceil(0.20 × 횡단면 종목 수)`
- 수익률 동점은 위 symbol tie-break로 결정하며 분수 보유는 사용하지 않는다.

상위 집합 중 다음을 모두 충족한 종목만 최종 후보다.

1. `ATR14_t > 0`, `SMA200_t > 0`, `high252_t > 0`
2. `close_t > SMA200_t` (strict)
3. `high52_proximity_t >= 0.97`
4. `first_breakout_t = true`

같은 진입일 후보는 `return63` 내림차순 → `breakout_strength` 내림차순 →
`high52_proximity` 내림차순 → symbol 오름차순으로 진입 우선순위를 정한다. 신호일 종가 이후에만
확정되며 다음 거래일 시가 전 체결은 금지한다.

## 5. 포트폴리오·체결·청산

- 초기자본: 100,000 USD
- 최대 동시 포지션: 5
- 종목당 최대 entry notional: 10,000 USD
- 종목당 위험예산: 500 USD
- 동일 종목 동시 중복 포지션 금지
- 수량: `floor(min(10000/entry_open, 500/(1.5*ATR14_signal)))`; 1주 미만 skip
- entry: 신호일 다음 SPY 거래일의 해당 종목 시가
- hard stop: `entry_open - 1.5*ATR14_signal`
- 각 보유일 `open <= stop`이면 open, 아니면 `low <= stop`이면 stop에 청산
- Primary time exit: entry day를 1일로 세어 5번째 시장 거래일 종가
- Sensitivity time exit: 3번째 시장 거래일 종가
- time-exit일에도 stop을 먼저 판정한다.
- 매일 기존 포지션 gap-stop → 신규 진입 → intraday stop → time exit 순서다.
- target, trailing stop, pyramiding, re-entry 보정은 없다. 포지션 청산 후 새로 발생한 독립
  `first_breakout` 신호는 다시 거래할 수 있다.

## 6. 비용과 손익

왕복 비용을 entry notional에 각 거래당 한 번 차감한다.

| 수준 | 왕복 비용 |
|---|---:|
| low | 0.14%p |
| official | 0.34%p |
| stress | 0.68%p |

```text
gross_pnl = quantity × (exit_price-entry_price)
cost = entry_price × quantity × round_trip_cost_pct / 100
net_pnl = gross_pnl - cost
```

비용 수준 간 신호·수량·체결가격은 동일하다.

## 7. 시간 블록과 보고

SPY development 거래일을 가능한 균등한 10개 연속 블록으로 나눈다. 각 fold는 독립 초기자본으로
재실행하며, 신호 계산은 fold 이전 development 데이터를 사용할 수 있지만 진입일과 전체 보유 경로가
fold 안에 있는 거래만 포함한다. random split은 금지한다.

Primary와 sensitivity 각각 다음을 보고한다.

- gross/low/official/stress: 거래수, 누적손익, 거래당 expectancy, PF, 승률
- official: 보유 중 종가 mark-to-market 일별 equity, MDD, 최대 연속손실, 양수 fold 수
- 종목별·진입일별 공식 순손익 기여도
- 평균/중앙 보유 거래일, stop/time-exit 사유 수
- 횡단면 가능일, qualifying 신호, 경계·데이터·포트폴리오 제외 수와 거래 digest

## 8. 개발 판정 게이트

데이터 선행 게이트:

| gate | 조건 |
|---|---|
| DQ1 | 필요한 entry/holding 일봉 누락·비정상 후보 / 전체 예약 후보 `<=5%` |
| DQ2 | manifest hash·유니버스·development/OOS 경계 일치 |
| DQ3 | OOS 접근 0, `blind_oos_opened=false` |

DQ 실패는 `INCONCLUSIVE_DATA`다.

Primary 경제·강건성 게이트는 아래를 전부 통과해야 한다.

| gate | 조건 |
|---|---|
| D1 | 거래 `>=300` |
| D2 | official net expectancy `>0` |
| D3 | official PF `>=1.20` |
| D4 | stress 누적 net PnL `>=0` |
| D5 | 10개 fold 중 official net PnL 양수 `>=7` |
| D6 | official MDD 절댓값 `<=10%` |
| D7 | official 총 net `>0`, 최대 종목 기여 `<=25%`, 최대 진입일 기여 `<=20%` |
| D8 | 3일 sensitivity official expectancy `>0`이고 primary와 부호 일치 |

- DQ 실패: `INCONCLUSIVE_DATA`
- DQ 통과 후 D1~D8 중 하나라도 실패: `NO_SELECTION`
- 전부 통과: `DEVELOPMENT_GO` — OOS·Paper·실거래 승인이 아님

## 9. 필수 회귀 테스트

1. ATR14·SMA200·252일 high·63일 수익률 hand calculation과 warm-up
2. prior20 high가 현재일을 제외하고 strict 돌파하는지
3. `first_breakout`이 연속 신고가를 중복 신호로 만들지 않는지
4. 0.97 proximity·SMA strict·횡단면 최소 20 경계
5. 상위 `ceil(20%)`·동점 symbol tie-break·입력 순서 불변
6. 다음 시가 진입, 5일/3일 거래일 청산, 주말·휴일 경계
7. 수량·최대 5포지션·동일종목 중복 금지
8. gap stop·intraday stop·time exit 우선순위
9. 누락 보유봉 fail-closed와 제외율 5% 경계
10. 비용 한 번 차감·비용 수준 간 동일 gross trades
11. 10개 연속 fold와 경계 거래 미혼입
12. mark-to-market MDD·연속손실·기여도
13. OOS grant 부재·TRACK_SWING 좁은 정적 허용·반개구간
14. JSON·Markdown·trade digest 결정론

## 10. 변경 규율

임계·비용·표본·체결·판정은 결과 확인 후 수정하지 않는다. look-ahead, OOS 혼입, 잘못된 데이터
identity 또는 손익·체결 계약 결함만 invalidation-grade다. 성과 부진은 재실행 사유가 아니다.
