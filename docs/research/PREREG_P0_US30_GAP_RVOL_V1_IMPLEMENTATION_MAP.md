# Gap+RVOL v1 사전등록 ↔ 구현 매핑

> 이 문서는 이슈 #100의 **구현·테스트 증거**다. 실제 development snapshot,
> 전략 성과, forward return, blind OOS는 읽거나 실행하지 않았다. 공식 development
> runner와 RESULT는 이 구현이 main에 병합된 뒤 별도 이슈에서 만든다.

## 1. 동결 계약

- Markdown normalized SHA-256:
  `88543b8bf30440751ab5af00d979dd39f0dfd7ac13100ea445b2c537849ee50e`
- JSON canonical SHA-256:
  `a181799c7301e09d49d90482975e8a0c86deb60ca216e6b9f900abc8bf73e29a`
- 로더: `src/research/gap_rvol/contract.py`
- 4-hash 입력 검증: `src/research/gap_rvol/identity.py`
- 690/670/125일·10-fold 경계: `src/research/gap_rvol/windows.py`
- 증거: `tests/test_gap_rvol_contract.py`

## 2. §14 최소 구현 매핑

| 사전등록 요구 | 구현 | 회귀 증거 |
|---|---|---|
| gap/RVOL/VWAP 손계산 | `features.compute_day_features`, `causal_vwap_by_bar` | `test_gap_rvol_features_signals.py` |
| VWAP causal 음성 테스트 | 누적 1분봉을 현재 완료 5분봉까지만 합산 | `test_causal_vwap_does_not_see_future_minutes` |
| pullback armed, same-bar trigger 금지 | `signals.detect_signal`의 `was_armed` 상태 | `test_armed_bar_cannot_trigger_and_reclaim_is_strict` |
| retention equality/붕괴 | `close < retention_floor`만 붕괴 | `test_retention_equality_holds_but_break_invalidates_day` |
| 직전 봉 high strict reclaim | `close > previous.high` | strict equality 회귀 |
| next-bar fill | `GapRvolSignal.fill_minute = signal + 5` | signal·simulator 회귀 |
| pullback-low stop | armed 이후 signal까지 최소 low | portfolio stop/2R 회귀 |
| 60분 time stop | 예정 시각 `entry_minute + 60` | `test_next_bar_fill_entry_bar_scan_and_scheduled_60_minute_stop` |
| EOD/조기폐장 | exchange calendar의 일별 close minute 사용 | simulator의 `regular_close_minute_checked` |
| 입력 순서·중복 불변 | 분 단위 canonicalization, 충돌 fail-closed | shuffle/identical duplicate 회귀 |
| 동시 후보 경쟁 | RVOL↓, gap↓, ASCII symbol↑, family/slot cap | portfolio 경쟁 회귀 |
| development OOS 동적/AST gate | 유일한 로더가 `research_load(... purpose=\"performance\", track=intraday)` | `test_gap_rvol_loading_and_map.py` |

## 3. 공통 계약 재사용 경계

- 재사용: ORB와 동일하게 동결된 캘린더·fold, 3수준 비용, 4-hash identity,
  통계 자료구조.
- 독립 구현: Gap+RVOL 피처, armed/reclaim 신호, pullback-low sizing, 예정
  60분 청산, 순수 event-time simulator, D1~D9 verdict 분기.
- 금지: ORB RESULT를 입력으로 사용하거나 ORB 파라미터를 Gap+RVOL에 재조정.

## 4. 판정 경계

- D1~D8 경제·통계 실패: `NO_SELECTION`.
- D9 데이터·운영 결함: `INCONCLUSIVE_DATA`.
- P1~P5 절차 전제 미충족: `NOT_RUN`.
- 이 PR은 어떤 판정도 실행하지 않는다. 후속 공식 development 실행은 이
  구현 merge commit, 독립 review evidence, snapshot 4-hash를 별도로 고정해야 한다.
