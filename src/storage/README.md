# src/storage — 캔들 영속화 (SQLite)

## 1. 목적

Canonical `Candle` 의 SQLite 저장/조회. 설계 `08_Database` 의 `candle` 테이블 구현.
키 기반 upsert 와 정렬된 범위 조회만 제공한다.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `db_path: str \| Path`, `list[Candle]` |
| 출력 | `list[Candle]`, `int`(건수), `tuple[datetime \| None, datetime \| None]`(범위) |

스키마: 테이블 `candle`, PK `(symbol, interval, ts_utc)`. `ts_utc` 는 ISO8601 UTC TEXT.

> ⚠️ `market` 은 컬럼이 아니다. 조회 시 `market_from_currency(currency)` 로 복원하고,
> `ts_exchange` 도 `ts_utc` 에서 재계산한다 (`candle_store.py:123`).

## 3. 주요 클래스/함수

- `CandleStore(db_path)` — `candle_store.py:35`. 부모 디렉터리 생성 + 스키마 적용. 컨텍스트 매니저.
- `.upsert_many(candles) -> int` — `:55`. `INSERT OR REPLACE`, 호출마다 commit. **멱등**.
- `.query(symbol, interval, *, start=None, end=None, limit=None) -> list[Candle]` — `:93`.
  `ts_utc` 오름차순. `start` 포함, `end` 제외. 필터는 키워드 전용.
- `.latest(symbol, interval, limit=5) -> list[Candle]` — `:112`. **내림차순**(뒤집지 않음).
- `.count(symbol, interval) -> int` — `:75` / `.range(symbol, interval)` — `:82`

## 4. 다른 모듈과의 관계

`src/models.py` 만 import 하는 **leaf**. `src/` 내부에서는 `src/ingest/backfill.py` 만 직접 사용하고,
나머지는 스크립트(`run_paper.py`, `build_dataset.py`, `run_reviews.py`)와 상위 계층이 주입해서 쓴다.
`src/replay/system.py` 는 read-only 로 조회만 한다.

## 5. 절대 하면 안 되는 것

- ❌ **지표 컬럼을 저장하지 않는다.** 도크스트링: *"지표 계산은 저장소 밖(IndicatorEngine)에서."*
  저장소는 원시 OHLCV 만 보관한다.
- ❌ Provider 를 직접 호출하지 않는다. 캔들은 항상 밖에서 주입받는다.
- ❌ 매매 판단·주문 금지.
- ❌ 로컬 시각으로 저장 금지 — 저장은 항상 UTC ISO 문자열 기준.

## 6. 사용 예시

```python
from src.storage.candle_store import CandleStore

with CandleStore("data/market.sqlite") as store:
    store.upsert_many(candles)              # 멱등 — 재실행 안전
    rows = store.query("AAPL", "1m", start=t0, end=t1, limit=1000)
    lo, hi = store.range("AAPL", "1m")
```

## 7. 테스트

`tests/test_store.py` (주 테스트). `tests/test_krx.py`, `tests/test_dataset.py`,
`tests/test_replay.py` 등에서도 간접 사용.
