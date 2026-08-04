# P0-US30 Gap+RVOL D9 Toss 원천 가용성 probe

> generated: `2026-08-04T01:14:00Z`
> JSON SHA-256: `b7c03520e0a0863eb1fb34c18f0db14f552a078be287603c2ffdd60371ac7489`
> 범위: read-only provider capability · DB/raw/snapshot 쓰기 없음 · 성과/OOS 미열람

## 1. 목적과 방법

D9 감사에서 집중 결측이 확인된 2024-07-24~2024-08-05 전후를 P0-US30 전체에 대해
조회했다. 각 종목·날짜마다 해당 ET 거래일이 끝난 뒤의 고정 `before`로 Toss 1분봉 200개를
한 페이지 요청했다. 가격·거래량은 저장하지 않고 반환 행수·timestamp·target date 존재
여부·`nextBefore` 존재 여부만 보존했다.

- 행렬: 30종목 × 11일 = 330회
- 간격: 0.8초
- HTTP 429: 2회, 제한된 지수 백오프로 재시도 후 완료
- control: 2024-07-23, 2024-08-05, 2024-08-06

`has_target_day=False`는 요청 시점보다 과거 데이터가 반환됐고 target ET date의 candle은
한 건도 반환되지 않았음을 뜻한다. `True`는 **존재**만 증명하며 하루 전체 완전성을 증명하지
않는다. 완전성은 후속 targeted full-day fetch에서 별도로 검사해야 한다.

## 2. 결과

| ET 거래일 | 가용 종목 | 미가용 종목 |
|---|---:|---:|
| 2024-07-23 | 30 | 0 |
| 2024-07-24 | 26 | 4 |
| 2024-07-25 | 11 | 19 |
| 2024-07-26 | 4 | 26 |
| 2024-07-29 | 3 | 27 |
| 2024-07-30 | 2 | 28 |
| 2024-07-31 | 1 | 29 |
| 2024-08-01 | 0 | 30 |
| 2024-08-02 | 0 | 30 |
| 2024-08-05 | 30 | 0 |
| 2024-08-06 | 30 | 0 |

핵심 결측 구간 8거래일(2024-07-24~08-02) 240 종목-일 중 **193 종목-일은 현재 Toss
원천에서 target date candle이 0건**이다. 종목별 미가용 일수는 AAPL·MSFT·MU·SPY 8일,
그 외 다수 6~7일이며 26종목이 6일 이상 미가용이다. 2024-08-05는 30종목 모두 현재
공급자에서 존재하므로, 그 날의 frozen snapshot 결측은 Toss로 복구 가능한 수집 공백 후보다.

## 3. 판정

**TOSS_PARTIAL_ONLY**

- Toss만으로 복구 가능한 날짜·종목이 존재한다.
- 그러나 핵심 구간의 193 종목-일은 같은 원천에서 현재 직접 복구할 수 없다.
- 따라서 Toss-only 재수집은 완전한 원천 복구가 아니며, 이 probe만으로 D9 통과를 주장할 수
  없다.
- D9는 `INCONCLUSIVE_DATA`, blind OOS와 다음 후보는 계속 닫힌다.

## 4. 다음 안전 단계

1. `has_target_day=True`인 결측 후보를 full-day로 읽어 09:30~필수 decision window와 전일
   close가 완전한지 검사한다.
2. canonical/frozen snapshot을 건드리지 않는 별도 repair candidate DB와 raw provenance를
   만든다.
3. Toss에서 0건인 193 종목-일은 독립 공급자 또는 보존 원본 없이는 채우지 않는다.
4. defect-only overlay의 코드·identity를 독립 리뷰한 뒤 동일 D9만 재판정한다.

성과 부진을 결측으로 재분류하거나, 5% 한도·15/20일 RVOL 규칙·개발/OOS 경계를 변경하지
않는다.
