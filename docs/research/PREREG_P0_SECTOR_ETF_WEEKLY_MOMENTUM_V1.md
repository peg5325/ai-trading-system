# P0 섹터 ETF 주간 상대강도 로테이션 사전등록 v1.0

> 상태: **결과 미열람·구현 미실행.** 이 문서와 동형 JSON을 먼저 커밋·원격 고정한 뒤에만
> 구현과 development 백테스트를 시작한다. swing blind OOS(2025-07-21~2026-07-21)는 열지 않는다.

## 1. 연구 질문과 범위

P0의 비레버리지 섹터 ETF 중 중기 상대강도가 높은 두 종목을 주 1회만 교체하고, 시장 장기추세가
하락이면 현금화하는 방식이 동일 노출의 섹터 ETF 균등보유보다 비용 후 우월한가?

- 전략 id: `p0_sector_etf_weekly_momentum_v1`
- 공식 8종목: `SMH,XLE,XLF,XLI,XLK,XLU,XLV,XLY`
- long-only, 최대 2종목, 주간 평가, 유지 종목 재매매 금지
- 외부 공급자·기업 이벤트·1분봉을 사용하지 않는다.
- 고정 P0 ETF 표본의 development 판정이며 전체 산업군 일반화를 주장하지 않는다.

배경 근거:

- Moskowitz & Grinblatt (1999), industry momentum,
  https://doi.org/10.1111/0022-1082.00146
- Jegadeesh & Titman (1993), 중기 cross-sectional momentum,
  https://doi.org/10.1111/j.1540-6261.1993.tb04702.x
- Korajczyk & Sadka (2004), momentum과 거래비용,
  https://doi.org/10.1111/j.1540-6261.2004.00656.x
- Daniel & Moskowitz (2016), momentum crash 위험,
  https://doi.org/10.1016/j.jfineco.2015.12.002

원 연구는 주로 월 단위다. 아래의 주간 실행은 논문 복제가 아니라 현재 데이터·비용 제약에서
검증하는 별도 가설이다.

## 2. 데이터 identity와 OOS 경계

- 정본: `config/universe_p0_us30.yaml`의 `class=sector_etf`
- 데이터 증거: `docs/research/P0_US30_DAILY_10Y_MANIFEST.json`
- 고정 일봉 지문: `756e5bd7cd4cc393dc18f88792a2f56b83a02ca45f28afb3633b139abac07772`
- development: **2016-03-18~2025-07-18** inclusive
- blind OOS: **2025-07-21~2026-07-21** inclusive, 미개봉
- 로드는 `research_load(..., track=TRACK_SWING, purpose="performance")`로 수행하고 end는
  `2025-07-21T00:00 ET` 이전 반개구간이다. grant를 생성·전달하지 않는다.
- 거래일 달력은 SPY 완성 일봉 날짜다. 8개 ETF 모두의 신호일·체결일 OHLC와 필요한 lookback이
  있어야 그 주를 평가한다. 결측은 보간하지 않는다.

## 3. 주간 신호와 순위

ISO 주별 마지막 SPY 거래일을 신호일 `t`로 삼고 다음 SPY 거래일 시가에 거래한다. 따라서 휴일이
있는 주도 달력 날짜가 아니라 실제 시장 거래일로 처리한다.

```text
ret63_skip5  = close[t-5] / close[t-63]  - 1
ret126_skip5 = close[t-5] / close[t-126] - 1
primary_score = 0.5*ret63_skip5 + 0.5*ret126_skip5
sensitivity_score = ret126_skip5
```

모든 참조는 신호일 완성 종가 이전이다. 최근 5거래일은 순위 계산에서 제외한다.

Primary와 sensitivity 각각:

1. `SPY close_t > SMA200_t`(strict)가 아니면 목표 포트폴리오는 현금이다.
2. 점수 `>0`인 ETF만 eligible이다.
3. 점수 내림차순 → symbol 오름차순으로 정렬한다.
4. 상위 2개를 목표로 한다. eligible이 하나면 하나만, 없으면 현금이다.

## 4. 체결·보유·교체

- 다음 SPY 거래일 open에 제거 종목을 먼저 매도하고 신규 종목을 매수한다.
- 선택 전략 신규 진입 notional은 종목당 25,000 USD, 최대 총 50,000 USD다.
- 연구 계산은 fractional shares를 허용한다.
- 다음 주에도 목표에 남은 종목은 수량을 조정하지 않고 그대로 보유한다. 추가매수·비중 재조정·
  비용 재부과가 없다.
- 목표에서 빠지거나 시장 regime이 꺼지면 다음 trade day open에 전량 매도한다.
- stop, target, scale-in, averaging down은 없다.
- development 마지막 날 남은 포지션은 해당일 close에 강제청산한다.

동일 regime baseline `equal_all_same_regime`은 8종목에 각각 6,250 USD를 진입해 같은 총 50,000
USD 노출을 사용한다. regime이 유지되는 동안 수량을 재조정하지 않는다. SPY buy-and-hold는
참고 보고만 하고 판정 게이트로 쓰지 않는다.

## 5. 비용과 turnover

왕복 비용은 0.14/0.34/0.68%p이며 편도율은 절반이다.

```text
entry_cost = entry_notional * round_trip_rate / 2
exit_cost  = exit_notional  * round_trip_rate / 2
```

비용은 실제 매수·매도 금액에만 적용한다. 유지 종목에는 turnover와 비용이 0이다. 비용 수준 간
신호·수량·가격은 동일하다.

## 6. 보고와 시간 블록

Primary·sensitivity·equal-all에 대해 gross/low/official/stress별로 다음을 보고한다.

- 완료 position episode 수, 누적 net PnL, episode expectancy, PF, 승률
- 실제 매수·매도 turnover notional, 총 비용, gross 대비 cost drag
- 일별 mark-to-market equity, initial-capital 기준 MDD, 최대 연속손실
- 종목별·진입주별 순손익, 평균·중앙 보유 거래일
- regime-on 주, 현금 주, 유효/제외 신호 주, 교체·유지 수
- SPY buy-and-hold 참고 수익

SPY development 거래일을 가능한 균등한 10개 연속 블록으로 나눈다. 각 fold는 빈 포지션과 독립
초기자본으로 시작하고 fold 안 trade day에서만 진입하며, 마지막 날 close에 청산한다. 지표 계산은
fold 이전 development 데이터를 사용할 수 있다. 각 fold에서 primary official net PnL이 equal-all
official net PnL보다 큰지 판정한다.

## 7. 개발 판정 게이트

데이터 게이트:

| gate | 조건 |
|---|---|
| DQ1 | 결측·비정상으로 제외된 신호 주 / 전체 예약 신호 주 `<=5%` |
| DQ2 | manifest hash·8종목·development/OOS 경계 일치 |
| DQ3 | OOS 접근 0, `blind_oos_opened=false` |

Primary 경제·강건성 게이트는 전부 통과해야 한다.

| gate | 조건 |
|---|---|
| D1 | 완료 position episode `>=100` |
| D2 | official episode expectancy `>0` |
| D3 | official PF `>=1.20` |
| D4 | stress 누적 net PnL `>=0` |
| D5 | 10개 fold 중 primary official가 equal-all official보다 큰 fold `>=7` |
| D6 | official MDD 절댓값 `<=15%` |
| D7 | primary official 누적 net PnL `>` equal-all official 누적 net PnL |
| D8 | official 총 net이 양수일 때 최대 종목 순기여 `<=50%` |
| D9 | sensitivity official 누적 net PnL `>0` |

- DQ 실패: `INCONCLUSIVE_DATA`
- DQ 통과 후 D1~D9 중 하나라도 실패: `NO_SELECTION`
- 전부 통과: `DEVELOPMENT_GO` — OOS·Paper·실거래 승인이 아님

## 8. 필수 회귀 테스트

1. 섹터 8종목 exact universe와 데이터 hash
2. ISO 주의 마지막 실제 거래일과 휴일 주 경계
3. 63/126일 수익률이 최근 5거래일을 정확히 제외
4. signal close 이후 다음 거래일 open 체결, look-ahead 부재
5. SPY SMA200 strict·absolute score strict·top2·동점 symbol 순서
6. 제거 매도 후 신규 매수, 유지 종목 수량·비용 불변
7. regime off 현금화와 재진입
8. selected 25,000×2와 equal-all 6,250×8의 동일 총 노출
9. 편도 비용·실제 turnover만 과금·비용 간 동일 gross path
10. 마지막 development close 청산
11. 독립 10 fold와 fold 경계 강제청산
12. mark-to-market MDD·episode PF·기여도·보유일
13. 결측 주 fail-closed와 5% 포함 경계
14. OOS grant 부재·TRACK_SWING 좁은 정적 허용·반개구간
15. JSON·Markdown·episode digest 결정론

## 9. 변경 규율

결과 확인 후 lookback·상위 수·regime·비용·노출·baseline·게이트를 바꾸지 않는다. look-ahead,
OOS 혼입, 잘못된 데이터 identity 또는 손익·turnover·체결 계약 결함만 invalidation-grade다.
