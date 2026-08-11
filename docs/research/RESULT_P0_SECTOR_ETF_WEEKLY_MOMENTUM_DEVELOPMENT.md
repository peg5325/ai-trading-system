# P0 섹터 ETF 주간 상대강도 로테이션 Development 결과

- verdict: **NO_SELECTION**
- strategy: `p0_sector_etf_weekly_momentum_v1`
- contract SHA-256: `4543e01984f0d53b35ea97893d9e40b4cc4a83c8b1e5d2277e85df19d5fb0d3b`
- daily data SHA-256: `756e5bd7cd4cc393dc18f88792a2f56b83a02ca45f28afb3633b139abac07772`
- development: 2016-03-18~2025-07-18 (2,347 SPY 거래일)
- symbols: SMH,XLE,XLF,XLI,XLK,XLU,XLV,XLY
- blind OOS opened: `False`

## 1. 비용별 성과

| portfolio | cost | episodes | net PnL | expectancy | PF | win rate | MDD | turnover | cost |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| primary | gross | 136 | 54822.750103 | 403.108457 | 2.327140 | 61.0294% | -10.968345% | 6854822.750103 | 0.000000 |
| primary | low | 136 | 50024.374178 | 367.826281 | 2.157591 | 55.8824% | -11.868709% | 6854822.750103 | 4798.375925 |
| primary | official | 136 | 43169.551428 | 317.423172 | 1.931276 | 50.7353% | -13.210719% | 6854822.750103 | 11653.198675 |
| primary | stress | 136 | 31516.352753 | 231.737888 | 1.605876 | 48.5294% | -15.656389% | 6854822.750103 | 23306.397350 |
| sensitivity | gross | 147 | 51446.250513 | 349.974493 | 2.161705 | 57.8231% | -8.651149% | 7401446.250513 | 0.000000 |
| sensitivity | low | 147 | 46265.238137 | 314.729511 | 1.994121 | 53.0612% | -9.443279% | 7401446.250513 | 5181.012375 |
| sensitivity | official | 147 | 38863.791887 | 264.379537 | 1.775871 | 49.6599% | -11.005724% | 7401446.250513 | 12582.458626 |
| sensitivity | stress | 147 | 26281.333261 | 178.784580 | 1.465785 | 47.6190% | -13.861620% | 7401446.250513 | 25164.917252 |
| equal_all | gross | 128 | 42799.476795 | 334.370912 | 3.676600 | 46.8750% | -9.565604% | 1642799.476795 | 0.000000 |
| equal_all | low | 128 | 41649.517162 | 325.386853 | 3.511781 | 46.0938% | -9.917415% | 1642799.476795 | 1149.959634 |
| equal_all | official | 128 | 40006.717685 | 312.552482 | 3.295519 | 46.0938% | -10.424762% | 1642799.476795 | 2792.759111 |
| equal_all | stress | 128 | 37213.958574 | 290.734051 | 2.969933 | 44.5312% | -11.300349% | 1642799.476795 | 5585.518221 |

## 2. 주간 신호·turnover 감사

| spec | scheduled | warmup | valid | invalid | invalid rate | boundary | regime on | cash | digest |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| primary | 488 | 41 | 446 | 0 | 0.0000% | 1 | 351 | 95 | `bd37ee62c7f3dc29…` |
| sensitivity | 488 | 41 | 446 | 0 | 0.0000% | 1 | 351 | 96 | `1029c80c0825b41d…` |

### 공식 비용 실행 수치

| portfolio | buys | sells | retained | mean hold | median hold | max loss streak | max symbol contribution | cost drag / gross PnL |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| primary | 136 | 136 | 565 | 25.816176 | 11.000000 | 6 | 0.944717 | 0.212561 |
| sensitivity | 147 | 147 | 550 | 23.823129 | 11 | 6 | 0.844636 | 0.244575 |
| equal_all | 128 | 128 | 2,680 | 106.625000 | 36.000000 | 11 | 0.307075 | 0.065252 |

## 3. 데이터·경제 게이트

### 데이터

- DQ1_invalid_week_rate_lte_0_05: **PASS**
- DQ2_identity_universe_boundaries_match: **PASS**
- DQ3_blind_oos_closed: **PASS**

### 경제·강건성

- D1_min_completed_position_episodes: **PASS**
- D2_official_expectancy_gt_0: **PASS**
- D3_official_pf_gte_1_20: **PASS**
- D4_stress_total_net_pnl_gte_0: **PASS**
- D5_positive_excess_folds_gte_7: **FAIL**
- D6_official_mdd_abs_lte_15pct: **PASS**
- D7_official_net_gt_equal_all: **PASS**
- D8_symbol_contribution_lte_0_50: **FAIL**
- D9_sensitivity_official_net_gt_0: **PASS**

## 4. 독립 시간 블록 — primary vs equal-all

| fold | start | end | primary net | equal-all net | excess | positive |
|---:|---|---|---:|---:|---:|:---:|
| 1 | 2016-03-18 | 2017-02-22 | 2476.897546 | 1958.095334 | 518.802213 | Y |
| 2 | 2017-02-23 | 2018-01-29 | 11779.876163 | 10279.729382 | 1500.146781 | Y |
| 3 | 2018-01-30 | 2019-01-04 | -1771.353425 | -546.355933 | -1224.997492 | N |
| 4 | 2019-01-07 | 2019-12-10 | 1744.775738 | 1937.536311 | -192.760573 | N |
| 5 | 2019-12-11 | 2020-11-13 | 8899.297857 | 1073.722384 | 7825.575473 | Y |
| 6 | 2020-11-16 | 2021-10-21 | 8840.659886 | 14463.791349 | -5623.131463 | N |
| 7 | 2021-10-22 | 2022-09-28 | -6537.632100 | -3677.388858 | -2860.243242 | N |
| 8 | 2022-09-29 | 2023-09-05 | 4642.787632 | 579.220260 | 4063.567372 | Y |
| 9 | 2023-09-06 | 2024-08-09 | 3499.207507 | 6488.105203 | -2988.897696 | N |
| 10 | 2024-08-12 | 2025-07-18 | 8013.349123 | 5132.460356 | 2880.888767 | Y |

## 5. 기준선과 공식 비용 기여도

- SPY buy-and-hold 참고 gross PnL(50,000 USD): 89437.433345
- primary official net: 43169.551428
- equal-all official net: 40006.717685

### Primary 종목별 official net PnL

| symbol | net PnL |
|---|---:|
| SMH | 40783.012714 |
| XLE | -2435.628820 |
| XLF | 1536.528841 |
| XLI | -1733.156819 |
| XLK | 6416.446995 |
| XLU | -375.409747 |
| XLV | -2800.584673 |
| XLY | 1778.342937 |

## 6. 판정 범위

이 결과는 고정 8개 ETF의 development-only 판정이다. `DEVELOPMENT_GO`도 blind OOS, Paper 또는 실거래 승인을 뜻하지 않는다. `NO_SELECTION`은 사전 고정 게이트 실패이며 결과에 맞춰 신호·보유·비용을 수정하지 않는다.

## 7. 실행 감사

- initial_markdown_render: `derived_invalid_week_rate_missing_from_asdict` → `explicit_property_serialization`; 성과 사전열람=False, 전략 규칙 변경=False
- post_result_self_review: `cost_drag_ratio_field_misnamed_as_gross_profit` → `renamed_to_gross_pnl_without_formula_change`; 성과 사전열람=True, 전략 규칙 변경=False
