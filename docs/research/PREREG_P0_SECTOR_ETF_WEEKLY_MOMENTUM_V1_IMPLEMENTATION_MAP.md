# P0 섹터 ETF 주간 상대강도 로테이션 v1.0 구현 매핑

> 상태: **development 결과 미실행·미열람.** 사전등록 MD/JSON과 이 구현을 먼저 커밋한 뒤에만
> development를 한 번 실행한다. swing blind OOS는 닫혀 있다.

## 1. 데이터·시간 경계

| 사전등록 | 구현 | 회귀 증거 |
|---|---|---|
| §2 `class=sector_etf` 8종목 | `loading.sector_symbols` | `test_contract_and_exact_sector_universe_are_locked` |
| §2 development 반개구간 | `loading.load_development_bars` | `test_development_loader_uses_swing_half_open_range_without_grant` |
| §2 TRACK_SWING·grant 없음 | 단일 loader + 전역 AST exact-file 허용 | 위 테스트, `tests/test_orb_loading.py` |
| §2 manifest identity | 실행기의 `build_manifest` 비교 | 실행 시 DQ2와 결과 data SHA-256 |

## 2. 신호·주간 일정

| 사전등록 | 구현 | 회귀 증거 |
|---|---|---|
| §3 최근 5일 제외 63/126 수익률 | `development.build_features` | `test_skipped_returns_and_sma_match_hand_calculation` |
| §3 ISO 주 마지막 실제 거래일 | `weekly_signal_trade_pairs` | `test_iso_week_uses_last_actual_trading_day_across_holiday` |
| §3 SPY SMA200 strict | `prepare_targets` | `test_spy_sma_regime_is_strict` |
| §3 score>0, top2, symbol tie | `prepare_targets` | `test_absolute_score_zero_is_ineligible`, `test_top_two_tie_break_and_next_open_are_locked` |
| primary/sensitivity 분리 | `_score` | `test_primary_and_sensitivity_scores_are_independent_locked_specs` |

## 3. 포트폴리오·turnover·비용

| 사전등록 | 구현 | 회귀 증거 |
|---|---|---|
| §4 다음 open, sell-before-buy | `simulate` | target·regime 테스트 |
| §4 유지 종목 무조정 | `simulate` | `test_retained_positions_have_zero_extra_turnover_and_cost` |
| §4 25k×2 = 6.25k×8 | `simulate`, `equal_all_events` | `test_equal_all_uses_same_fifty_thousand_total_entry_exposure` |
| §4 regime off 현금 | `simulate` | `test_regime_off_sells_before_remaining_in_cash` |
| §4 마지막 close 청산 | `simulate` | `test_development_boundary_liquidates_at_close` |
| §5 실제 양방향 turnover만 편도 비용 | `simulate` | retained/cost-level 테스트 |
| §5 비용 간 gross path 동일 | `_run_levels` | `test_cost_levels_preserve_identical_gross_episode_path` |

## 4. 판정·결정론

- `run_development`는 primary, 126일 sensitivity, 동일 regime equal-all을 같은 데이터 경계에서
  실행하고 DQ1~DQ3·D1~D9를 boolean으로 보존한다.
- `_fold_comparison`은 10개 독립 연속 블록에서 primary와 equal-all을 각각 빈 포지션으로
  재실행하고 마지막 close에 청산한다.
- `summarize`는 episode PF·실제 turnover·비용·mark-to-market MDD·종목 기여도를 계산하며
  관련 합성 회귀가 수식을 고정한다.
- `canonical_json_bytes`, gross episode digest, `render_markdown`은 결정론적이다.

## 5. 결과 전 실행 규율

1. 이 구현·테스트·매핑을 사전등록과 별도 커밋으로 원격 고정한다.
2. 개발 구간 명령을 한 번 실행한다.
3. 동일 명령 재실행의 JSON·Markdown byte identity를 확인한다.
4. 성과 부진은 규칙 수정 사유가 아니다. 사전등록 §9의 invalidation-grade 결함만 수정할 수 있다.
