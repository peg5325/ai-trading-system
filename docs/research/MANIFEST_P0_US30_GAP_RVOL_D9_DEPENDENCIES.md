# P0-US30 Gap+RVOL D9 dependency repair plan

> generated: `2026-08-04T02:35:00Z`

## 판정

**DEPENDENCY_TARGETS_FROZEN**

- unique dependency symbol-days: `458`
- prior regular close targets: `193`
- RVOL 15m lookback targets: `448`
- date range: `2023-04-21` → `2024-08-26`
- symbols: `30`

## 범위

- frozen snapshot read-only: `True`
- blind OOS opened: `False`
- performance / forward returns computed: `False`
- canonical DB / frozen snapshot written: `False`

## 해석

이 목록은 event-day 재수집 목록이 아니다. 각 D9 제외 이벤트를 실제로 만든 선행 거래일 종가 또는 직전 20거래일 09:30–09:44 RVOL 입력 중 고정 스냅샷에 없는 날만 중복 제거해 고정한 목록이다.
