# P0-US30 오전 강세·점심 눌림·오후 재상승 development 결과

> scope: **development-only** · blind OOS 미개봉
> payload SHA-256: `fcac5096c21961c05d1ffc5db476bd98f23f78506cfc192d4f8c5cde7a98711b`

## 결론

- verdict: **NO_SELECTION**
- failed gates: `['D1', 'D2', 'D3', 'D4', 'D6', 'D7']`
- candidates/trades: **115/110**

## 비용별 성과

| 비용 | 거래 | 거래일 | 누적 net | 기대값 | PF | MDD | 양수 fold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| low | 110 | 94 | -2275.752951 | -20.688663 | 0.716488 | 0.024033 | 4 |
| official | 110 | 94 | -6595.071749 | -59.955198 | 0.397825 | 0.065951 | 0 |
| stress | 110 | 94 | -13937.913705 | -126.708306 | 0.155033 | 0.139379 | 0 |

## 청산·포트폴리오

- exit reasons: `{'eod': 39, 'stop': 46, 'target': 25}`
- portfolio skips: `{'family_cap': 1, 'qty_lt_1': 4}`

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
- 2026-01-21 이후 blind OOS를 로드하지 않았다.
- 결과 후 임계값·목표·손절을 수정하지 않았다.
- Paper·실거래 승격 결과가 아니다.

## 실행 감사

- post_result_merge_review: `signal_completion_window_compared_against_start_label` → `compare_canonical_5m_start_label_plus_5_minutes`; 성과 사전열람=True, 전략 규칙 변경=False
