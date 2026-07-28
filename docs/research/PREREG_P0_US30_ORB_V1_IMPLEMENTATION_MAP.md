# P0-US30 ORB 사전등록 ↔ 구현 매핑

이 문서는 동결된 `PREREG_P0_US30_ORB_V1.md` §15의 실행 전 체크리스트를
별도 tracked 문서로 완성한다. 정본 사전등록 본문은 개서하지 않는다.

| 조항 | 내용 | 구현 file:symbol | 테스트 | 출력 필드 |
| --- | --- | --- | --- | --- |
| §2 | snapshot identity fail-closed | `src/research/orb/identity.py:require_identity` | `tests/test_orb_identity.py:test_any_hash_mismatch_fails_ok` | `snapshot.*` |
| §3.1 | development/OOS 경계 | `src/research/orb/windows.py:development_eval_days` | `tests/test_orb_windows.py:test_development_ends_before_oos_starts` | `window.*` |
| §3.2 | 10 fold 경계 | `src/research/orb/windows.py:folds` | `tests/test_orb_windows.py:test_folds_tile_the_eval_window_without_gap_or_overlap` | `folds[]` |
| §3.3 | embargo 계약 | `src/research/orb/windows.py:folds` | `tests/test_orb_windows.py:test_folds_match_contract_boundaries_exactly` | `embargo` |
| §3.4 | 조기폐장·휴장 | `src/research/orb/windows.py:regular_close_minute_checked` | `tests/test_orb_windows.py:test_regular_close_minute_checked_normal_and_early_close` | `early_close_days[]` |
| §4 | primary/sensitivity 권한 | `src/research/orb/contract.py:all_specs` | `tests/test_orb_contract.py:test_all_specs_are_exactly_eight_with_correct_authority` | `spec` |
| §5.1 | gap 정의·임계 | `src/research/orb/signals.py:is_strategy_candidate` | `tests/test_orb_signals.py:test_candidate_requires_data_eligibility_and_both_thresholds` | `gap` |
| §5.2 | RVOL 정의·임계 | `src/research/orb/features.py:RvolWindow` | `tests/test_orb_features.py:test_rvol_denominator_excludes_incomplete_lookback_days_and_changes_value` | `rvol` |
| §5.5 | 후보 제외 사유 집계 | `src/research/orb/simulator.py:simulate_day` | `tests/test_orb_simulator.py:test_feature_exclusion_reasons_are_preserved_for_defect_aggregation` | `excluded[]` |
| §5.6 | family 합산 | `src/research/orb/stats.py:compute_stats` | `tests/test_orb_stats.py:test_family_concentration_aggregates_qqq_family` | `family` |
| §6.1 | opening range | `src/research/orb/features.py:opening_range_for_spec` | `tests/test_orb_features.py:test_opening_range_hand_calculated_high_low` | `or_high`,`or_low` |
| §6.3 | 돌파 정의(close, buffer 0) | `src/research/orb/signals.py:detect_breakout` | `tests/test_orb_signals.py:test_breakout_is_strict_and_cannot_fire_inside_opening_range` | `signal_bar` |
| §6.4 | LONG 전용·인버스 경로 | `src/research/orb/portfolio.py:size_position` | `tests/test_orb_portfolio.py:test_family_cap_includes_same_bar_accepted_candidate` | `direction` |
| §6.5 | next-bar 체결·최대 진입 시각 | `src/research/orb/simulator.py:simulate_day` | `tests/test_orb_simulator.py:test_next_bar_fill_and_entry_bar_is_scanned_for_stop` | `entry_bar`,`entry_price` |
| §7 | 정렬 key·tie-break·한도 | `src/research/orb/portfolio.py:allocate_entries` | `tests/test_orb_signals.py:test_rank_is_rvol_then_abs_gap_then_ascii_symbol` | `rank`,`skipped_reason` |
| §7 | skipped_reason 폐쇄 어휘·binding cap | `src/research/orb/portfolio.py:SKIPPED_REASONS` | `tests/test_orb_portfolio.py:test_skip_vocabulary_is_exactly_five` | `skipped_reason`,`binding_cap` |
| §8.1~8.3 | 1R·stop·target·sizing | `src/research/orb/portfolio.py:size_position` | `tests/test_orb_portfolio.py:test_sizing_caps_instead_of_blocking_and_reports_binding_cap` | `stop`,`target`,`qty` |
| §8.4 | 동일봉 충돌 보수적 | `src/research/orb/execution.py:evaluate_exit` | `tests/test_orb_execution.py:test_same_bar_stop_beats_target` | `exit_reason` |
| §8.7 | EOD flat | `src/research/orb/simulator.py:simulate_day` | `tests/test_orb_simulator.py:test_eod_normal_day_and_early_close_use_exchange_calendar` | `eod_*` |
| §8.8 | stale/결측 | `src/research/orb/simulator.py:simulate_day` | `tests/test_orb_simulator.py:test_three_missing_expected_bars_force_stale_exit_at_next_open` | `stale_exit`,`no_regular_data` |
| §9 | 비용 3수준 | `src/research/orb/costs.py:all_costed_pnls` | `tests/test_orb_execution.py:test_costs_are_three_levels_and_not_double_charged` | `cost_level`,`fees` |
| §10 | development 게이트 | `src/research/orb/gates.py:evaluate_development` | `tests/test_orb_gates.py:test_all_development_boundaries_pass_and_open_only_next_stage` | `dev_gate.*` |
| §11 | OOS 게이트 | `src/research/orb/gates.py:evaluate_oos` | `tests/test_orb_gates.py:test_oos_gate_is_pure_and_does_not_load_data` | `oos_gate.*` |
| §12 | 최종 상태 5종 | `src/research/orb/gates.py:GateDecision` | `tests/test_orb_gates.py:test_precondition_failure_is_not_strategy_failure` | `verdict` |
| §3.5 | 로더 배선·직접 로드 금지 | `src/research/orb/loading.py:load_symbol_day` | `tests/test_orb_loading.py:test_research_load_calls_never_pass_unlock_and_use_intraday_track` | `loader` |
| §8.0 | 계좌 모형·fixed base·곡선 연속성 | `src/research/orb/stats.py:compute_stats` | `tests/test_orb_stats.py:test_stats_curve_includes_t0_and_no_trade_days_and_folds_stay_continuous` | `account.*` |
| §8.4·§8.5 | 청산 우선순위·touch/gap-through·5분 | `src/research/orb/execution.py:evaluate_exit` | `tests/test_orb_execution.py:test_gap_through_stop_and_target_fill_at_open` | `exit_reason`,`exit_price` |
| §10.3 | PF·expectancy·MDD·거래일·집중도 | `src/research/orb/stats.py:compute_stats` | `tests/test_orb_stats.py:test_mdd_uses_running_peak_and_t0_initial_capital` | `stats.*` |
| §10.4 | 비-primary 파라미터화 영구 실격 | `src/research/orb/gates.py:disqualified_non_primary_specs` | `tests/test_orb_gates.py:test_oos_months_use_frozen_anchor_boundaries_and_non_primary_are_disqualified` | `disqualified[]` |
| §11.0 | OOS 6개월 앵커 구간 | `src/research/orb/gates.py:oos_months` | `tests/test_orb_gates.py:test_oos_months_use_frozen_anchor_boundaries_and_non_primary_are_disqualified` | `months[]` |
| §11.1 | 단일 실행·변형 독립성·8개 전량 | `src/research/orb/simulator.py:simulate_all_specs` | `tests/test_orb_simulator.py:test_all_eight_specs_are_simulated_as_independent_result_streams` | `single_pass` |
| §7 | 같은 봉 처리 순서(청산→진입) | `src/research/orb/simulator.py:simulate_day` | `tests/test_orb_simulator.py:test_same_bar_exit_releases_capacity_before_new_entry` | `fill_order` |
| 5/6 | unlock artifact 필드 | `src/history/oos_guard.py:OOSUnlock` | `tests/test_oos_guard_wiring.py:test_eval_slices_wired_through_guarded_loader` | `unlock.*` |
