# P0-US30 Gap+RVOL development walk-forward result

> generated: `2026-07-30T06:03:13Z`
> payload SHA-256: `a0e46bf6f4135dab6961107fcb50ab226ee4ad5be5338cdde15097893ba81f14`
> scope: development-only. blind OOS was not opened.

## 1. Machine Verdict

- verdict: **INCONCLUSIVE_DATA**
- development gate: **FAIL**
- failed: `["D3","D4","D5","D6","D7","D9"]`
- next stage: `None`

| gate | criterion | actual | pass |
| --- | --- | --- | --- |
| D1 | `">=536"` | `603` | PASS |
| D2 | `">=322"` | `377` | PASS |
| D3 | `">0"` | `-81.57507016137305` | FAIL |
| D4 | `">=1.2"` | `0.2779373895028917` | FAIL |
| D5 | `"<=0.1"` | `0.49909080781730136` | FAIL |
| D6 | `">=7"` | `0` | FAIL |
| D7 | `">=0.0"` | `-87673.42713461595` | FAIL |
| D8 | `"<=1 opposite-sign neighbors"` | `0` | PASS |
| D9 | `"defect policy"` | `{"data_ratio":0.06447761194029851,"major_defects":28,"operational_ratio":0.0}` | FAIL |

## 2. Preconditions P1-P5

- P1 prereg merge: `d16c6692beca828e6afba32b57fad8a0282089c6`
- P2 implementation merge: `caa507415c2d5e34a25279fa83a356d6b19a54e6`
- P2 implementation map: `tracked`
- P2 implementation review: https://github.com/peg5325/ai-trading-system/pull/101#issuecomment-5126919041
- P3 runner reviewed commit: `8f36af564c6d8bda5820fcff991f60f2a2496f1a`
- P3 runner code-reviewer/architect: `approved` / `clear`
- P3 runner review evidence: https://github.com/peg5325/ai-trading-system/pull/103#issuecomment-5127205706
- P4 snapshot identity: `matched_all_four_hashes`
- P4 tracked manifest: `docs/research/P0_US30_ORB_CLEAN_1M_MANIFEST.json`
- P4 tracked manifest SHA-256: `0b77433c58dbc15c933d2077fbf3cf64b65d194078c8bb6c498a1a78866fc978`
- P5 loader boundary: `passed` - `tests/test_gap_rvol_loading_and_map.py::test_performance_package_and_runner_have_no_loader_bypass`

## 3. Input Identity

- snapshot: `p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite`
- file: `3c2c475125594bd7fe8c59d994bdc6d4cb9f1f1e0b69005b9fbcbf5eb852ca33`
- content: `d0d145518938686937895fb3c886a4c2fd996588f129e8234a5913b6be2597e4`
- manifest: `e270a1b56d2466a856ef92e32abb58730953b7b77ce1038d7b596f66f477bd0c`
- raw: `22c02118c7c8bffc225fc021e2c4eb9ad4bf7b1d8c66df8e927909c46e609ed4`
- producer commit: `8f36af564c6d8bda5820fcff991f60f2a2496f1a`
- prereg merge: `d16c6692beca828e6afba32b57fad8a0282089c6`

## 4. 9 Specs - 3 Costs

| spec | authority | gap | RVOL | retention | target | trades | days | net PnL | expectancy | PF | MDD |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| primary | decides_go_nogo | 0.003000 | 1.500000 | 0.500000 | 2.000000 | 603 | 377 | -49189.767307 | -81.575070 | 0.277937 | 0.499091 |
| gap_threshold_0.002 | diagnostic_only | 0.002000 | 1.500000 | 0.500000 | 2.000000 | 626 | 385 | -50299.624442 | -80.350838 | 0.277485 | 0.510393 |
| gap_threshold_0.004 | diagnostic_only | 0.004000 | 1.500000 | 0.500000 | 2.000000 | 569 | 366 | -46132.341754 | -81.076172 | 0.287661 | 0.468949 |
| rvol_threshold_1.3 | diagnostic_only | 0.003000 | 1.300000 | 0.500000 | 2.000000 | 762 | 439 | -57918.157071 | -76.008080 | 0.292341 | 0.586266 |
| rvol_threshold_1.7 | diagnostic_only | 0.003000 | 1.700000 | 0.500000 | 2.000000 | 468 | 315 | -41499.610132 | -88.674381 | 0.270110 | 0.421860 |
| retention_fraction_0.4 | diagnostic_only | 0.003000 | 1.500000 | 0.400000 | 2.000000 | 624 | 387 | -50034.139562 | -80.182916 | 0.287974 | 0.507666 |
| retention_fraction_0.6 | diagnostic_only | 0.003000 | 1.500000 | 0.600000 | 2.000000 | 578 | 366 | -46950.305513 | -81.228902 | 0.276085 | 0.477448 |
| target_r_multiple_1.5 | diagnostic_only | 0.003000 | 1.500000 | 0.500000 | 1.500000 | 609 | 377 | -50140.084070 | -82.331829 | 0.256243 | 0.506692 |
| target_r_multiple_2.5 | diagnostic_only | 0.003000 | 1.500000 | 0.500000 | 2.500000 | 593 | 377 | -48072.807610 | -81.067129 | 0.283836 | 0.485344 |

## 5. Primary Cost Sensitivity

| cost | trades | net PnL | expectancy | PF | MDD | positive folds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| low | 603 | -26552.320350 | -44.033699 | 0.487709 | 0.281297 | 0 |
| official | 603 | -49189.767307 | -81.575070 | 0.277937 | 0.499091 | 0 |
| stress | 603 | -87673.427135 | -145.395402 | 0.116799 | 0.877130 | 0 |

## 6. Defects And Audit

- data ratio: `1296/20100 = 0.064478`
- operational ratio: `0/603 = 0.000000`
- major defects: `28`
- tracked conflict total: `28`
- identity failures: `[]`
- excluded summary: `[{"count":734,"reason":"incomplete_decision_bar","spec":"gap_threshold_0.002"},{"count":708,"reason":"incomplete_rvol_window","spec":"gap_threshold_0.002"},{"count":566,"reason":"insufficient_rvol_lookback","spec":"gap_threshold_0.002"},{"count":723,"reason":"no_previous_regular_close","spec":"gap_threshold_0.002"},{"count":734,"reason":"incomplete_decision_bar","spec":"gap_threshold_0.004"},{"count":708,"reason":"incomplete_rvol_window","spec":"gap_threshold_0.004"},{"count":566,"reason":"insufficient_rvol_lookback","spec":"gap_threshold_0.004"},{"count":723,"reason":"no_previous_regular_close","spec":"gap_threshold_0.004"},{"count":734,"reason":"incomplete_decision_bar","spec":"primary"},{"count":708,"reason":"incomplete_rvol_window","spec":"primary"},{"count":566,"reason":"insufficient_rvol_lookback","spec":"primary"},{"count":723,"reason":"no_previous_regular_close","spec":"primary"},{"count":734,"reason":"incomplete_decision_bar","spec":"retention_fraction_0.4"},{"count":708,"reason":"incomplete_rvol_window","spec":"retention_fraction_0.4"},{"count":566,"reason":"insufficient_rvol_lookback","spec":"retention_fraction_0.4"},{"count":723,"reason":"no_previous_regular_close","spec":"retention_fraction_0.4"},{"count":734,"reason":"incomplete_decision_bar","spec":"retention_fraction_0.6"},{"count":708,"reason":"incomplete_rvol_window","spec":"retention_fraction_0.6"},{"count":566,"reason":"insufficient_rvol_lookback","spec":"retention_fraction_0.6"},{"count":723,"reason":"no_previous_regular_close","spec":"retention_fraction_0.6"},{"count":734,"reason":"incomplete_decision_bar","spec":"rvol_threshold_1.3"},{"count":708,"reason":"incomplete_rvol_window","spec":"rvol_threshold_1.3"},{"count":566,"reason":"insufficient_rvol_lookback","spec":"rvol_threshold_1.3"},{"count":723,"reason":"no_previous_regular_close","spec":"rvol_threshold_1.3"},{"count":734,"reason":"incomplete_decision_bar","spec":"rvol_threshold_1.7"},{"count":708,"reason":"incomplete_rvol_window","spec":"rvol_threshold_1.7"},{"count":566,"reason":"insufficient_rvol_lookback","spec":"rvol_threshold_1.7"},{"count":723,"reason":"no_previous_regular_close","spec":"rvol_threshold_1.7"},{"count":734,"reason":"incomplete_decision_bar","spec":"target_r_multiple_1.5"},{"count":708,"reason":"incomplete_rvol_window","spec":"target_r_multiple_1.5"},{"count":566,"reason":"insufficient_rvol_lookback","spec":"target_r_multiple_1.5"},{"count":723,"reason":"no_previous_regular_close","spec":"target_r_multiple_1.5"},{"count":734,"reason":"incomplete_decision_bar","spec":"target_r_multiple_2.5"},{"count":708,"reason":"incomplete_rvol_window","spec":"target_r_multiple_2.5"},{"count":566,"reason":"insufficient_rvol_lookback","spec":"target_r_multiple_2.5"},{"count":723,"reason":"no_previous_regular_close","spec":"target_r_multiple_2.5"}]`
- filtered summary: `[{"count":180,"reason":"continuation_not_triggered","spec":"gap_threshold_0.002"},{"count":10797,"reason":"gap_below_threshold","spec":"gap_threshold_0.002"},{"count":686,"reason":"retention_broken","spec":"gap_threshold_0.002"},{"count":6272,"reason":"rvol_below_threshold","spec":"gap_threshold_0.002"},{"count":162,"reason":"continuation_not_triggered","spec":"gap_threshold_0.004"},{"count":12715,"reason":"gap_below_threshold","spec":"gap_threshold_0.004"},{"count":566,"reason":"retention_broken","spec":"gap_threshold_0.004"},{"count":4571,"reason":"rvol_below_threshold","spec":"gap_threshold_0.004"},{"count":170,"reason":"continuation_not_triggered","spec":"primary"},{"count":11806,"reason":"gap_below_threshold","spec":"primary"},{"count":630,"reason":"retention_broken","spec":"primary"},{"count":5364,"reason":"rvol_below_threshold","spec":"primary"},{"count":193,"reason":"continuation_not_triggered","spec":"retention_fraction_0.4"},{"count":11806,"reason":"gap_below_threshold","spec":"retention_fraction_0.4"},{"count":572,"reason":"retention_broken","spec":"retention_fraction_0.4"},{"count":5364,"reason":"rvol_below_threshold","spec":"retention_fraction_0.4"},{"count":139,"reason":"continuation_not_triggered","spec":"retention_fraction_0.6"},{"count":11806,"reason":"gap_below_threshold","spec":"retention_fraction_0.6"},{"count":700,"reason":"retention_broken","spec":"retention_fraction_0.6"},{"count":5364,"reason":"rvol_below_threshold","spec":"retention_fraction_0.6"},{"count":226,"reason":"continuation_not_triggered","spec":"rvol_threshold_1.3"},{"count":11806,"reason":"gap_below_threshold","spec":"rvol_threshold_1.3"},{"count":938,"reason":"retention_broken","spec":"rvol_threshold_1.3"},{"count":4656,"reason":"rvol_below_threshold","spec":"rvol_threshold_1.3"},{"count":129,"reason":"continuation_not_triggered","spec":"rvol_threshold_1.7"},{"count":11806,"reason":"gap_below_threshold","spec":"rvol_threshold_1.7"},{"count":451,"reason":"retention_broken","spec":"rvol_threshold_1.7"},{"count":5806,"reason":"rvol_below_threshold","spec":"rvol_threshold_1.7"},{"count":170,"reason":"continuation_not_triggered","spec":"target_r_multiple_1.5"},{"count":11806,"reason":"gap_below_threshold","spec":"target_r_multiple_1.5"},{"count":630,"reason":"retention_broken","spec":"target_r_multiple_1.5"},{"count":5364,"reason":"rvol_below_threshold","spec":"target_r_multiple_1.5"},{"count":170,"reason":"continuation_not_triggered","spec":"target_r_multiple_2.5"},{"count":11806,"reason":"gap_below_threshold","spec":"target_r_multiple_2.5"},{"count":630,"reason":"retention_broken","spec":"target_r_multiple_2.5"},{"count":5364,"reason":"rvol_below_threshold","spec":"target_r_multiple_2.5"}]`
- skipped summary: `[{"count":3,"reason":"family_cap","spec":"gap_threshold_0.002"},{"count":8,"reason":"qty_lt_1","spec":"gap_threshold_0.002"},{"count":232,"reason":"slot_cap","spec":"gap_threshold_0.002"},{"count":2,"reason":"family_cap","spec":"gap_threshold_0.004"},{"count":8,"reason":"qty_lt_1","spec":"gap_threshold_0.004"},{"count":211,"reason":"slot_cap","spec":"gap_threshold_0.004"},{"count":2,"reason":"family_cap","spec":"primary"},{"count":8,"reason":"qty_lt_1","spec":"primary"},{"count":221,"reason":"slot_cap","spec":"primary"},{"count":2,"reason":"family_cap","spec":"retention_fraction_0.4"},{"count":10,"reason":"qty_lt_1","spec":"retention_fraction_0.4"},{"count":233,"reason":"slot_cap","spec":"retention_fraction_0.4"},{"count":2,"reason":"family_cap","spec":"retention_fraction_0.6"},{"count":8,"reason":"qty_lt_1","spec":"retention_fraction_0.6"},{"count":207,"reason":"slot_cap","spec":"retention_fraction_0.6"},{"count":3,"reason":"family_cap","spec":"rvol_threshold_1.3"},{"count":11,"reason":"qty_lt_1","spec":"rvol_threshold_1.3"},{"count":402,"reason":"slot_cap","spec":"rvol_threshold_1.3"},{"count":2,"reason":"family_cap","spec":"rvol_threshold_1.7"},{"count":8,"reason":"qty_lt_1","spec":"rvol_threshold_1.7"},{"count":134,"reason":"slot_cap","spec":"rvol_threshold_1.7"},{"count":2,"reason":"family_cap","spec":"target_r_multiple_1.5"},{"count":8,"reason":"qty_lt_1","spec":"target_r_multiple_1.5"},{"count":215,"reason":"slot_cap","spec":"target_r_multiple_1.5"},{"count":2,"reason":"family_cap","spec":"target_r_multiple_2.5"},{"count":8,"reason":"qty_lt_1","spec":"target_r_multiple_2.5"},{"count":231,"reason":"slot_cap","spec":"target_r_multiple_2.5"}]`

## 7. Scope Limit

- data window: `2023-04-20 ~ 2026-01-20` (690 trading days)
- eval window: `2023-05-18 ~ 2026-01-20` (670 trading days)
- loader: `{"function":"src.history.oos_guard.research_load","purpose":"performance","track":"intraday"}`
- blind OOS opened: `False`
