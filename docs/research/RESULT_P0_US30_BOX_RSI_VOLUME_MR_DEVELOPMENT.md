# P0-US30 박스권 RSI·거래량 평균회귀 development 결과

> scope: **development-only** · blind OOS 미개봉
> payload SHA-256: `5ee7ae033102f05a577a02e4a9cbd94751bb42ccd82e41baf322a03d6275069e`

## 결론

- verdict: **NO_SELECTION**
- failed gates: `['D1', 'D2', 'D3', 'D4', 'D6', 'D7']`
- trades: **97**

## 비용별 성과

| 비용 | 거래 | 거래일 | 누적 net | 기대값 | PF | MDD | 양수 fold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| low | 97 | 82 | -2466.810688 | -25.431038 | 0.056828 | 0.025051 | 1 |
| official | 97 | 82 | -6326.589035 | -65.222567 | 0.000000 | 0.063266 | 0 |
| stress | 97 | 82 | -12888.212225 | -132.868167 | 0.000000 | 0.128882 | 0 |

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
