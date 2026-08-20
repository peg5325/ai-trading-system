# P0-US30 박스 돌파 + 오더블록 리테스트 Development 결과

- verdict: **INCONCLUSIVE_DATA**
- scope: `exploratory-development-only`
- payload SHA-256: `fd0f2cbc38e08ad4f2949aee6199a86963e14e6abedb4921fc106cee6af9e230`
- development: 2016-03-18~2025-07-18 (2347 거래일)
- raw breakout signals: 42
- blind OOS opened: `false`
- promotion allowed: `false`

## Arm별 official 결과

| arm | candidates | trades | net | expectancy | PF | exp/R | MDD | folds |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| base | 41 | 38 | -1489.274947 | -39.191446 | 0.593427 | -0.280894 | -1.838005% | 5/10 |
| primary | 1 | 1 | -135.477505 | -135.477505 | 0.000000 | -1.330056 | -0.135478% | 0/10 |
| partial | 1 | 1 | -135.477505 | -135.477505 | 0.000000 | -1.330056 | -0.135478% | 0/10 |

## Primary 비용별 결과

| cost | trades | net | expectancy | PF | win rate | exp/R | MDD |
|---|---:|---:|---:|---:|---:|---:|---:|
| gross | 1 | -101.858472 | -101.858472 | 0.000000 | 0.0000% | -1.000000 | -0.113365% |
| low | 1 | -115.701603 | -115.701603 | 0.000000 | 0.0000% | -1.135906 | -0.120259% |
| official | 1 | -135.477505 | -135.477505 | 0.000000 | 0.0000% | -1.330056 | -0.135478% |
| stress | 1 | -169.096537 | -169.096537 | 0.000000 | 0.0000% | -1.660113 | -0.169097% |

## 데이터·후보 감사

- signal audit: `{"breakout_filter_rejection":54788,"breakout_signals":42,"mean_atr_contraction_ratio":0.725890115510153,"mean_box_width_fraction":0.06659958858807616,"mean_breakout_atr":0.5366797082029792,"mean_volume_ratio":1.8213360674398587,"order_block_signals":3,"retest_delay_days":{"1":1},"warmup_not_ready":4885}`
- base: prepared=41, excluded=0, rate=0.000000%, boundary=0, filters={"entry_lte_stop":1}
- primary: prepared=1, excluded=0, rate=0.000000%, boundary=0, filters={"no_order_block":39,"no_retest":2}
- canonical defects: `{"invalid_daily_bar":3,"missing_daily_bar":1280}`

## Turnover·비용·보유 감사

- base: entry_turnover=376491.696500, exit_turnover=376282.137070, cost=1279.715517, hold_mean=4.368421, hold_median=5.000000, max_loss_streak=5, exits={"gap_stop":2,"intraday_stop":11,"time_exit":25}
- primary: entry_turnover=9938.880000, exit_turnover=9837.021528, cost=33.619033, hold_mean=3.000000, hold_median=3.000000, max_loss_streak=1, exits={"intraday_stop":1}
- partial: entry_turnover=9938.880000, exit_turnover=9837.021528, cost=33.619033, hold_mean=3.000000, hold_median=3.000000, max_loss_streak=1, exits={"intraday_stop":1}

## 게이트

### 데이터

- DQ1_data_exclusion_rate_lte_0_05: **PASS**
- DQ2_identity_and_boundaries_match: **PASS**
- DQ3_current_execution_blind_oos_closed: **PASS**
- DQ4_new_conflicts_eq_0: **PASS**
- DQ5_major_defects_eq_0: **FAIL**

### 경제·강건성

- D10_partial_expectancy_gt_0: **FAIL**
- D1_min_trades: **FAIL**
- D2_min_trading_days: **FAIL**
- D3_official_expectancy_gt_0: **FAIL**
- D4_official_pf_gte_1_20: **FAIL**
- D5_stress_net_gte_0: **FAIL**
- D6_positive_folds_gte_7: **FAIL**
- D7_mdd_abs_lte_10pct: **PASS**
- D8_symbol_contribution_lte_0_35: **FAIL**
- D9_entry_day_contribution_lte_0_20: **FAIL**

## 해석 경계

- Primary만 판정 권한이 있으며 base/partial로 사후 교체하지 않는다.
- 같은 development 데이터의 결과는 exploratory-only다.
- all-pass도 untouched forward 데이터의 새 사전등록이 필요하다.
- 이 결과는 blind OOS, Paper 또는 live 승격을 승인하지 않는다.
