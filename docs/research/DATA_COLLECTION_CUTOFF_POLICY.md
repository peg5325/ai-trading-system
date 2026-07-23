# 데이터 수집 완료-거래일 cutoff + partial→final 안전 정책 (이슈 #72)

> 배경: Phase 1B-Daily(#70/PR #71)에서 미완성 진행일(2026-07-22) 일봉 30봉이 as_of 경계를 넘어
> 수집·manifest 에 포함된 결함을 **사후 삭제**로 정정해야 했다. 재발 방지가 운영 절차에 의존하던
> 것을 코드 게이트로 옮긴다. 전략/주문/config 무변경. 자본 안전 경로(store.py 쓰기 정책) 무변경.

## 1. 공식 P0-US30 bulk 실행(안전한 CLI 예시)

공식 수집은 **완료-거래일 cutoff 를 강제하는 전용 CLI** `scripts/history_backfill_official.py` 로만 한다.
`--completed-through` 가 없으면 자격증명/ API/DB 접근 전에 `exit 2`(fail-closed).

```bash
# 30종목 3년 1분봉(세션 PRE/REGULAR/POST, raw immutable 스냅샷 보존)
./.venv/bin/python scripts/history_backfill_official.py \
    --db data/market_history.sqlite --interval 1m \
    --completed-through 2026-07-21 \
    --raw-snapshot-dir data/raw_snapshots \
    --spacing 0.5 --gap-skip-minutes 200 --max-consecutive-empty 200

# 30종목 10년 일봉
./.venv/bin/python scripts/history_backfill_official.py \
    --db data/market_history.sqlite --interval 1d --completed-through 2026-07-21
```

비공식/프로브 실험은 `scripts/history_backfill.py`(비공식 경로)를 쓴다. 이 CLI 는 조용한
fail-open 을 막기 위해:
- `--completed-through` 미지정 + **canonical DB**(`market_history.sqlite`) → **하드 게이트로 거부**
  (`exit 2`). 진짜 필요하면 `--allow-unofficial-canonical` 로 명시 opt-in 해야 한다.
- `--completed-through` 미지정 + **scratch DB** → stderr 비공식 경고 후 진행(프로브 편의).
- `--completed-through` 지정 시 공식 경로와 **동일한 세션-범위 검증**(scope)을 적용한다.

## 2. cutoff 의 NYSE 판정(세션 범위·정상일·조기폐장·휴장·DST)

`src/history/completed_session.py:last_completed_trading_day(now_utc, session_scope=...)` 가
판정한다. 현재시각은 **주입**(now_utc)하며 코드 내부에서 임의로 고정하지 않는다(테스트 재현성).
ET 변환은 저장소 표준 `src/models.py:ET = ZoneInfo("America/New_York")`(stdlib zoneinfo)를
재사용해 EDT/EST·자정 교차를 tz-aware ET datetime 비교로 자동 처리한다.

**★완료 경계는 수집하는 세션 범위(scope)에 맞춘다.** 저장하는 가장 늦은 세션이 그 날의 완료
시각을 정한다(그렇지 않으면 진행 중 세션 봉이 샌다 — 이슈 #72 핵심):

| scope | 수집 범위 | 그 날 "완료" 시각(ET) |
|-------|-----------|----------------------|
| `regular` | 1d(일봉) 또는 PRE+REGULAR | 정규 마감 — 정상 **16:00** / 조기폐장 **13:00** |
| `post` | **1m 기본(PRE+REGULAR+POST)** | 정규 마감 + 애프터마켓 4h — 정상 **20:00** / 조기폐장 **17:00** |
| `overnight` | 1m `--all-sessions`(OVERNIGHT 포함) | **익일 04:00** |

scope 는 `completion_scope_for_sessions(interval, allowed_sessions)` 로 수집 interval·세션에서
자동 산출한다. `exchange_calendar.regular_close_minute()` 가 조기폐장(780분)/정상(960분)을 구분하고,
POST 는 +4h(16→20 정상, 13→17 조기), OVERNIGHT 은 익일 04:00 으로 확장한다.

- **주말·전휴장일**: 그 날은 거래일이 아니므로 직전 완료 거래일로 판정.
- **캘린더 범위 밖**: `exchange_calendar` artifact 구간(2023-01-01~2026-12-31) 밖은
  `CalendarRangeError` 로 거부(추측으로 거래일 오판 금지 — NYSE 공지로 상수 확장 후 재실행).
  ★ `CALENDAR_END=2026-12-31` 이후에는 모든 공식 수집이 fail-closed(의도된 게이트, 상수 확장 필요).

요청 `--completed-through` 가 위(scope 반영) 마지막 완료 거래일보다 **늦으면**(진행 중/미래 세션),
또는 **거래일이 아니면**(주말/휴장), 또는 **형식 오류/범위 밖**이면 `exit 2`(fail-closed).

## 3. 저장 순서(raw → cutoff → session filter → canonical)

`src/ingest/backfill_history.py:backfill_symbol` 의 페이지 처리 순서:

1. **provider raw payload immutable snapshot** — `RawSnapshotStore.emit`(parse 전, content-addressed).
2. **payload parsing** — provider 가 canonical `Candle` 로 변환.
3. **완료-거래일 cutoff 필터** — `partition_by_cutoff`: `ts_exchange` 거래소-로컬 일자가
   `completed_through` **이하**면 보존, 초과(다음 거래일 이후 = 미완성 진행일)면 제외.
4. **SessionFilter**(PRE/REGULAR/POST) — 1분봉 세션 선별(unknown 세션은 fail-closed).
5. **canonical upsert** — `HistoryStore.upsert_candles`.

불변식:
- 페이지네이션 cursor(oldest/newest/next_before)는 **필터 전 전체 provider 페이지** 기준으로
  계산한다 → cutoff 가 과거 페이지 탐색 깊이를 바꾸지 않는다(제외돼도 커서는 정상 전진).
- cutoff 로 제외된 봉도 **raw 스냅샷에는 원본 그대로** 남는다(raw 는 parse 전 단계).
- 경계: `trading_day <= completed_through` → cutoff 당일 포함, 이후 제외. 1d·1m 동일.
- resolved `completed_through`·제외 행수·제외 일자 범위를 **결과(report)와 ingest_log** 에 기록.
- manifest 연결: `daily_manifest`/`intraday_manifest.build_manifest(collection_completed_through=...)`
  는 수집 cutoff 가 manifest 의 `as_of` 보다 **이르면**((cutoff, as_of] 미수집) fail-closed 해
  OK 판정을 받을 수 없게 한다(경계 완화 금지 규율과 정합).

## 4. first-write-wins 를 유지한 이유

`src/history/store.py:_upsert_row` 는 동일 우선순위(adjusted·source_rank 동률) 충돌 시
**기존 값을 유지**한다(first-write-wins). 이는 자본 안전·원본 불변 정책의 핵심이다: 한 번 확정
저장된 canonical 봉이 이후 실행에서 조용히 덮어써지지 않음을 보장한다. 따라서 이번 작업에서

- generic `force_update=True` 같은 우회를 **추가하지 않았다**(정책 완화 금지).
- 대신 **예방**(cutoff)으로 partial 봉이 애초에 들어가지 않게 하고,
- 이미 들어간 partial 은 아래 **감사 가능한 삭제 후 재수집**으로만 교체한다.

## 5. partial→final 교체 = audited delete→refetch 전용

`src/history/partial_remediation.py`(+ `scripts/history_partial_remediation.py`)가 강제하는 표준 정책:

1. partial 식별(symbol/interval/거래소-로컬 일자 범위).
2. pre-image 행수·content hash 기록.
3. SQLite backup API(`.backup()`)로 원본 read-only 안전 백업.
4. 예상 범위만 transaction DELETE.
5. 삭제 행수·대상 pre-image hash 가 **예상값과 불일치하면 rollback**(무변경).
6. `integrity_check`(불통 시 rollback).
7. 완료-거래일 cutoff 를 적용해 **refetch** ← 삭제 후 `scripts/history_backfill_official.py
   --completed-through …` 재실행(완성봉으로 채움).
8. post-image hash·행수·감사 receipt(JSONL) 기록.

- **기본 dry-run**. 실제 삭제는 `--execute` + `--expected-rows` + `--expected-hash` 필수.
  현재 대상이 예상과 다르면 삭제 전에 거부(fail-closed) — DB 가 예상 상태가 아니면 손대지 않는다.
- 모든 mutation 은 호출자가 지정한 임시 DB 에서만. `--db` 는 필수(기본 canonical 경로 없음).

## 6. 이번 PR 의 범위와 실행하지 않은 것

- **실제 30종목×3년 1m bulk 수집을 실행하지 않았다.** 실 Toss API 호출 0, canonical DB write/delete 0.
- canonical `data/market_history.sqlite` 에 대한 remediation(삭제)도 실행하지 않았다. 모든 mutation
  테스트는 임시 DB 전용.
- 전략 성과·forward return·blind OOS 성과 계산/열람 0.
- 이번 PR 은 **예방(cutoff) + 감사 가능한 remediation 도구 + 테스트 + 문서**까지다.

## 7. 다음 단계 / 게이트

- **다음 단계**: `scripts/history_backfill_official.py` 로 **30종목×3년 1m bulk 수집**(승인 후 실행).
  10년 일봉은 이미 수집(#70)돼 있으므로 재수집은 선택.
- **#76**(리샘플러 통일): ORB 공식 사전등록/백테스트 **전** 별도 blocking gate. 이번 PR 범위 밖.
- **#78**(blind OOS unlock 검증 로더): blind OOS 개봉 **직전** 필수. 이번 PR 범위 밖.

## 8. 알려진 잔여/후속(조용히 생략하지 않음)

독립 리뷰(code-reviewer·architect)에서 나온 항목 중 이번 PR 에 반영한 것과 후속으로 분리한 것:

**반영됨(이번 PR)**
- 완료 경계를 세션 범위에 연동(POST 20:00/17:00, OVERNIGHT 익일 04:00) — 리뷰 HIGH 해소.
  16:00 고정 경계가 진행 중 애프터마켓 봉을 새게 하던 결함을 닫음. 20:00 경계는 일봉(16:00 확정)에
  대해서도 4h 여유를 줘 공급자 확정 지연도 흡수한다.
- 비공식 CLI 의 canonical fail-open 을 하드 게이트로 차단(§1).
- remediation 의 pre-image content hash 를 쓰기락(BEGIN IMMEDIATE) **안**에서 재검증(TOCTOU 차단).
- 백업 파일명에 timestamp 포함(반복 remediation 백업 덮어쓰기 방지), refused 경로 receipt 대칭.

**후속(별도 이슈로 분리 — 이번 PR 범위 밖, blind OOS/실 bulk 전 처리)**
- manifest 의 `collection_completed_through` 를 CLI 인자 신뢰 대신 **ingest_log 에 기록된 실제 수집
  cutoff 와 교차검증**(현재는 정직하게 선언된 cutoff 만 방어). 오선언 cutoff 로 인한 거짓 OK 방지.
- backfill report 의 `earliest/latest` 는 **fetch 도달 범위**(cutoff 제외분 포함)라 저장 범위와
  다를 수 있음 — 라벨 명확화 또는 저장 범위 별도 노출.
