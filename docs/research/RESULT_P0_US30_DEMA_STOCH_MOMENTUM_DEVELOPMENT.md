# P0-US30 DEMA(60) + Stochastic %D(60) development 결과

> scope: **development-only** · OOS identity 편차 정정본
> payload SHA-256: `4341a7347bbc7040b8cfbe58587668992202d4ae7ea98451e20dd00124383428`

## 결론

- verdict: **NO_SELECTION**
- failed gates: `['D3', 'D4', 'D5', 'D6', 'D7']`
- candidates/trades: **4121/3715**
- direction: `{'LONG': 2031, 'SHORT': 1684}`

## Primary combined 비용별 성과

| 비용 | 거래 | 거래일 | 누적 net | 기대값 | PF | 승률 | MDD | 양수 fold |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| low | 3715 | 647 | -89133.464577 | -23.992857 | 0.6643695465496068 | 37.2005% | 90.1748% | 0/10 |
| official | 3715 | 647 | -236702.911530 | -63.715454 | 0.3581859395891475 | 23.9300% | 236.7029% | 0/10 |
| stress | 3715 | 647 | -487570.971349 | -131.243868 | 0.15121332022460338 | 13.3513% | 487.5710% | 0/10 |

## 방향별 official 성과

| 방향 | 거래 | 누적 net | 기대값 | PF | 승률 |
| --- | ---: | ---: | ---: | ---: | ---: |
| LONG | 2031 | -125905.392849 | -61.991823 | 0.3579134989114987 | 23.8306% |
| SHORT | 1684 | -110797.518681 | -65.794251 | 0.35849524861101967 | 24.0499% |

## 데이터 품질

- incomplete symbol-days: `734 / 20100 = 3.651741%`
- new conflicts: `0`
- major defects: `0`

## 해석 경계

- 사진에 없던 청산은 결과 전에 60분 고정 보유로 잠갔다.
- MDD는 초기자본 기준 고정 notional을 계속 적용한 연구 손익곡선이라 자본 소진 뒤에도 100%를 넘을 수 있으며, 실계좌 운용 가능성을 뜻하지 않는다.
- SHORT는 borrow/locate/borrow fee를 모델링하지 않은 연구용 수치다.
- 최초 identity 검사는 blind OOS OHLCV를 기계적으로 읽었지만 성과를 계산·출력·사람 열람하지 않았다.
- 교정 실행은 blind OOS OHLCV를 읽지 않았다.
- prereg compliance: `DEVIATION_ACCEPTED_BY_APPEND_ONLY_ERRATUM`.
- erratum: `docs/research/ERRATUM_P0_US30_DEMA_STOCH_OOS_IDENTITY.md`.
- 외부 공급자·Paper·실거래·주문·위험·config 변경은 없다.

## 병합 전 실행 감사

- 결함: `full_snapshot_content_hash_decoded_blind_oos_ohlcv`
- 수정: `snapshot_file_sha_plus_tracked_content_receipt`
- 전략 규칙 변경: `false`
- 결과 재생성: `true`
