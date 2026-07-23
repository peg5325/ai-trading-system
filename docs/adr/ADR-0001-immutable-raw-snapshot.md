# ADR-0001 — Immutable raw 페이지 스냅샷 + 무의존 거래소 캘린더

- 상태: **Accepted** (이슈 #75, Phase 1B, 2026-07-23)
- 관련 계약: `docs/research/DATA_CONTRACT_4STRATEGY_V1.md` §4.1(원천 immutable) / §2.4(휴장 캘린더) / AMENDMENT A1
- 범위: 코드/저장 구조 결정만. **실제 30종목×3년 bulk 는 미실행.** 전략 성과·forward return 미계산.

---

## 1. 배경 / 문제

현 provider `TossMarketDataProvider._parse`(`toss_market_data.py`)는 응답을 즉시 canonical
`Candle` 로 변환하고 **raw payload 를 버린다**. 계약 §4.1 은 1m 대량 수집 **전에** "공급자 원본
payload 를 재요청 없이 재현 가능한 immutable snapshot 으로 보존"하거나, 그 대체안("canonical
append-only 로 대체")을 **명문 결정**하도록 요구한다.

또한 계약 §2.4 는 주말만 제외하는 현 품질 도구가 **공휴일을 결측으로 오탐**하고 **조기폐장일
커버리지를 과대계상**하는 한계를 지적하며 NYSE 휴장/조기폐장 캘린더 도입을 요구한다. 저장소에는
거래소 캘린더 라이브러리가 없다.

## 2. 결정

### D1 — Immutable content-addressed raw 스냅샷을 **구현한다**(canonical 대체 아님).

`src/history/raw_snapshot.py`(`RawSnapshotStore`) 신설:
- provider 응답 payload 를 **parse 전에** 넘겨받아 보존(`fetch_page(raw_sink=...)` → emit).
- **content-addressed**: 파일명/경로 = payload SHA-256(`<symbol>/<interval>/<sha[:2]>/<sha>.json.gz`).
  동일 payload 멱등(재기록 안 함), 다른 payload 다른 파일(충돌 없음).
- **atomic**: `.partial` → flush → fsync → `os.replace` + dir fsync. 완료본만 `*.json.gz`(partial 미승격).
- **provenance**: append-only `receipts.jsonl`(symbol/interval/before/requested_at/received_at/source/
  content_sha256/바이트/캔들수) — 고정 화이트리스트(secret·인증헤더 미포함).
- **gitignore**: 기본 위치 `data/raw_snapshots/`(`.gitignore` 의 `data/` 로 자동 제외).
- **외부 복제 대비**: 파일 기반 트리 + receipts 라 #73 외부복제 대상에 raw 계층을 그대로 포함 가능.

### D2 — 거래소 캘린더는 **신규 의존성 없이** 고정 추적 상수로 구현한다.

`src/history/exchange_calendar.py`: 2023-01-01~2026-12-31 NYSE 전휴장·조기폐장을 관찰 규칙 적용해
상수로 고정(특별휴장 2025-01-09 카터 국가애도일 포함). `is_trading_day`/`is_early_close`/
`expected_regular_minutes`/`max_consecutive_nontrading_days`/`recommended_max_consecutive_empty`.

### D3 — 압축은 **stdlib gzip**(mtime=0 결정론)을 쓴다.

## 3. 용량 추정 (해석적 — 라이브 API 호출 없음)

근거: `phase1a_probe_result.json`(755 1m-bars/거래일)·PROBE_REPORT(548 bytes/canonical row). 대표
raw 페이지(200봉, Toss 1m 스키마)를 `raw_snapshot.measure_page` 로 실측해 30종목×3년으로 환산.
재현: `src/history/raw_snapshot.estimate_bulk_raw_storage`.

| 항목 | 값 |
|---|---|
| 대표 페이지(200봉) raw JSON | 32,598 B (162.99 B/candle) |
| 대표 페이지 gzip | 1,946 B (9.73 B/candle) |
| **gzip 비율** | **≈ 5.97%** (반복적 JSON → 고압축) |
| 30종목×3년 총 봉(전 세션, 755/일×252×3×30) | ≈ 17.1M |
| **raw gzip 총량(전 세션)** | **≈ 0.16 GiB (166 MB)** |
| raw 미압축 JSON | ≈ 2.6 GiB |
| canonical DB(548 B/row) | ≈ 8.74 GiB |
| **raw_gz + canonical** | **≈ 8.9 GiB** |

→ raw 계층은 gzip 으로 **매우 작다**(canonical 대비 ~2%). 디스크 여유 권장 ≥20 GiB 안에 여유롭게 든다.

## 4. 대안 (canonical append-only 로 대체) 과 비교

| 기준 | D1 immutable raw(채택) | 대체: canonical append-only 만 |
|---|---|---|
| 재현성 | **원본 payload byte 재현** — 파싱 로직이 바뀌어도 원천에서 재파싱 가능 | canonical 만 보존 → **파싱 후 손실 필드**(여분 키·원문 문자열)는 재현 불가 |
| 감사 | 요청 provenance(before/cursor/시각) 원본과 결합 | ingest_log/provenance 컬럼(요약)만 |
| 저장 | +0.16 GiB(gzip, 무시할 수준) | +0 |
| 복잡도 | 신규 저장 계층·atomic·content-addressing | 없음(기존 유지) |
| 계약 부합 | §4.1 "원천 immutable snapshot" 직접 충족 | §4.1 이 요구한 "명문 대체 결정"에 해당하나 재현성 약함 |

**결정 근거**: 용량 비용이 무시할 수준(gzip 0.16 GiB)인데 재현성·감사 이득이 크다. back-adjusted
데이터(§3.2)의 point-in-time 한계상 원천 보존 가치가 높다. → **immutable raw 채택, blocker 없음.**

> 만약 실측에서 raw+canonical 이 비정상적으로 컸다면(예: 압축이 무력) canonical 대체를 재검토해야
> 했으나, gzip 5.97% 로 그 반례는 성립하지 않는다.

## 5. 의존성 결정 (무단 추가 금지 원칙)

| 후보 | 판정 | 이유 / 대안 / 지원·라이선스·업데이트 |
|---|---|---|
| **gzip**(stdlib) | **채택** | Python 표준. 별도 설치·라이선스·업데이트 불필요. mtime=0 로 결정론. |
| zstandard | 기각 | 미설치·미선언. gzip 대비 이득(0.16→더 작게)이 무의미(이미 작음). 신규 의존성 회피. |
| exchange-calendars / pandas-market-calendars | 기각 | 미설치·미선언. **대안**=고정 상수 artifact(3년 구간, 재검증 용이). **지원기간**=구간 밖 조회 시 `CalendarRangeError` 로 fail-closed(조용한 오판 금지). **라이선스**=신규 없음. **업데이트**=구간 확장 시 NYSE 공지로 재검증 후 상수 갱신(추적 diff). 저장소 관례(`quality.py` 주말 손수 모델링)와 일치. |
| scipy | 무관 | Phase 1B 미사용. |

## 6. 결과 / 후속

- immutable raw·캘린더·gzip 채택. **blocker 없음**(용량 추정 정상).
- raw 계층 외부 복제(#73 확장)·수집 후 자동 백업은 별도 이슈.
- 5m 연구↔라이브 리샘플러 통일은 **별도 Phase 2 blocking-gate 이슈 #76**(ORB 공식 연구 전 필수).
- 실제 30종목×3년 raw 수집은 이 ADR 범위 밖(Phase 1B 이후 사용자 승인 하 실행).
