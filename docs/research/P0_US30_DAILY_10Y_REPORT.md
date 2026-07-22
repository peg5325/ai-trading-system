# P0-US30 일봉(1d) 10년 Bulk Backfill 리포트

- schema: `p0_us30_daily_10y_manifest/v1`  ·  interval: **1d**  ·  **as_of(공식 경계): 2026-07-21**
- 유니버스 정본: `universe_p0_us30.yaml` v1 (as_of 2026-07-21, count=30, US/USD, valid=True)
- 수집 코드 커밋(merged path): `8a58fdc`  ·  DB: `market_history.sqlite`
- 수집 파라미터: max_pages=13, page_size=200, spacing=0.5s, resume=True, gap_skip_minutes=0
- **data_content_sha256**(포함 데이터 결정론 지문): `756e5bd7cd4cc393dc18f88792a2f56b83a02ca45f28afb3633b139abac07772`
- generated_at_utc: 2026-07-22T06:45:00Z (hash 비대상)

## 요약
- 종목 30 / attempted 30 / with_rows 30
- verdict: {"OK": 30}
- 공식 총 일봉 행수(≤as_of): 76,690  ·  범위 2016-03-18 ~ 2026-07-21
- as_of 초과(미완성) 봉: 0 (0 이어야 정상)
- 수집 실행: 총 API 데이터페이지 385, inserted 76,720, duplicate 0, conflict 0, errors []

## as_of 경계 교정 (감사 가능)
- 수집분에는 미완성 진행일 **2026-07-22** 일봉 30개(30종목)가 포함돼 있었다(정규장 미완료·거래량 극소).
- `remove_partial_daily_beyond_as_of` 로 제거 → 1d 76,690행, integrity `ok`. 1m 무변경. 감사 로그 `p0_us30_daily_partial_removal_audit.20260722.json`(reports/, gitignore).
- 정책: 수집 CLI 에는 완료-거래일 컷오프가 없으므로 **수집 후 as_of 초과 봉 제거**가 표준 절차. 같은 날짜 partial→완성 갱신은 store.py first-write-wins(동일 source·adjusted 유지) 때문에 in-place 로 안 되며 **삭제 후 재수집**이 필요(별도 정책).

## 저장소/백업 durability
- ⚠ canonical DB·백업은 **로컬 디스크에만** 존재(GitHub 미저장, gitignore). 여기서 durable=로컬 디스크 기준. API 재소모 방지를 위해 외부 저장소 복제 권장.
- 백업 `data/backups/market_history.pre-1d-backfill.20260722T023627Z.sqlite` — 506,720,256B · sha256 `f0b0860545db23f874cbf2d6a93c13503a4beb7bfd22bd15251105fe48624d1f`
- 백업 `data/backups/market_history.post-1d-with-partial.20260722T063921Z.sqlite` — 548,896,768B · sha256 `77422e533ee3e21828b59d294be75de6d848de0eedfdb1bab53734056eecc296`

## 1m 무변경 불변식 (자본/실험 안전)
- 실행 후 1m OHLCV-content 스냅샷: symbols=15, rows=925,146, combined_sha256=`f9dfc0a53d6a0fbc7155d2154af496bbeca4cbecd0d6dd0d6918883d0d6d403b`
- 실행 전 baseline(OHLCV-content) `f9dfc0a53d6a0fbc7155d2154af496bbeca4cbecd0d6dd0d6918883d0d6d403b` → **HELD: True**
- **전체 저장 필드**(ts_exchange/session/currency/adjusted/source 포함) 대조: 현재 `1f03972f9a5906114855399bb17be6a3f7abb3818896a5f2a66274d9fe10e3a1` vs pre-1d 백업 `1f03972f9a5906114855399bb17be6a3f7abb3818896a5f2a66274d9fe10e3a1` → **HELD: True** (rows=925,146)

## 종목별 스냅샷 (≤as_of)
| symbol | rows | days | earliest | latest | span_y | density | gap_d | verdict | depth | listing | sha256(12) |
|--------|-----:|-----:|----------|--------|-------:|--------:|------:|---------|-------|:-------:|------------|
| SPY | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `fe2124e90b9d` |
| QQQ | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `5b5964fe9bfe` |
| IWM | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `750f63fd82dc` |
| DIA | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `0ad9b43eafff` |
| XLK | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `4c7510a0fa37` |
| XLF | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `ae66ee6aef31` |
| XLE | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `4e6a53d1b931` |
| XLV | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `295d910190e7` |
| XLI | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `b3fe866a6dd7` |
| XLY | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `e26d463005eb` |
| XLU | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `02fd4d35fc8f` |
| SMH | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `88fa7c5e34f7` |
| AAPL | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `4a0892541038` |
| MSFT | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `fccced48336b` |
| NVDA | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `fd628ff61b27` |
| AMD | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `5242b7625058` |
| AVGO | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `e684fc9a8de4` |
| AMZN | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `5e153ad41930` |
| META | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `4187e53cef2b` |
| GOOGL | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `9cf8f9de38bb` |
| TSLA | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `9252131c5fd5` |
| MU | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `21a6ecfcf38a` |
| NFLX | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `8e475bab837a` |
| CRM | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `60f2bbf2a793` |
| PLTR | 1457 | 1457 | 2020-09-30 | 2026-07-21 | 5.804 | 0.996 | 4 | OK | listing_full | Y | `76a1dcfd5e91` |
| VST | 2461 | 2461 | 2016-10-04 | 2026-07-21 | 9.793 | 0.997 | 4 | OK | listing_full | Y | `088bb0a47e24` |
| TQQQ | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `b536f97870c1` |
| SQQQ | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `4003324cd946` |
| SOXL | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `7121a32a8dec` |
| SOXS | 2599 | 2599 | 2016-03-18 | 2026-07-21 | 10.341 | 0.997 | 4 | OK | page_budget_capped |  | `d1b3d1638d31` |

> verdict OK = span≥min_years AND as_of 초과봉 0 AND density≥0.90 AND 내부gap≤10d. 하나라도 불충족 시 INCONCLUSIVE_DATA(사유는 manifest.verdict.inconclusive_reasons).

### 상장/스핀오프 이력 제한(별도 표시)
- **PLTR**: earliest 2020-09-30 (≈5.804y, 공급자 원천 바닥까지 도달=상장이력 전체). ≥5년 요건 충족.
- **VST**: earliest 2016-10-04 (≈9.793y, 공급자 원천 바닥까지 도달=상장이력 전체). ≥5년 요건 충족.

## R1 배당 조정 점검 (XLU)
- adjusted 플래그(source 주장): [1]
- **판정: INCONCLUSIVE** — 배당(총수익) 조정은 캔들 OHLCV 만으로 독립 검증 불가(외부 배당/총수익 기준 필요). adjusted 플래그는 source(toss_backfill) 주장이며 PROBE_REPORT 는 split 조정만 실증. 수익률 미계산·추측 금지 → INCONCLUSIVE.

## R2 split 거래량/가격 조정 점검
| symbol | split | ratio | close_ratio | price | volume_ratio | volume | probe검증 |
|--------|-------|------:|------------:|-------|-------------:|--------|:--------:|
| AAPL | 2020-08-31 | 4 | 0.9672 | adjusted | 4.8117 | inconclusive | Y |
| NVDA | 2024-06-10 | 10 | 0.9926 | adjusted | 0.7618 | inconclusive | Y |
| AMZN | 2022-06-06 | 20 | 0.9804 | adjusted | 27.7181 | unadjusted | Y |
| TSLA | 2022-08-25 | 3 | 1.0035 | adjusted | 2.7889 | inconclusive | Y |
| AVGO | 2024-07-15 | 10 | 0.9921 | adjusted | 0.7552 | inconclusive | 정보 |
| GOOGL | 2022-07-18 | 20 | 1.0252 | adjusted | 18.4586 | unadjusted | 정보 |

> price=adjusted 는 split 경계에서 가격 불연속이 없음(수정주가). volume 판정은 큰 비율(≥5)에서만 신뢰 가능하며 작은 split·경계비율≈1 은 노이즈로 INCONCLUSIVE(추측 금지).

## 안전 재확인
- 전략 수익률·forward return·신호 성능·blind OOS 성과: **미계산/미열람**(R1/R2 도 수익률 미계산).
- 주문/Paper 세션/전략·config·가중치 경로: **무변경**. DB 는 canonical 절대경로에만 기록.
- interval `1d` 고정 · 1m gap-skip 미사용 · resume 활성 · 단일 프로세스.
