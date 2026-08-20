# P0-US30 박스 돌파 + 오더블록 리테스트 v1.2 구현 매핑

> 이슈 #142 · 결과 실행 전 구현 증거 · blind OOS 미개봉

| 계약 | 구현 | 회귀 테스트 |
|---|---|---|
| prereg hash·적응성/OOS | `contract.py`, `loading.py` | contract/loader tests |
| 유효 OHLC·TR·ATR·EMA/SMA·reset | `development.py:build_features` | feature readiness tests |
| 20일 박스·추세·volume 돌파 | `breakout_signal` | strict/inclusive boundary tests |
| bearish engulfing order-block | `breakout_signal` | order-block tests |
| 3일 최초 리테스트·무효화 | `prepare_arms` | retest/invalidation tests |
| base/primary/partial 고정 | `prepare_arms`, `simulate` | arm mapping tests |
| stop/target/time-exit 우선순위 | `simulate` | conservative path tests |
| risk quantity·동시 5포지션 | `simulate` | allocation tests |
| 비용·PF·MDD·expectancy/R | `episode_net_pnl`, `summarize` | metric tests |
| exact fold·DQ·verdict | `evaluate_arm`, `run_development` | fold/gate tests |
| 결정론 JSON/Markdown | runner, `report.py` | artifact mapping tests |

공식 실행기는 `producer_commit == HEAD`와 안정적인 PR review comment URL을 요구한다. 구현 리뷰
승인 전에는 result 파일을 만들지 않는다.
