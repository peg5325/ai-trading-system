# P0-US30 장 마감 과잉압력 반전 사전등록 v1.0

> 상태: **결과 미열람·구현 미실행.** 이 문서와 동형 JSON을 먼저 커밋한 뒤에만 구현·development
> 백테스트를 시작한다. swing blind OOS(2025-07-21~2026-07-21)는 열지 않는다.

## 1. 연구 질문과 범위

완성 일봉에서 종가가 당일 저가 부근에 있고 당일 수익률도 음수인 종목은 일시적인 장 마감
매도압력의 일부를 다음 1~3거래일에 되돌리는가? 이 가설을 P0-US30 중 비레버리지·비인버스
26종목의 long-only 포트폴리오로 검증한다.

- 전략 id: `p0_us30_close_pressure_reversal_v1`
- Primary: 다음 거래일 시가 진입, **3번째 거래일 종가** time exit
- Sensitivity: 다음 거래일 시가 진입, **당일 종가** time exit
- naked short, scale-in, 물타기, 결과 기반 임계 변경은 금지한다.
- 이 검증은 현재 고정 P0 표본의 development 판정이며 시장 전체 일반화를 주장하지 않는다.

근거는 다음 세 연구의 공통 부분인 단기 가격압력 반전이다.

- https://doi.org/10.1016/j.econlet.2012.10.023
- https://doi.org/10.1016/j.jfineco.2021.04.029
- https://doi.org/10.1093/rfs/hhs066

## 2. 데이터 identity와 시간 경계

- 정본: `config/universe_p0_us30.yaml` v1
- 데이터 증거: `docs/research/P0_US30_DAILY_10Y_MANIFEST.json`
- 고정 일봉 지문: `756e5bd7cd4cc393dc18f88792a2f56b83a02ca45f28afb3633b139abac07772`
- interval/market: `1d` / `US`
- development: **2016-03-18~2025-07-18**(inclusive)
- blind OOS: **2025-07-21~2026-07-21**(inclusive, 미개봉)
- 성과 로드는 `research_load(..., track="swing", purpose="performance")`를 통하며 end는
  `2025-07-21T00:00:00 ET`로 고정한다. OOS grant를 받거나 생성하지 않는다.

Primary 공식 유니버스는 P0-US30에서 `class in {leveraged_etf,inverse_etf}`인
`TQQQ,SQQQ,SOXL,SOXS`를 제외한 26종목이다. PLTR·VST는 실제 상장 이후 가용 구간에서만 참여한다.

거래일 달력은 development 구간의 SPY 완성 일봉 날짜를 사용한다. 신호일부터 time exit까지 필요한
모든 시장 거래일에 해당 종목 일봉이 있어야 한다. 누락·비정상 OHLC는 해당 후보를 제외하고 DQ1에
집계한다. 데이터 결함을 직전/직후 봉으로 보간하지 않는다.

## 3. 지표와 신호

모든 지표는 신호일 `t`의 완성 일봉까지만 사용한다.

```text
TR_t = max(high_t-low_t, abs(high_t-close_{t-1}), abs(low_t-close_{t-1}))
ATR14_t = 최근 14개 TR의 산술평균(현재 t 포함)
day_return_t = close_t/open_t - 1
close_location_t = (close_t-low_t)/(high_t-low_t)
pressure_score_t = (high_t-close_t)/ATR14_t
```

다음 조건을 모두 충족한 종목만 후보가 된다.

1. 최근 14개 TR 존재, `ATR14_t > 0`
2. `open/high/low/close > 0`, `high >= max(open,close)`, `low <= min(open,close)`, `high > low`
3. `day_return_t <= -1.00%`
4. `close_location_t <= 0.20`

같은 신호일 후보는 `pressure_score` 내림차순 → `day_return` 오름차순 → symbol 오름차순으로
정렬한다. 신호는 `t` 종가 이후에만 확정되며 `t+1` 시가 전에는 체결할 수 없다.

## 4. 포트폴리오·체결·청산

- 초기자본: 100,000 USD
- 최대 동시 포지션: 5
- 종목당 최대 entry notional: 10,000 USD
- 종목당 고정 위험예산: 500 USD(초기자본의 0.5%)
- 동일 종목 동시 중복 포지션 금지
- 수량:
  `floor(min(10000/entry_open, 500/(1.5*ATR14_signal)))`; 1주 미만이면 skip
- entry: 신호일 다음 SPY 거래일의 해당 종목 **시가**
- hard stop: `entry_open - 1.5*ATR14_signal`
- stop 처리: 각 보유일 `open <= stop`이면 open, 아니면 `low <= stop`이면 stop에 청산
- Primary time exit: entry day를 1일로 세어 3번째 시장 거래일 종가
- Sensitivity time exit: entry 당일 종가
- time-exit일에도 stop을 먼저 판정한다(보수적).
- 매 거래일 진입 전, 그날 시가에서 발생한 기존 포지션의 gap-stop을 먼저 처리한다. 빈 슬롯에만
  전일 신호 후보를 순위대로 진입한다.
- 일봉 안에서 stop 이외의 경로를 추정하지 않는다. target/trailing stop/re-entry는 없다.

## 5. 비용과 손익

왕복 entry notional 비용을 각 거래에 한 번 차감한다.

| 수준 | 왕복 비용 |
|---|---:|
| low | 0.14%p |
| official | 0.34%p |
| stress | 0.68%p |

```text
gross_pnl = quantity * (exit_price-entry_price)
cost = entry_price * quantity * round_trip_cost_pct / 100
net_pnl = gross_pnl - cost
```

비용 수준마다 동일 거래·동일 체결가격을 사용한다. 비용으로 신호·수량·청산을 바꾸지 않는다.

## 6. 시간 블록과 보고 지표

SPY development 거래일을 앞에서부터 가능한 균등하게 10개 연속 블록으로 나눈다. 각 fold는 독립
초기자본으로 재실행하며, 신호 warm-up은 fold 이전 데이터를 사용할 수 있지만 진입일과 전체 보유
경로가 fold 안에 있는 거래만 포함한다. random split은 금지한다.

Primary와 sensitivity 각각 다음을 보고한다.

- gross/low/official/stress: 거래수, 총손익, 거래당 expectancy, PF, 승률
- official: MDD, 최대 연속 손실, 일별 equity, 10-fold 양수 개수
- 종목별·진입일별 공식 순손익 기여도
- 평균/중앙 보유 거래일, stop/time-exit 사유 수
- 데이터 후보 수, 제외 수·사유, 제외율

## 7. 개발 판정 게이트

데이터 선행 게이트:

| gate | 조건 |
|---|---|
| DQ1 | 필요한 entry/holding 일봉 누락·비정상 후보 / 전체 예약 후보 `<=5%` |
| DQ2 | manifest hash·유니버스·development/OOS 경계 일치 |
| DQ3 | OOS 접근 0, `blind_oos_opened=false` |

DQ 중 하나라도 실패하면 `INCONCLUSIVE_DATA`이며 경제적 실패로 해석하지 않는다.

Primary 경제·강건성 게이트는 아래를 **전부** 통과해야 한다.

| gate | 조건 |
|---|---|
| D1 | 거래 `>=300` |
| D2 | official net expectancy `>0` |
| D3 | official PF `>=1.20` |
| D4 | stress 누적 net PnL `>=0` |
| D5 | 10개 fold 중 official net PnL 양수 `>=7` |
| D6 | official MDD 절댓값 `<=10%` |
| D7 | official 총 net PnL `>0`일 때 최대 종목 기여 `<=25%`, 최대 진입일 기여 `<=20%`; 총 net `<=0`이면 실패 |
| D8 | 1일 sensitivity의 official expectancy `>0`이고 primary와 부호 일치 |

판정:

- DQ 실패: `INCONCLUSIVE_DATA`
- DQ 통과 후 D1~D8 중 하나라도 실패: `NO_SELECTION`
- 모두 통과: `DEVELOPMENT_GO` — OOS 개봉 승인이 아니며 별도 prereg merge·구현 리뷰·unlock 필요

## 8. 필수 회귀 테스트

1. ATR14 hand calculation과 warm-up
2. close-location/day-return 경계값
3. 후보 순위·symbol tie-break·입력 순서 불변
4. 신호일 체결 금지와 다음 거래일 시가 진입
5. quantity·5포지션·동일종목 중복 금지
6. gap-through stop / intraday stop / time exit 우선순위
7. 1일·3일 보유일 계산과 휴일 경계
8. 누락 보유봉 fail-closed·제외율 5% 경계
9. 비용 단일 차감·3수준 동일 gross trades
10. 10개 chronological fold와 boundary 거래 미혼입
11. OOS 첫날·중간·마지막 접근 거부 및 실행기 AST 경계
12. 같은 입력의 JSON·Markdown·거래 digest 결정론

## 9. 변경 규율

이 v1.0의 임계·비용·표본·체결·판정은 결과 확인 후 수정하지 않는다. look-ahead, OOS 혼입,
잘못된 데이터 identity 또는 손익 산식 결함만 invalidation-grade다. 성과 부진은 재실행 사유가 아니다.

