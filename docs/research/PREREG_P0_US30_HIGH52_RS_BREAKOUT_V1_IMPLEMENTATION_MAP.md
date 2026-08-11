# P0-US30 52주 고점·상대강도 돌파 v1.0 구현 매핑

> 상태: **development 결과 미실행·미열람.** 이 문서는 사전등록
> `PREREG_P0_US30_HIGH52_RS_BREAKOUT_V1.{md,json}`의 규칙을 코드와 테스트에 연결한다.
> 이 문서와 구현을 먼저 커밋한 뒤에만 development 백테스트를 한 번 실행한다.

## 1. 데이터 identity와 OOS 경계

| 사전등록 | 구현 | 회귀 증거 |
|---|---|---|
| §2 P0 26종목 | `loading.official_symbols` | `test_contract_and_official_universe_are_locked` |
| §2 development 반개구간 | `loading.load_development_bars` | `test_development_loader_uses_swing_half_open_range_without_grant` |
| §2 `TRACK_SWING`, grant 없음 | `loading.load_development_bars` 및 전역 AST 가드의 단일 파일 허용 | 위 테스트, `tests/test_orb_loading.py` |
| §2 manifest identity | 실행기 `build_manifest` 비교 | 실행 시 DQ2 및 결과의 data SHA-256 |

## 2. 지표·신호·횡단면

| 사전등록 | 구현 | 회귀 증거 |
|---|---|---|
| §3 ATR14/SMA200/high252/return63 | `development.build_features` | `test_feature_windows_match_hand_calculation` |
| §3 현재일 제외 prior20, first breakout | `development.build_features` | `test_first_breakout_excludes_current_bar_and_blocks_consecutive_signal` |
| §4 strict SMA, proximity 0.97 | `development.is_signal` | `test_signal_boundaries` |
| §4 최소 20·ceil(20%)·동점 | `development.prepare_candidates` | `test_cross_section_*` |
| §4 입력 순서 불변 | 후보 정렬 key | `test_cross_section_uses_ceil_top_20_percent_and_is_input_invariant` |

## 3. 체결·포트폴리오·손익

| 사전등록 | 구현 | 회귀 증거 |
|---|---|---|
| §5 다음 시가·3/5 거래일 종가 | `prepare_candidates`, `simulate` | `test_next_open_entry_and_fifth_day_close_exit`, `test_holding_period_counts_market_days_across_weekend` |
| §5 최대 5·상대강도 우선 | `simulate` | `test_capacity_uses_relative_strength_rank` |
| §5 gap→신규→intraday→time | `simulate` | `test_gap_and_intraday_stop_precede_time_exit` |
| §5 결측 fail-closed | `prepare_candidates` | `test_missing_holding_bar_is_counted_fail_closed` |
| §6 비용 1회 | `costed_pnl` | `test_round_trip_cost_is_charged_once` |
| §7 MTM MDD·연속손실 | `summarize` | `test_mark_to_market_drawdown_and_loss_streak` |
| §7 연속 10 fold | `chronological_blocks`, `_fold_pnls` | `test_chronological_blocks_cover_calendar_once` |

## 4. 판정·결정론

- `development.run_development`가 DQ1~DQ3 및 D1~D8을 JSON boolean으로 보존한다.
- DQ 실패는 `INCONCLUSIVE_DATA`, DQ 통과 후 경제 게이트 실패는 `NO_SELECTION`, 전부 통과는
  `DEVELOPMENT_GO`로만 판정한다.
- `contract.canonical_json_bytes`, 거래 digest, `report.render_markdown`은 결정론적으로 출력하며
  `test_json_and_markdown_are_deterministic`가 형식을 고정한다.
- 실행기는 development 결과만 생성한다. blind OOS grant 생성·전달·열람 경로는 없다.

## 5. 실행 규율

1. 이 구현 매핑과 코드·테스트를 결과 전에 커밋·원격 보존한다.
2. 잠긴 JSON 계약으로 development 실행을 한 번 수행한다.
3. 동일 명령 재실행 산출물의 byte identity를 확인한다.
4. 결과가 부진해도 규칙을 변경하지 않는다. 사전등록 §10의 invalidation-grade 결함만 수정할 수
   있으며, 발견 시 감사 이력을 결과 문서에 남긴다.
