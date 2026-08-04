# P0-US30 Gap+RVOL D9 defect-only 감사

## 판정

**INCONCLUSIVE_DATA**

- blind OOS opened: `False`
- performance computed: `False`
- strategy contract changed: `False`

## 충돌 기준선 감사

- frozen manifest conflict total: `28`
- pre-result baseline total: `28`
- pre-result baseline by symbol: `{"NVDA": 5, "QQQ": 7, "SOXL": 12, "SPY": 3, "VST": 1}`
- snapshot promotion conflict candles: `1` (manifest 28건 중 부분집합)
- new conflict total: `0`
- raw major defect events: `0`
- corrected major defect count: `0`
- baseline source: `docs/research/PREREG_P0_US30_ORB_V1.json@17681a7c1b893f035a7c4f645e08d98734e00fec`

원 실행은 결과 전에 이미 판정된 충돌 기준선 28건을 새 중대 결함으로 다시 더했다. 감사에서는 새 충돌만 무효화 대상으로 계산한다.

## 데이터 제외 감사

- excluded: `1296 / 20100`
- ratio: `0.06447761` (threshold `<= 0.05`)
- reason counts: `{"incomplete_decision_bar": 734, "incomplete_rvol_window": 708, "insufficient_rvol_lookback": 566, "no_previous_regular_close": 723}`
- fully excluded market days: `["2024-08-01", "2024-08-02", "2024-08-05"]`

기준선 중복계산을 바로잡아도 데이터 제외율이 5%를 넘으므로 D9는 계속 실패한다. 고정 스냅샷에서 결측된 과거 1분봉 구간을 별도 검증 데이터로 복구하기 전에는 OOS나 다음 전략 후보로 진행하지 않는다.
