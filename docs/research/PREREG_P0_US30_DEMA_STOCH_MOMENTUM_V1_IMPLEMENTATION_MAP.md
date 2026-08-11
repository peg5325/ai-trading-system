# DEMA(60) + Stochastic %D(60) v1.1 구현 매핑

> 이슈 #128 · development 실행 전 구현 증거 · blind OOS 미개봉

| 계약 | 구현 | 회귀 증거 |
| --- | --- | --- |
| prereg hash·개발 구간·fold | `src/research/dema_stoch/contract.py` | `test_contract_is_frozen_and_blind_oos_is_not_in_development` |
| DEMA `2EMA-EMA(EMA)` | `src/research/dema_stoch/indicators.py:dema` | `test_ema_and_dema_follow_declared_formula` |
| raw %K60 + %D SMA3 | `src/research/dema_stoch/indicators.py:stochastic_d` | `test_stochastic_d_is_raw_k60_smoothed_by_sma3` |
| same-bar DEMA/%D 50 교차 | `src/research/dema_stoch/development.py:cross_side` | `test_cross_side_requires_same_bar_dema_and_stochastic_confirmation` |
| 다음 5분봉 시가·12봉 보유 | `src/research/dema_stoch/development.py:candidates_in_segment` | `test_candidate_uses_next_open_and_twelfth_held_close` |
| 완전 REGULAR 1분→5분 | `src/research/dema_stoch/loading.py:complete_regular_5m` | `test_incomplete_regular_grid_fails_closed_and_resets` |
| OOS performance gate | `src/research/dema_stoch/loading.py:load_symbol_day` | `test_performance_loader_is_the_only_candle_path` |
| long/short 비용·통계·D1~D9 | `src/research/dema_stoch/development.py` | `test_long_and_short_pnl_are_directionally_symmetric_before_cost` |
| 공식 단일 실행 CLI | `scripts/run_dema_stoch_development.py` | `test_official_runner_has_no_oos_unlock_or_direct_loader_import` |

실행기는 `producer_commit == HEAD`와 안정적인 PR 리뷰 URL을 요구한다. 구현 리뷰 승인 후에만
고정 snapshot의 development 구간을 한 번 실행한다. 결과 파일·forward return·blind OOS는 이
구현 커밋에서 만들거나 읽지 않는다.
