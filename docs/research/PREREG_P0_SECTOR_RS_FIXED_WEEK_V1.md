# 사전등록 — P0 섹터 상대강도 최대 5거래일 고정보유 v1.0

상태: **FROZEN BEFORE THIS STRATEGY RESULTS** · 이슈 #138

이 문서는 사전등록만 고정한다. 구현·백테스트·성과 산출·blind OOS 개봉은 이 PR에서 하지 않는다.

## 1. 적응적 연구임을 먼저 공개

이 가설은 기존 `P0 섹터 ETF 주간 상대강도 로테이션` 결과를 본 뒤 제안됐다. 기존 전략은 공식
수익이 양수였지만 10개 fold 중 5개만 초과성과가 양수였고 SMH 기여도가 94.47%에 달해
`NO_SELECTION`이었다.

따라서 같은 2016~2025 development 데이터를 다시 사용하는 이번 결과는 **탐색적 결과**일
뿐이다. 모든 게이트를 통과해도 blind OOS나 Paper로 승격하지 않고, untouched forward 데이터에
대한 새 사용자 승인 사전등록을 요구한다.

## 2. 연구 질문

중기 상대강도가 높은 섹터를 고르되 포지션을 다음 주로 이월하지 않고 최대 한 주만 보유하면,
동일 regime·동일 보유기간의 8개 섹터 균등보유보다 비용 후 성과와 시간 안정성이 개선되는가?

- 전략 id: `p0_sector_rs_fixed_week_v1`
- 종목: `SMH,XLE,XLF,XLI,XLK,XLU,XLV,XLY`
- long-only, primary top-3 동일 진입 notional
- SPY 장기추세가 꺼지면 현금
- 첫 거래일 진입, 같은 ISO 주 마지막 거래일 청산, 최대 5거래일
- 다음 주 이월·stop·target·scale-in·물타기 없음

## 3. 데이터와 잠금

- 정본 manifest: `docs/research/P0_US30_DAILY_10Y_MANIFEST.json`
- full content SHA-256: `756e5bd7cd4cc393dc18f88792a2f56b83a02ca45f28afb3633b139abac07772`
- development content SHA-256(2025-07-18까지):
  `96151eeb277528e1b8167e7c8c531155640cd52973c0c2f646bd23ed11018a7a`
- development: 2016-03-18~2025-07-18 inclusive
- blind OOS: 2025-07-21~2026-07-21 inclusive, 잠금 유지
- full 정본은 추적 manifest receipt로 인증하고 live DB hash는 development 종료일까지 계산한다.
- 로더는 `research_load(... purpose="performance", track=TRACK_SWING)`만 사용하고 OOS grant를
  만들거나 전달하지 않는다.

## 4. 신호와 선택

각 ISO 주의 마지막 완료 거래일을 신호일 `t`로 사용한다.

```text
ret63_skip5  = close[t-5] / close[t-63]  - 1
ret126_skip5 = close[t-5] / close[t-126] - 1
primary_score = 0.5*ret63_skip5 + 0.5*ret126_skip5
sensitivity_score = ret126_skip5
```

Primary와 sensitivity 각각:

1. 8개 ETF와 SPY의 필요한 일봉이 모두 있어야 한다.
2. `SPY close_t > SMA200_t`가 아니면 현금이다.
3. 점수 `>0`인 ETF만 eligible이다.
4. 점수 내림차순 → symbol 오름차순으로 정렬한다.
5. 상위 3개를 선택한다. eligible이 적으면 남은 슬롯은 현금이다.

최근 5거래일을 순위에서 제외하며 모든 신호 입력은 신호일 종가까지 완료된 정보다.

## 5. 체결과 고정보유

- 신호 다음 ISO 주의 첫 거래일 open에 진입한다.
- 종목당 진입 notional은 USD 50,000 / 3, fractional share를 허용한다.
- 청산은 `진입 ISO 주 마지막 거래일 close`와 `5번째 보유 거래일 close` 중 빠른 시점이다.
- 따라서 정상 주는 첫 거래일 open→마지막 거래일 close, 휴일 주는 3~4거래일일 수 있다.
- 다음 ISO 주로 포지션을 이월하지 않는다.
- 같은 주 재진입·리밸런싱·수량조정은 없다.
- 진입 또는 예정 청산 OHLC가 없거나 비정상이면 해당 주 전체를 제외한다.
- 매도 체결을 매수보다 먼저 처리한다.

동일 regime baseline은 8개 ETF에 각각 USD 6,250을 같은 entry/exit 시각으로 보유한다. 이를 통해
시장 노출이 아니라 선택의 증분 가치를 비교한다.

## 6. 비용·fold·보고

왕복 비용은 0.14/0.34/0.68%p이고 entry/exit 실제 notional에 절반씩 적용한다.

SPY development 거래일을 가능한 균등한 10개 연속 fold로 나눈다. 각 fold는 빈 포지션으로
시작하며 entry와 exit가 모두 fold 안에 있는 episode만 포함한다. 지표 warm-up은 fold 이전
development 데이터 사용을 허용한다.

primary·sensitivity·equal-all에 대해 episode 수, gross/비용별 PnL·expectancy·PF·승률,
turnover·cost drag, mark-to-market MDD, fold 초과성과, 종목/진입주 기여도, 보유일 분포와
휴일 단축 주, 데이터 제외율을 보고한다.

## 7. 판정 게이트

데이터 게이트:

- 제외 신호 주 비율 `<=5%`
- manifest/full receipt/development hash·구간 일치
- 교정 실행의 blind OOS 접근 0
- 8개 ETF 입력 완전성

Primary 경제·강건성 게이트는 전부 통과해야 한다.

- 완료 position episode `>=300`
- official expectancy `>0`, PF `>=1.20`
- stress 누적 net PnL `>=0`
- 10개 fold 중 primary official가 equal-all official보다 큰 fold `>=7`
- official MDD 절댓값 `<=10%`
- primary official total net `>` equal-all official total net
- official 총 net이 양수일 때 최대 종목 기여 `<=50%`
- official 총 net이 양수일 때 최대 진입주 기여 `<=20%`
- sensitivity official total net `>0`

판정:

- 데이터 게이트 실패: `INCONCLUSIVE_DATA`
- 경제 게이트 실패: `EXPLORATORY_NO_SELECTION`
- 전부 통과: `EXPLORATORY_GO_REQUIRES_FRESH_VALIDATION`

어떤 판정도 현재 blind OOS·Paper·live 승격을 승인하지 않는다.

## 8. 필수 구현 테스트

1. exact 8 ETF universe와 full/development hash
2. ISO 주 마지막 신호일·다음 주 첫 거래일·휴일 단축 주
3. 63/126일 수익률의 최근 5거래일 제외
4. SPY SMA200 strict, score strict, top-3, 동점 symbol 순서
5. 신호일 종가 이후 다음 주 첫 open 진입과 look-ahead 부재
6. 같은 주 마지막 close/5번째 close 중 빠른 청산, cross-week carry 0
7. top-3 USD 50k/3과 equal-all USD 6,250×8 동일 총 노출
8. 편도 비용·실제 turnover만 과금
9. fold entry/exit 동시 포함과 빈 시작
10. 제외율 5% 포함 경계와 missing/invalid week fail-closed
11. MDD·PF·fold 초과성과·기여도 계산
12. OOS grant 부재, development-bounded live hash
13. JSON/Markdown/episode digest 결정론
14. all-pass도 현재 승격하지 않고 fresh validation을 요구

## 9. 변경 규율

이 PR이 병합된 뒤 결과를 보기 전 데이터 무효화 결함만 append-only ERRATUM으로 교정할 수 있다.
lookback·top-N·보유기간·regime·비용·baseline·게이트를 결과에 맞춰 바꾸지 않는다.
