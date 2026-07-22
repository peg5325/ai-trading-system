# P0-US30 일봉(1d) 10년 Bulk Backfill 리포트

- schema: `p0_us30_daily_10y_manifest/v1`  ·  interval: **1d**
- 유니버스 정본: `universe_p0_us30.yaml` v1 (as_of 2026-07-21, count=30, US/USD, valid=True)
- 수집 코드 커밋(merged path): `8a58fdc`  ·  DB: `market_history.sqlite`
- 수집 파라미터: max_pages=13, page_size=200, spacing=0.5s, resume=True, gap_skip_minutes=0
- **manifest_content_sha256**: `951440bf8d22b5a50c4f252591022ed0ad3d2fdfbed161178412d9193e15953a`
- generated_at_utc: 2026-07-22T02:42:02Z (hash 비대상)

## 요약
- 종목 30 / attempted 30 / with_rows 30
- verdict: {"OK": 30}
- 총 일봉 행수: 76,720  ·  범위 2016-03-18T04:00:00+00:00 ~ 2026-07-22T04:00:00+00:00
- 실행: 총 API 데이터페이지 385, inserted 76,720, duplicate 0, conflict 0, errors 0 []

## 1m 무변경 불변식 (자본/실험 안전)
- 실행 후 1m 스냅샷: symbols=15, rows=925,146, combined_sha256=`f9dfc0a53d6a0fbc7155d2154af496bbeca4cbecd0d6dd0d6918883d0d6d403b`
- 실행 전 baseline combined_sha256=`f9dfc0a53d6a0fbc7155d2154af496bbeca4cbecd0d6dd0d6918883d0d6d403b`
- **1m INVARIANT HELD: True** (전후 combined_sha256 일치)

## 종목별 스냅샷
| symbol | rows | days | earliest | latest | span_y | verdict | depth | listing | sha256(12) |
|--------|-----:|-----:|----------|--------|-------:|---------|-------|:-------:|------------|
| SPY | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `db33b7f16c3c` |
| QQQ | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `ef63d4806828` |
| IWM | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `95539ebb9cdd` |
| DIA | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `ce92b2bc9027` |
| XLK | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `5a6265bab6a8` |
| XLF | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `c7054640e4b7` |
| XLE | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `09efc9786851` |
| XLV | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `1910c23430c2` |
| XLI | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `bf5076b990b3` |
| XLY | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `aedafed7de86` |
| XLU | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `3c89f0720c0b` |
| SMH | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `141097be556a` |
| AAPL | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `68492e59a4fd` |
| MSFT | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `7119d8b5ab13` |
| NVDA | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `309e177b03ce` |
| AMD | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `24ef6ddd202d` |
| AVGO | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `617e62ba6487` |
| AMZN | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `c8ea6eaeeb4f` |
| META | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `2558668dd4e4` |
| GOOGL | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `9ed5a4e15f91` |
| TSLA | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `6848edfc0ba6` |
| MU | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `c4970180cac8` |
| NFLX | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `6cfc4df7c262` |
| CRM | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `15b99bf709fa` |
| PLTR | 1458 | 1458 | 2020-09-30 | 2026-07-22 | 5.807 | OK | listing_full | Y | `a1d4c90c9955` |
| VST | 2462 | 2462 | 2016-10-04 | 2026-07-22 | 9.796 | OK | listing_full | Y | `b53a1d89afdc` |
| TQQQ | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `8f83b9c82c65` |
| SQQQ | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `a7698f3f1f39` |
| SOXL | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `120f5bec6a48` |
| SOXS | 2600 | 2600 | 2016-03-18 | 2026-07-22 | 10.344 | OK | page_budget_capped |  | `25d78ce56ccc` |

### 상장/스핀오프 이력 제한(별도 표시)
- **PLTR**: earliest 2020-09-30 (≈5.807y, 공급자 원천 바닥까지 도달=상장이력 전체). ≥5년 요건 충족.
- **VST**: earliest 2016-10-04 (≈9.796y, 공급자 원천 바닥까지 도달=상장이력 전체). ≥5년 요건 충족.

## R1 배당 조정 점검 (XLU)
- adjusted 플래그: [1] · 최대 단일일 낙폭: -11.36%
- **판정: INCONCLUSIVE** — 배당(총수익) 조정은 캔들 OHLCV 만으로 독립 검증 불가. adjusted 플래그는 source(toss_backfill) 주장이며 PROBE_REPORT 는 split 조정만 실증. 추측 금지 → INCONCLUSIVE.

## R2 split 거래량/가격 조정 점검
| symbol | split | ratio | close_ratio | price | volume_ratio | volume | probe검증 |
|--------|-------|------:|------------:|-------|-------------:|--------|:--------:|
| AAPL | 2020-08-31 | 4 | 0.9672 | adjusted | 4.8117 | inconclusive | Y |
| NVDA | 2024-06-10 | 10 | 0.9926 | adjusted | 0.7618 | inconclusive | Y |
| AMZN | 2022-06-06 | 20 | 0.9804 | adjusted | 27.7181 | unadjusted | Y |
| TSLA | 2022-08-25 | 3 | 1.0035 | adjusted | 2.7889 | inconclusive | Y |
| AVGO | 2024-07-15 | 10 | 0.9921 | adjusted | 0.7552 | inconclusive | 정보 |
| GOOGL | 2022-07-18 | 20 | 1.0252 | adjusted | 18.4586 | unadjusted | 정보 |

> price=adjusted 는 split 경계에서 가격 불연속이 없음(수정주가). volume 판정은 큰 비율(≥5)에서만 신뢰 가능하며 작은 split 은 노이즈로 INCONCLUSIVE.

## 안전 재확인
- 전략 수익률·forward return·신호 성능·blind OOS 성과: **미계산/미열람**.
- 주문/Paper 세션/전략·config·가중치 경로: **무변경**. DB 는 canonical 절대경로에만 기록.
- interval `1d` 고정 · 1m gap-skip 미사용 · resume 활성 · 단일 프로세스.
