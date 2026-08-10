# P0-US30 오전 강세·점심 눌림·오후 재상승 development 결과

> scope: **development-only** · blind OOS 미개봉
> payload SHA-256: `8d032db4607d5db6da997fc181b5b3609bc4c919bb7bf8d79a4bb162a6ad25bb`

## 결론

- verdict: **NO_SELECTION**
- failed gates: `['D1', 'D2', 'D3', 'D4', 'D6', 'D7']`
- candidates/trades: **116/111**

## 비용별 성과

| 비용 | 거래 | 거래일 | 누적 net | 기대값 | PF | MDD | 양수 fold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| low | 111 | 95 | -2476.977075 | -22.315109 | 0.698966 | 0.024949 | 4 |
| official | 111 | 95 | -6836.045874 | -61.585999 | 0.389260 | 0.068360 | 0 |
| stress | 111 | 95 | -14246.462831 | -128.346512 | 0.152186 | 0.142465 | 0 |

## 청산·포트폴리오

- exit reasons: `{'eod': 39, 'stop': 47, 'target': 25}`
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
