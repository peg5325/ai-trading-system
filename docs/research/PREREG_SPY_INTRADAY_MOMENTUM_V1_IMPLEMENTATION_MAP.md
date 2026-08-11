# 구현 매핑 — SPY 첫 30분 → 마지막 30분 장중 모멘텀 v1

> 사전등록 커밋: `cdcacb4` · 결과 실행 전 작성

| 사전등록 항목 | 구현 | 회귀 증거 |
|---|---|---|
| §2 identity·development/OOS 경계 | `contract.py`, `development._verify_data_identity`, `loading.py`의 `research_load(... purpose="performance")` | 계약/OOS 제외 테스트, 저장소 전역 OOS wiring 테스트 |
| §3 완전 REGULAR 1분봉 | `loading.complete_regular_1m` | 정상 390분·결측 1분 제외 테스트 |
| §3 첫 30분 수익률 부호 | `development.trade_for_day`, `signal_direction` | LONG·SHORT·ZERO 테스트 |
| §3 마지막 30분 진입·종가 청산 | `development.trade_for_day` | 15:30 시가·15:59 종가 테스트 |
| §3 조기폐장 | `regular_close_minute_checked(day)-30/-1` | 2025-07-03 12:30/12:59 테스트 |
| §4 고정 $100,000·정수 수량 | `trade_for_day` | 진입가별 `floor` 수량 테스트 |
| §4 비용 세 수준 | `net_pnl`, `_stats` | 진입·청산 양쪽 notional 비용 테스트 |
| §5 10개 fold·D1~D9 | `contract.fold_boundaries`, `development._gate` | 경제 실패·품질 실패 판정 테스트 |
| §6 방향별·비용별 공개 | `development.run`, `report.render` | 결과 JSON/Markdown 단일 renderer |

구현은 사전등록 숫자를 계약 JSON에서 읽는다. 별도 sensitivity, 성과 기반 필터, OOS unlock은 없다.

## 병합 전 구현 감사

- 전체 intraday snapshot은 file SHA와 tracked 생성 receipt로 인증해 OOS OHLCV를 역직렬화하지 않는다.
  전체 일봉 계약은 tracked manifest로 확인하고 live DB content hash는 development 종료일
  `2026-01-20`까지만 계산한다(`0550100c…`, 72,940행).
- 마지막 1분봉 청산 timestamp는 canonical 시작 라벨에 1분을 더한 완료시각으로 기록한다.
- 최초 결과 확인 뒤 발견된 구현·증거 경계 교정이며, 신호·진입가·청산가·비용·게이트는 바꾸지 않는다.
  결과의 `execution_audit`에 성과 사전열람과 재생성을 공개한다.
