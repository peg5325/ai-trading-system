# P0-US30 Gap+RVOL D9 Toss repair candidate

> generated: `2026-08-04T01:41:12.334868+00:00`

## 판정

**PARTIAL_REPAIR_CANDIDATE**

- frozen targets: `76`
- full regular session: `66`
- required feature minutes complete: `66`
- incomplete and ineligible: `10`
- isolated candidate rows: `49150`
- HTTP 429 recovered: `3`

## 불변식

- canonical DB written: `False`
- frozen research snapshot written: `False`
- blind OOS opened: `False`
- performance / forward returns computed: `False`
- selection: tracked D9 exclusion event AND tracked probe availability

## 식별자

- repair data content: `c798b00b9d7f0caef142fc8c813ae4db4497129780801cc6906b01c564acea21`
- raw content: `7a55bd24deaaad9be9888a150e4aadb768dde6edfb0dead734e1fc01136d05da`
- raw files: `299`

## 불완전 후보

| day | symbol | regular | missing | first missing |
| --- | --- | ---: | ---: | ---: |
| 2024-07-23 | XLI | 389 | 1 | 570 |
| 2024-07-23 | XLU | 389 | 1 | 570 |
| 2024-07-23 | XLV | 389 | 1 | 570 |
| 2024-07-23 | XLY | 389 | 1 | 570 |
| 2024-07-24 | VST | 389 | 1 | 570 |
| 2024-07-24 | XLI | 389 | 1 | 570 |
| 2024-07-24 | XLU | 389 | 1 | 570 |
| 2024-07-29 | VST | 389 | 1 | 570 |
| 2024-08-05 | XLI | 389 | 1 | 570 |
| 2024-08-06 | XLI | 389 | 1 | 570 |

이번 산출물은 복구 후보 계층이다. full regular session을 충족한 66개 symbol-day만 별도 overlay 재감사 후보이며, 389/390인 10개는 결과에 사용하지 않는다. 이 단계 자체는 D9 재판정이나 경제 성과 판정이 아니다.
