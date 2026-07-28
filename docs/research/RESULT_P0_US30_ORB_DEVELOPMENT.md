# P0-US30 ORB development walk-forward 결과

> 생성: `2026-07-28T07:33:49Z`  
> payload SHA-256: `9e30f390577491bce5ba9688b61e9338c3f2c76f7fec2a569361baa46ac0f595`  
> 범위: development-only. blind OOS는 열지 않았다.

## 1. 기계 판정

- verdict: **INCONCLUSIVE_DATA**
- development gate: **FAIL**
- failed: `["D3","D4","D5","D6","D7","D9"]`
- next stage: `None`

| 게이트 | 기준 | 실제 | 통과 |
| --- | --- | --- | --- |
| D1 | `">=536"` | `871` | PASS |
| D2 | `">=322"` | `486` | PASS |
| D3 | `">0"` | `-56.764970090786385` | FAIL |
| D4 | `">=1.2"` | `0.6049204802866439` | FAIL |
| D5 | `"<=0.1"` | `0.5043709846249758` | FAIL |
| D6 | `">=7"` | `1` | FAIL |
| D7 | `">=0.0"` | `-104050.36390814993` | FAIL |
| D8 | `"<=1 opposite-sign neighbors"` | `0` | PASS |
| D9 | `"defect policy"` | `{"conflict_total":28,"data_ratio":0.06353233830845771,"major_defect_count":0,"operational_ratio":0.0}` | FAIL |

## 2. 실행 전 절차 P1~P3

- P1 review: `approved` — https://github.com/peg5325/ai-trading-system/pull/93
- P1 implementation merge: `ddaacabfb28fca11c1f3280b89fe522a3fca76c2`
- P2 loader AST boundary: `passed` — `tests/test_oos_guard_wiring.py::test_no_direct_load_candles_outside_gate`
- P3 snapshot identity: `matched_all_four_hashes`
- P3 tracked manifest: `docs/research/P0_US30_ORB_CLEAN_1M_MANIFEST.json`
- P3 tracked manifest SHA-256: `0b77433c58dbc15c933d2077fbf3cf64b65d194078c8bb6c498a1a78866fc978`

## 3. 입력 identity

- snapshot: `p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite`
- file: `3c2c475125594bd7fe8c59d994bdc6d4cb9f1f1e0b69005b9fbcbf5eb852ca33`
- content: `d0d145518938686937895fb3c886a4c2fd996588f129e8234a5913b6be2597e4`
- manifest: `e270a1b56d2466a856ef92e32abb58730953b7b77ce1038d7b596f66f477bd0c`
- raw: `22c02118c7c8bffc225fc021e2c4eb9ad4bf7b1d8c66df8e927909c46e609ed4`
- producer commit: `3a7495658587efba3c63b275e0201d33d3f03f0e`
- prereg merge: `e42dafcf4a5f803c1af9a3535a980492c93e9c96`

## 4. 8개 파라미터화 — 공식 비용

| spec | 권한 | OR | gap | RVOL | target | trades | days | net PnL | expectancy | PF | MDD |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| primary | decides_go_nogo | 15 | 0.003000 | 1.500000 | 2.000000 | 871 | 486 | -49442.288949 | -56.764970 | 0.604920 | 0.504371 |
| rvol_1.3 | diagnostic_only | 15 | 0.003000 | 1.300000 | 2.000000 | 1105 | 552 | -56710.464161 | -51.321687 | 0.620278 | 0.575499 |
| rvol_1.7 | diagnostic_only | 15 | 0.003000 | 1.700000 | 2.000000 | 701 | 422 | -43117.849932 | -61.509058 | 0.590783 | 0.442209 |
| gap_0.002 | diagnostic_only | 15 | 0.002000 | 1.500000 | 2.000000 | 904 | 495 | -50288.946429 | -55.629366 | 0.603336 | 0.512671 |
| gap_0.004 | diagnostic_only | 15 | 0.004000 | 1.500000 | 2.000000 | 827 | 474 | -45305.986422 | -54.783539 | 0.619576 | 0.464754 |
| target_1.5 | diagnostic_only | 15 | 0.003000 | 1.500000 | 1.500000 | 880 | 486 | -54954.257356 | -62.448020 | 0.563933 | 0.556798 |
| target_2.5 | diagnostic_only | 15 | 0.003000 | 1.500000 | 2.500000 | 866 | 486 | -50322.038538 | -58.108590 | 0.596013 | 0.514756 |
| sensitivity | diagnostic_only | 5 | 0.003000 | 1.500000 | 2.000000 | 1101 | 532 | -68461.885850 | -62.181549 | 0.576389 | 0.688481 |

## 5. Primary 비용 민감도

| 비용 | trades | net PnL | expectancy | PF | MDD | positive folds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| low | 871 | -17319.891914 | -19.885065 | 0.836791 | 0.191069 | 4 |
| official | 871 | -49442.288949 | -56.764970 | 0.604920 | 0.504371 | 1 |
| stress | 871 | -104050.363908 | -119.460808 | 0.359113 | 1.039773 | 0 |

## 6. 결함 및 감사

- data ratio: `1277/20100 = 0.063532`
- operational ratio: `0/871 = 0.000000`
- major defects: `0`
- tracked conflict total: `28`
- excluded summary: `[{"count":708,"reason":"incomplete_opening_range","spec":"gap_0.002"},{"count":826,"reason":"insufficient_rvol_lookback","spec":"gap_0.002"},{"count":193,"reason":"no_previous_regular_close","spec":"gap_0.002"},{"count":193,"reason":"no_regular_data","spec":"gap_0.002"},{"count":708,"reason":"incomplete_opening_range","spec":"gap_0.004"},{"count":826,"reason":"insufficient_rvol_lookback","spec":"gap_0.004"},{"count":193,"reason":"no_previous_regular_close","spec":"gap_0.004"},{"count":193,"reason":"no_regular_data","spec":"gap_0.004"},{"count":708,"reason":"incomplete_opening_range","spec":"primary"},{"count":826,"reason":"insufficient_rvol_lookback","spec":"primary"},{"count":193,"reason":"no_previous_regular_close","spec":"primary"},{"count":193,"reason":"no_regular_data","spec":"primary"},{"count":708,"reason":"incomplete_opening_range","spec":"rvol_1.3"},{"count":826,"reason":"insufficient_rvol_lookback","spec":"rvol_1.3"},{"count":193,"reason":"no_previous_regular_close","spec":"rvol_1.3"},{"count":193,"reason":"no_regular_data","spec":"rvol_1.3"},{"count":708,"reason":"incomplete_opening_range","spec":"rvol_1.7"},{"count":826,"reason":"insufficient_rvol_lookback","spec":"rvol_1.7"},{"count":193,"reason":"no_previous_regular_close","spec":"rvol_1.7"},{"count":193,"reason":"no_regular_data","spec":"rvol_1.7"},{"count":708,"reason":"incomplete_opening_range","spec":"sensitivity"},{"count":826,"reason":"insufficient_rvol_lookback","spec":"sensitivity"},{"count":193,"reason":"no_previous_regular_close","spec":"sensitivity"},{"count":193,"reason":"no_regular_data","spec":"sensitivity"},{"count":708,"reason":"incomplete_opening_range","spec":"target_1.5"},{"count":826,"reason":"insufficient_rvol_lookback","spec":"target_1.5"},{"count":193,"reason":"no_previous_regular_close","spec":"target_1.5"},{"count":193,"reason":"no_regular_data","spec":"target_1.5"},{"count":708,"reason":"incomplete_opening_range","spec":"target_2.5"},{"count":826,"reason":"insufficient_rvol_lookback","spec":"target_2.5"},{"count":193,"reason":"no_previous_regular_close","spec":"target_2.5"},{"count":193,"reason":"no_regular_data","spec":"target_2.5"}]`
- skipped summary: `[{"count":4,"reason":"family_cap","spec":"gap_0.002"},{"count":18,"reason":"qty_lt_1","spec":"gap_0.002"},{"count":87,"reason":"signal_expired","spec":"gap_0.002"},{"count":758,"reason":"slot_cap","spec":"gap_0.002"},{"count":4,"reason":"family_cap","spec":"gap_0.004"},{"count":19,"reason":"qty_lt_1","spec":"gap_0.004"},{"count":75,"reason":"signal_expired","spec":"gap_0.004"},{"count":637,"reason":"slot_cap","spec":"gap_0.004"},{"count":4,"reason":"family_cap","spec":"primary"},{"count":18,"reason":"qty_lt_1","spec":"primary"},{"count":83,"reason":"signal_expired","spec":"primary"},{"count":700,"reason":"slot_cap","spec":"primary"},{"count":8,"reason":"family_cap","spec":"rvol_1.3"},{"count":24,"reason":"qty_lt_1","spec":"rvol_1.3"},{"count":119,"reason":"signal_expired","spec":"rvol_1.3"},{"count":1186,"reason":"slot_cap","spec":"rvol_1.3"},{"count":3,"reason":"family_cap","spec":"rvol_1.7"},{"count":15,"reason":"qty_lt_1","spec":"rvol_1.7"},{"count":58,"reason":"signal_expired","spec":"rvol_1.7"},{"count":406,"reason":"slot_cap","spec":"rvol_1.7"},{"count":8,"reason":"family_cap","spec":"sensitivity"},{"count":33,"reason":"qty_lt_1","spec":"sensitivity"},{"count":70,"reason":"signal_expired","spec":"sensitivity"},{"count":892,"reason":"slot_cap","spec":"sensitivity"},{"count":4,"reason":"family_cap","spec":"target_1.5"},{"count":18,"reason":"qty_lt_1","spec":"target_1.5"},{"count":83,"reason":"signal_expired","spec":"target_1.5"},{"count":691,"reason":"slot_cap","spec":"target_1.5"},{"count":4,"reason":"family_cap","spec":"target_2.5"},{"count":18,"reason":"qty_lt_1","spec":"target_2.5"},{"count":83,"reason":"signal_expired","spec":"target_2.5"},{"count":705,"reason":"slot_cap","spec":"target_2.5"}]`

## 7. 범위 제한

- window: `2023-05-18 ~ 2026-01-20` (670 거래일)
- loader: `{"function":"src.history.oos_guard.research_load","purpose":"performance","track":"intraday"}`
- blind OOS opened: `False`
- 이 결과가 development gate를 통과하지 못하면 blind OOS를 열지 않는다.
