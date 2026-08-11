# P0-US30 Gap+RVOL Toss dependency repair candidate

> generated: `2026-08-11T00:32:14Z`

## 판정

**TOSS_DEPENDENCY_REPAIR_CANDIDATE**

- frozen available targets: `265`
- eligible dependencies: `0`
- ineligible dependencies: `265`
- isolated candidate rows: `110611`
- HTTP 429 recovered: `62`
- first missing REGULAR minute among ineligible targets: `{"570": 250, "571": 14, "574": 1}`

## Identity

- plan: `7a27ee176f910e943c955ac2db034dfebf47d7f2ffa5583c3edf244d576014c0`
- probe: `7d27367c20f4d43444a1f622ef4331f07c1bb666529cae54227165e5ce9e1717`
- candidate data: `f2eae1464ae28ba0abc983137c4c16fd6f3a4c7915572e9317f7ce79af22a718`
- candidate raw: `088941ecadaf99265701557272acab4a0cfc92044cf65ba2b02485ed37b13d4e`

## Scope

- Toss-only / frozen available target intersection: `True`
- canonical DB / frozen snapshot written: `False`
- blind OOS opened: `False`
- performance / forward returns computed: `False`

`has_target_day=true`는 날짜 존재만 뜻했다. 실제 full-day 수집 결과 고정 RVOL 입력인 09:30–09:44 ET가 완전한 대상은 0개였으므로, 이번 후보 계층은 D9 결손을 해결하지 못한다.
