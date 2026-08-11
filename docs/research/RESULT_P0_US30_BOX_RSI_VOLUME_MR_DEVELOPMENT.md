# P0-US30 박스권 RSI·거래량 평균회귀 development 결과

> scope: **development-only** · blind OOS 미개봉
> payload SHA-256: `c83ca6d846d0b8156be8e688e0e18d271dcb40e5095cf940e8f56911137fe14b`

## 결론

- verdict: **NO_SELECTION**
- failed gates: `['D1', 'D2', 'D3', 'D4', 'D6', 'D7']`
- trades: **92**

## 비용별 성과

| 비용 | 거래 | 거래일 | 누적 net | 기대값 | PF | MDD | 양수 fold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| low | 92 | 79 | -2368.675585 | -25.746474 | 0.059044 | 0.024070 | 1 |
| official | 92 | 79 | -6029.556741 | -65.538660 | 0.000000 | 0.060296 | 0 |
| stress | 92 | 79 | -12253.054707 | -133.185377 | 0.000000 | 0.122531 | 0 |

## 게이트

| 게이트 | 통과 |
| --- | --- |
| D1 | False |
| D2 | False |
| D3 | False |
| D4 | False |
| D5 | True |
| D6 | False |
| D7 | False |
| D9 | True |

## 데이터 품질

- incomplete symbol-days: `962/20100 = 4.786070%`
- new conflicts: `0`
- major defects: `0`

## 실행 규율

- 사전등록 primary 한 개만 실행했다.
- 2026-01-21 이후 blind OOS는 로드하지 않았다.
- 이 결과는 Paper·실거래 승격이 아니다.
- 결과 후 임계값이나 규칙을 수정하지 않았다.

## 실행 감사

- post_result_independent_review: `signal_window_compared_against_start_label_instead_of_completion_time` → `compare_canonical_5m_start_label_plus_5_minutes`; 성과 사전열람=True, 전략 규칙 변경=False
