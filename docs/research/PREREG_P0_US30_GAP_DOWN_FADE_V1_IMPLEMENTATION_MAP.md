# 구현 매핑 — P0-US30 큰 갭하락 후 VWAP 회복 v1

> 사전등록 커밋 `df2296f` 이후 작성. 성과 실행 전 매핑.

| 사전등록 | 구현 |
|---|---|
| 계약 해시·development 날짜·10개 fold | `src/research/gap_down_fade/contract.py` |
| 고정 1분봉 4중 identity | `src/research/orb/identity.py`, `development._verify_data_identity` |
| 일봉 content hash·행수 검증 | `development._verify_data_identity`, `src/history/daily_manifest.py` |
| blind OOS 잠금 일봉·1분봉 로드 | `loading.load_daily_closes`, `loading.load_symbol_day` |
| 완전 REGULAR 1분봉→5분봉 | `loading.complete_regular_5m` |
| RSI·ATR·VWAP·중앙값 | `indicators.py` |
| gap/opening selloff/volume/RSI/VWAP higher-low 신호 | `development.detect_candidate` |
| next-bar entry·stop-first·1.5R·EOD flat | `development.close_candidate` |
| 고정 위험·notional·동시/가족 cap·순위 | `development._allocate` |
| 비용 3수준·10 fold·D1~D9 | `development._stats`, `development._gate` |
| 결정론 JSON·Markdown | `development.write_result`, `report.render` |

## 실행 전 검증

- `tests/test_gap_down_fade.py`가 계약 잠금, OOS 경계, 지표, 완전 세션, 전일 종가 기반 gap,
  next-bar entry, stop-first, 데이터/경제성 판정을 고정한다.
- repository OOS wiring 테스트가 신규 성과 로더의 `research_load` 경유를 검사한다.
- canonical DB, snapshot, raw store는 read-only이며 live/Paper/order/risk/config를 수정하지 않는다.

## 병합 전 구현 감사

- 최초 결과 보존 뒤 canonical 5분봉의 시작시각을 완료시각으로 직접 비교한 신호창 경계 오독을
  발견했다. 사전등록 규칙은 바꾸지 않고 `start + 5분` 완료시각으로 구현을 교정했다.
- 교정 전 성과값을 이미 확인했으므로 결과의 `execution_audit`에 이를 명시하고, 같은 잠긴 계약으로
  결과를 다시 생성한다. blind OOS는 계속 미개봉이다.
