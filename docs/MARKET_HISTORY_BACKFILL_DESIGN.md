# 통합 히스토리 데이터 인프라 설계 — `market_history.sqlite` (설계 전용, 미구현)

> 목적: session별로 파편화된 1분봉을 **단일 통합 저장소**로 병합 + Toss 과거 backfill로 데이터 깊이 확보 →
> 다일 walk-forward / 파라미터 스윕 기반 마련. **이번 문서는 설계만. 구현·실행 없음.**
> 안전: 실주문 금지 · Runtime/Paper 코드 무변경 · 기존 session DB **원본 보존** · append/upsert only · 삭제/덮어쓰기 금지.

---

## 1. 현재 문제 (확인됨)
- **파편화**: 같은 symbol이 6개 session DB(`market_krx.sqlite`, `market_krx_s002`, `market_krx_exit_test_session004`, `market_mixed`, `market_us`, `market_us_exit_test_2000usd`)에 흩어져 중복 저장.
- **중복**: 예) 005930이 4개 DB에 각각 존재(총 1,951행이나 union).
- **얕은 깊이**: 달력 4일(07-06~07-09), 온전한 정규장은 **KR 3일·US 2일**뿐. 최근 30일 영업일 23일 중 19~20일 결측.
- **API 캡**: `get_candles` count 최대 200(초과 400) — `toss_client.py:72` 클램프.
- **결론**: 1개월 백테스트/스윕 불가.

## 2. 통합 저장소 스키마 (제안)

파일: `data/market_history.sqlite` (신규, 기존 DB와 분리)

```sql
CREATE TABLE candle_history (
    symbol       TEXT NOT NULL,
    market       TEXT NOT NULL,          -- 'US' | 'KR'  (currency에서 도출, 명시 저장)
    interval     TEXT NOT NULL,          -- '1m'(원천), 5m은 파생/뷰
    ts_utc       TEXT NOT NULL,          -- ISO8601 UTC — 정규화 정렬/조인 키
    ts_exchange  TEXT NOT NULL,          -- ISO8601 거래소 로컬(ET/KST)
    session      TEXT NOT NULL,          -- PRE/REGULAR/POST/OVERNIGHT/CLOSED
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume REAL NOT NULL,
    currency     TEXT NOT NULL,          -- 'USD' | 'KRW'
    adjusted     INTEGER NOT NULL DEFAULT 1,
    source       TEXT NOT NULL,          -- 'toss_backfill' | 'import:market_krx_s002' 등 provenance
    created_at   TEXT NOT NULL,          -- 최초 삽입 UTC
    updated_at   TEXT NOT NULL,          -- 최종 갱신 UTC
    PRIMARY KEY (symbol, interval, ts_utc)   -- ← UNIQUE KEY = dedup 기준
) WITHOUT ROWID;

CREATE INDEX ix_hist_market_ts  ON candle_history(market, interval, ts_utc);
CREATE INDEX ix_hist_symbol_day ON candle_history(symbol, interval, substr(ts_exchange,1,10));

-- 인제스트 감사 로그(재현/자원추적)
CREATE TABLE ingest_log (
    run_id TEXT, ts TEXT, symbol TEXT, interval TEXT, source TEXT,
    rows_seen INT, rows_inserted INT, rows_updated INT, rows_conflict INT,
    earliest TEXT, latest TEXT, note TEXT
);

-- backfill resume 커서(중단 후 이어받기)
CREATE TABLE backfill_cursor (
    symbol TEXT, interval TEXT, oldest_ts_utc TEXT, next_before TEXT,
    exhausted INTEGER DEFAULT 0, updated_at TEXT,
    PRIMARY KEY (symbol, interval)
);
```

**설계 포인트**
- **UNIQUE KEY = `(symbol, interval, ts_utc)`** — 기존 CandleStore와 동일 3키. 재실행 시 동일 봉은 upsert로 흡수(idempotent).
- `market`를 **명시 컬럼**으로 저장(기존은 currency에서 매번 도출) → US/KR 필터·인덱스 효율.
- `source`/`created_at`/`updated_at`로 provenance·conflict 추적.
- `WITHOUT ROWID` + 복합 PK로 저장효율·조회속도.
- 원천은 **1m만 저장**, 5m은 **조회 시 리샘플**(파생 저장 안 함 → 단일 진실원천 유지).

## 3. 기존 session DB 통합 (read-only import → idempotent merge)

**흐름** (`scripts/history_import_sessions.py`, 신규 제안):
1. `data/market_*.sqlite` 나열(단, `market_history.sqlite` 자신 제외).
2. 각 DB `candle` 테이블을 **read-only(`mode=ro`)** 로 SELECT.
3. 각 행 → `market=market_from_currency(currency)`, `source=f"import:{db_basename}"` 부여.
4. `market_history` 로 **UPSERT**:
   ```sql
   INSERT INTO candle_history(...) VALUES(...)
   ON CONFLICT(symbol,interval,ts_utc) DO UPDATE SET
       open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
       volume=excluded.volume, session=excluded.session, updated_at=excluded.updated_at,
       source=excluded.source
   WHERE <conflict_policy>;   -- 아래
   ```

**중복 제거 방식**: PK 충돌 = 동일 봉. 값 동일하면 no-op(멱등). 값이 다르면 conflict.

**Conflict 우선순위 정책** (권장):
1. **`adjusted=1`(수정주가) 우선** — backfill(adjusted historical)이 session 라이브 캡처보다 정합적.
2. 동일 adjusted면 **source 신뢰순위**: `toss_backfill` > `import:*`(라이브 캡처).
3. 값 불일치 시 **덮어쓰기 전 `ingest_log`(rows_conflict)에 기록** → 사람이 사후 검토(조용한 손실 방지).
4. `created_at`은 **최초값 보존**(COALESCE), `updated_at`만 갱신.

**데이터 품질 검사(merge 직후)**: §5 리포트를 그대로 실행.

## 4. Backfill 설계 — Toss API **가능함 (확인됨)**

**✅ API 지원 확정**: `get_candles(symbol, interval, count≤200, before=?)` 응답에 **`nextBefore` 커서** 반환(`toss_client.py:64-91`). 과거로 페이징하는 iterator가 **이미 구현됨**:
`TossMarketDataProvider.iter_history(symbol, interval, max_pages, page_size=200, sleep_s=0.4)` — `toss_market_data.py:57-75` (nextBefore 따라가며 yield, `nextBefore=null`이면 정지, 빈 페이지면 정지, `sleep_s`로 rate-limit 보호).

→ **backfill은 신규 엔진 불필요.** 기존 `iter_history`를 호출하는 **얇은 driver**만 신설.

**흐름** (`scripts/history_backfill.py`, 신규 제안):
1. symbol×interval 루프. `backfill_cursor.next_before`가 있으면 그 지점부터 resume.
2. `iter_history(sym, iv, max_pages=N, page_size=200, sleep_s=spacing)` 로 과거 페이지 스트림.
3. 페이지마다 `market_history` UPSERT + `backfill_cursor` 갱신(oldest_ts, next_before).
4. **정지 조건**: `nextBefore=null`(Toss 보존 한계 도달) · 빈 페이지 · 설정한 `--from-date` 바닥 · ts가 더 이상 과거로 안 가는 안전장치 · `max_pages` 소진.
5. 종료 시 `backfill_cursor.exhausted=1`(보존 한계 도달) 기록.

**200봉 제한 대응**: pagination(=`before`/`nextBefore` 커서)이 곧 해법. 페이지당 200봉 × 페이지 반복.
- 1개월 추정: KR ~390봉/일 × ~20일 ≈ 7,800봉 → **~40페이지/종목**. US(연장/24h 포함 시 ~900봉/일) ≈ 18,000봉 → **~90페이지/종목**. `max_pages`는 넉넉히(예 200) 두고 nextBefore로 자연 정지.

**⚠️ 미지수(실측 필요)**: Toss가 **얼마나 과거까지** 1분봉을 제공하는지는 문서화 안 됨 → **소량(max_pages=5) 프로브 실행으로 실제 깊이 확인** 후 본격 backfill. "가능한 범위까지"라는 목표는 nextBefore가 끊기는 지점까지로 정의.

**Rate limit / 429**: (a) `sleep_s`(종목간·페이지간 spacing, 기본 0.4~0.5s) (b) 429 시 지수 backoff 재시도 (c) 종목별 격리(한 종목 실패 → 로그 후 다음 종목). **stale-token 401은 이미 자동 재발급**(`toss_client.py:79-81`).

**retry / resume / idempotent**: `backfill_cursor`로 중단 지점 이어받기 + PK UPSERT로 재실행 안전(중복 무해).

## 5. 데이터 품질 리포트 (backfill 후)

`scripts/history_quality_report.py`(신규 제안) — read-only 집계, symbol별:
- earliest / latest ts
- total bars · distinct trading days
- **full-session days**(정규장 분 커버리지 ≥ 임계, 예 KR 390/US 390분의 ≥90%)
- **missing days**(최근 N일 영업일 대비 결측 — 거래소 캘린더/주말 제외)
- **missing intraday minutes**(정규장 창 내 결측 분 수)
- **gap count**(연속성 깨진 지점 수)
- **5m resample 가능 여부**(정규장 커버리지·gap 기준 판정)
- **market별 coverage summary**(US/KR 각 온전일 수, 총 bars, 스윕 적합성)

> 이번 조사에 쓴 커버리지 로직(§ 직전 분석)을 그대로 재사용 → 통합 DB 대상으로 재실행.

## 6. 향후 파라미터 스윕 연결

- **BacktestEngine 입력 연결**: 신규 read-only 로더 `history_loader.load(symbol, interval, start, end) -> list[Candle]` (기존 `candle_store._row_to_candle` 재사용 패턴). BacktestEngine은 `list[Candle]` 입력이므로 그대로 소비.
- **스윕 데이터셋 생성**: `(market, symbol, date_range)` 슬라이스 → US/KR **분리**.
- **in-sample / out-of-sample**: 날짜 경계로 분할(예 앞 70% train / 뒤 30% test).
- **walk-forward 최소 기준(권장)**: 1분봉 스윕은 **시장당 정규장 온전일 ≥ 20일**(단일 train/test), walk-forward는 **≥ 60일**(다중 폴드) 권장. 현재 5일로는 불가 → backfill 깊이가 이 기준을 넘는지로 스윕 착수 판단.
- **별도 코드 변경 필요(승인 항목)**: `BacktestConfig`에 `time_exit_minutes` 필드가 없어(`backtest/engine.py:38-41` 하드와이어 off) **Time 스윕은 이 배선 추가 후 가능**. SL/TP/EOD 스윕은 현행 엔진으로 가능.

## 7. 안전 원칙 (설계 반영)
- 실주문 0 · 시장데이터 조회만(읽기) · Runtime/Paper 코드 **무변경**.
- 기존 session DB는 **`mode=ro`로만 읽음**(원본 불변).
- 통합 DB는 **append/UPSERT only**, 삭제/덮어쓰기 없음(값 충돌은 정책+로그).
- 전 과정 idempotent(재실행 안전).

---

## 구현 순서 (제안)
1. **DDL only**: `market_history.sqlite` 스키마 생성(`scripts/history_init.py`). — API 무관, 무위험.
2. **세션 병합**: 기존 6개 DB read-only import → 즉시 통합(중복 제거). — API 무관.
3. **품질 리포트 v1**: 통합 결과로 §5 리포트(현 5일 baseline 확인).
4. **프로브 backfill**: `max_pages=5`로 Toss 실제 보존 깊이 측정(1종목씩).
5. **본 backfill**: nextBefore 소진까지(종목별 spacing·resume·backoff).
6. **품질 리포트 v2**: 확보 깊이 문서화 → walk-forward 최소 기준 충족 여부 판정.
7. **(별도 승인) 스윕 연결**: history_loader + `BacktestConfig.time_exit_minutes` 배선.

## 예상 명령어 (제안, 미구현)
```bash
./.venv/bin/python scripts/history_init.py --db data/market_history.sqlite
./.venv/bin/python scripts/history_import_sessions.py --db data/market_history.sqlite --src-glob 'data/market_*.sqlite'
./.venv/bin/python scripts/history_quality_report.py --db data/market_history.sqlite
# 프로브(깊이 측정)
./.venv/bin/python scripts/history_backfill.py --db data/market_history.sqlite --symbols 005930 --interval 1m --max-pages 5
# 본 backfill
./.venv/bin/python scripts/history_backfill.py --db data/market_history.sqlite \
    --symbols 005930,000660,402340,SOXL,QQQ,NVDA --interval 1m --max-pages 200 --spacing 0.5
```

## 위험 요소
| 위험 | 영향 | 완화 |
|---|---|---|
| Toss 1m 보존 깊이 미지 | 1개월 미달 가능 | 프로브 먼저, nextBefore 소진까지만, 실제 깊이 보고 |
| 429 rate limit | backfill 중단 | spacing·backoff·종목 격리 |
| stale-token 401 | 장시간 fetch 실패 | 기존 자동 재발급 재사용 |
| adjusted vs 라이브 값 충돌 | 데이터 정합성 | 우선순위 정책 + conflict 로그(조용한 덮어쓰기 금지) |
| US DST/타임존 | ts_exchange 오차 | 기존 `exchange_tz` 재사용 |
| 24h/overnight US 봉 | count 왜곡 | 리포트에서 regular/extended 분리 집계 |
| 거래소 휴장일 오탐 | missing day 과대 | 거래소 캘린더 반영(초기엔 주말만 제외, 공휴일 후속) |

## 다음 승인 요청 항목
1. **DDL + 병합 스크립트 신설**(신규 DB·신규 scripts, 런타임 무변경) 진행 승인 — 무위험 우선.
2. **Toss backfill 실행 승인**(시장데이터 read-only, 실주문 0) — 실행 시간대(장외 권장)·rate-limit 예산 확인.
3. **(별도) BacktestConfig time_exit 배선** 승인 — 스윕 단계에서 필요.
