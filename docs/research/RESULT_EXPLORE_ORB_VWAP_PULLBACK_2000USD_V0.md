# 결과: 탐색 ORB+VWAP 되돌림 USD 2,000 (v0, 1회 실행)

_실행 2026-10-13. 명세 [EXPLORE_ORB_VWAP_PULLBACK_2000USD_V0.md](EXPLORE_ORB_VWAP_PULLBACK_2000USD_V0.md)(커밋 `2f24db0`, SHA-256 `56f3ee69…6f67`)._

- **지위: 탐색용, 판정 근거 아님.** Paper·실주문·승격을 승인하지 않는다. 2023-04-05 ~ 2026-01-20 1분봉은 이 전략 계열에 대해 이미 본 데이터다.
- 코드 `c456f89`(dirty 아님). 독립 리뷰 APPROVE(CRITICAL·HIGH·MEDIUM 0, LOW 6 — L1만 실행 전 반영). 전체 시험 2,888 passed·11 skipped(L1 반영 전), 관련 시험 132 passed(반영 후).
- 산출물(로컬, gitignore): `reports/explore_pullback_vwap_v0/result.json` SHA-256 `a5aa2991d6aefc397581c8e296b8d2bac5d6a8f42230e03622386e2bec751db4`.
  읽은 행: QQQ 598,358 · SPY 540,420 · IWM 494,326 · AMD 611,575 · AAPL 622,890 · AMZN 608,531 · PLTR 607,511. 봉인(2026-01-21~)은 읽지 않았다.

## 한 줄 결론

지시서 비용으로 −7.78%(USD 2,000 → 1,844.47), Toss 실제 수수료 기준(왕복 0.20%)으로 −70.94%(→ 581.15). 같은 기간 QQQ 1배 보유는 +91.32%.
비용 전 이익이 거래당 +0.02% 수준이라 비용을 넘지 못한다.

## 스크립트 출력 (그대로 옮김)

### Summary

| item | value |
|---|---|
| period | 2023-04-05 ~ 2026-01-20 |
| total trading days | 700 |
| trade days | 240 |
| no-trade days | 460 |
| day status | no_candidates 352, no_trade 99, spy_excluded 9, traded 240 |
| start equity (USD) | 2,000.00 |
| final equity (USD) | 1,844.47 |
| cumulative return | -7.78% |
| CAGR | -2.85% |
| MDD (USD) | 301.46 |
| MDD (%) | 14.82% |
| longest drawdown (trading days) | 659 |
| trades | 441 |
| win rate | 36.05% |
| profit factor | 0.91 |
| avg win (USD) | 9.43 |
| avg loss (USD) | -5.87 |
| TP1 hits | 150 |
| excluded symbol-days (incomplete) | 50 |

### By symbol

| symbol | trades | win rate | net P&L (USD) | PF |
|---|---|---|---|---|
| AAPL | 70 | 35.71% | -48.01 | 0.75 |
| AMD | 61 | 44.26% | 222.11 | 1.84 |
| AMZN | 74 | 36.49% | -42.96 | 0.84 |
| IWM | 176 | 32.39% | -183.85 | 0.65 |
| PLTR | 60 | 38.33% | -102.82 | 0.74 |

### Long / short

| side | trades | win rate | net P&L (USD) | PF |
|---|---|---|---|---|
| long | 228 | 38.16% | 79.50 | 1.09 |
| short | 213 | 33.80% | -235.03 | 0.71 |

### Exit reasons (final leg)

| reason | count |
|---|---|
| breakeven | 39 |
| ema20 | 93 |
| flatten | 38 |
| stop | 271 |

### Comparison (report only, not a decision)

| item | value |
|---|---|
| QQQ 1x buy & hold 2023-04-11 open ~ 2026-01-20 close | 91.32% |
| reference cost (0.10%/side, no slippage) final equity (USD) | 581.15 |
| reference cost cumulative return | -70.94% |
| reference cost MDD (%) | 71.05% |
| reference cost PF | 0.44 |

### By year

| year | days | trade days | trades | win rate | net P&L (USD) | return | ref return |
|---|---|---|---|---|---|---|---|
| 2023 | 186 | 73 | 125 | 34.40% | -35.31 | -1.77% | -19.30% |
| 2024 | 252 | 77 | 140 | 32.86% | -165.79 | -8.44% | -33.40% |
| 2025 | 250 | 86 | 167 | 39.52% | 42.63 | 2.37% | -43.72% |
| 2026 | 12 | 4 | 9 | 44.44% | 2.95 | 0.16% | -3.94% |

## 추가 집계 (result.json에서 스크립트로 계산, 판정 아님)

| 항목 | 값 |
|---|---|
| 비용 전 손익 합계 (441건) | +USD 180.47 |
| 비용 전 거래당 평균 수익률 | +0.0215% |
| 지시서 비용 후 합계 / 참고 비용 후 합계 | −USD 155.53 / −USD 1,418.85 |
| 거래당 비용 중앙값: 지시서(주당 0.02×2 + 0.005×2) / 참고(대금 0.20%) | USD 0.50 / USD 3.67 |
| 손절폭 중앙값 (가격 대비) | 0.28% |
| 진입 대금 중앙값 | USD 1,835 |
| 실제 위험액 중앙값 (주수 × 손절폭) / 위험 < USD 15 비율 | USD 5.04 / 94.6% |
| $250 초과로 건너뛴 신호 / 손절선 너머 체결로 건너뛴 신호 | 1,011 / 24 |

종목별 비용 전 거래당 평균: AMD +0.247%(61건), PLTR +0.0211%(60), AMZN −0.002%(74), AAPL −0.0091%(70), IWM −0.0344%(176). QQQ·SPY는 거래 0건.

## 해석

1. **비용 전 우위가 거의 없다.** 거래당 +0.02%는 이전 ORB v1(역산 +0.032%)·오후 지속(+0.035%)과 같은 크기다. 되돌림 확인 진입과
   절반 익절·EMA20 트레일링으로 바꿔도 비용 전 이익이 커지지 않았다.
2. **비용 모델이 결과를 가른다.** 지시서의 주당 비용은 USD 2,000 계좌의 실제 Toss 수수료(대금의 편도 0.10%)보다 약 7배 싸다.
   실제 수수료로는 계좌가 71% 줄어든다.
3. **"1회 USD 20 위험" 규칙은 사실상 작동하지 않았다.** 손절폭이 좁아(중앙값 0.28%) 1% 위험 수량이 현금을 넘고, 현금 한도로 잘려
   실제 위험은 대부분 USD 5 안팎이었다. 이 계좌 크기에서 노출을 정하는 것은 위험 비율이 아니라 현금 한도다.
4. **QQQ·SPY는 $250 규칙으로 한 번도 거래되지 않았다**(신호 1,011건 건너뜀). 거래는 IWM에 몰렸고(176건) IWM이 가장 큰 손실(−USD 183.85)이다.
5. **AMD 한 종목만 양수**(+USD 222.11, PF 1.84). 61건·한 종목이고 다중 비교 보정이 없어 우연과 구별할 수 없다. 이 결과를 보고 AMD만 고르는
   새 버전을 만들면 결과를 보고 고른 것이 된다.
6. 롱 +USD 79.50(PF 1.09), 숏 −USD 235.03(PF 0.71). 숏은 실제 공매도 가정이며 Toss에서는 미국주식 공매도를 하지 않는 전제다.
7. 연도별로 2025년만 지시서 비용 기준 +2.37%였고, 참고 비용으로는 모든 해가 손실이다.

## 한계

명세 §5 그대로. 추가: 1차 익절은 고가가 목표가에 닿기만 하면 체결(리뷰 L3), MDD는 일말 잔고 기준(L5), 현금 한도에 진입 수수료 미포함(L4, 최대 약 USD 0.10).
