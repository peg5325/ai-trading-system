# P0-US30 장 마감 과잉압력 반전 v1.0 구현 매핑

> 상태: **development 결과 미열람.** 사전등록 커밋 `0f0d41f` 이후 구현되었으며, 이 문서와
> 구현 커밋을 원격에 고정한 뒤에만 단일 development 실행을 허용한다.

## 1. 사전등록 → 구현

| 사전등록 | 구현 위치 | 고정된 의미 |
|---|---|---|
| §2 데이터 identity | `scripts/run_close_pressure_reversal_development.py` | 일봉 manifest SHA-256·30종목 OK·as-of 초과 0을 실행 전에 검증 |
| §2 OOS 경계 | `src/research/close_pressure_reversal/loading.py` | swing `research_load`, development 반개구간, grant 없음 |
| §2 공식 26종목 | `loading.official_symbols` | leveraged/inverse class 제외, YAML 순서 보존 |
| §3 ATR·신호 | `development.build_features`, `is_signal` | 현재일 포함 TR 14개 산술평균, -1%·close-location 0.20 inclusive |
| §3 순위 | `development.prepare_candidates`, `simulate` | pressure 내림차순 → 당일수익 오름차순 → symbol |
| §4 체결·수량 | `development.simulate` | 다음 거래일 시가, 5 slots, $10k/$500 위험예산, 정수 수량 |
| §4 청산 | `development.simulate` | gap stop → 신규 진입 → intraday stop → time exit 순서 |
| §5 비용 | `development.costed_pnl`, `summarize` | entry notional에 왕복 비용 1회, 체결 경로는 비용 간 동일 |
| §6 MDD·일별 equity | `development.summarize` | 비용은 entry일 차감, 보유 중 종가 mark-to-market, exit일 실현 |
| §6 fold | `development.chronological_blocks`, `_fold_pnls` | 연속 10블록, 진입·전체 보유 경로가 블록 안인 거래만 독립 재실행 |
| §6 보고 | `development.run_development`, `report.render_markdown` | 비용별 지표·연속손실·보유기간·기여도·일별 equity·거래 digest |
| §7 판정 | `development.run_development` | DQ 선행, D1~D8 전부 통과만 DEVELOPMENT_GO |
| §9 결정론 | `contract.canonical_json_bytes`, `report.render_markdown` | 정렬된 JSON과 고정 순서 Markdown |

## 2. 필수 회귀 증거

`tests/test_close_pressure_reversal.py`가 다음을 고정한다.

1. 계약·26종목 유니버스
2. ATR14 warm-up 및 현재일 포함
3. 신호 경계와 0-range 거부
4. 후보 순위·symbol tie-break·입력 순서 불변
5. 누락 holding bar fail-closed와 정확한 5% 제외율 경계
6. 다음 시가 진입·3거래일/1거래일 time exit·주말 비거래일 처리
7. gap stop·intraday stop·time exit 우선순위
8. 최대 5포지션과 결정론적 후보 선택
9. 비용 1회 차감
10. 10개 연속 fold의 완전·비중복 분할
11. 보유 중 mark-to-market MDD, 연속손실, 비용 반영 기여도
12. development 반개구간·swing track·grant 없음
13. JSON·Markdown 결정론

## 3. 실행 규율

- 실행 입력 DB는 추적 일봉 manifest와 동일한 canonical DB여야 한다.
- 출력은 `RESULT_P0_US30_CLOSE_PRESSURE_REVERSAL_DEVELOPMENT.{json,md}` 한 쌍이다.
- 동일 커밋·동일 DB 재실행 결과는 byte-identical이어야 한다.
- 결과가 좋거나 나쁘다는 이유로 사전등록 또는 구현을 바꾸지 않는다.
- blind OOS는 별도 사전등록·리뷰·unlock 없이는 열지 않는다.
