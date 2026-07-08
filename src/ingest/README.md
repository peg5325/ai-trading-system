# src/ingest — 과거 캔들 백필 (backfill)

## 1. 목적

과거 캔들 백필. Toss 시세를 페이징으로 훑어 Canonical `Candle` 로 바꾼 뒤 SQLite `CandleStore` 에 upsert 한다.
트레이딩 파이프라인과 완전히 분리된 **데이터 수집 사이드 경로**다.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `symbol`(위치인자), `interval`(`1m`\|`1d`), `--pages`(기본 3), `--page-size`(기본 200) |
| | 자격증명은 `.env` → `load_settings()`; DB 경로는 `settings.db_path` |
| 출력 | 캔들 DB 에 upsert. stdout 에 진행/검증 리포트(`[backfill]` 건수, `[store]` 총계, UTC 범위, 최근 3봉) |

자격증명이 없으면 `SystemExit` (`backfill.py:20-21`).

## 3. 주요 함수

- `run(symbol: str, interval: str, pages: int, page_size: int = 200) -> None` — `backfill.py:18`
- `main() -> None` — `backfill.py:46` (argparse 래퍼)

500건마다 `store.upsert_many(batch)` 로 배치 커밋한다 (`backfill.py:29-33`).

## 4. 다른 모듈과의 관계

`src/config`, `src/providers/toss_market_data`, `src/storage/candle_store` 를 import.
**이 모듈을 import 하는 곳은 아무 데도 없다** — `python -m src.ingest.backfill` 로만 실행된다.
(프로젝트에서 유일하게 `scripts/` 진입점이 없는 운영 표면이다.)

## 5. 절대 하면 안 되는 것

- ❌ **주문하지 않는다.** 도크스트링: *"주문은 없다. 읽기 + 저장만."*
- ❌ **지표를 계산하지 않는다.** 원시 OHLCV 만 저장한다.
- ❌ **`--pages` 가드를 제거하지 않는다.** *"페이징 최대 페이지 수(호출 제한 보호)"*.
  `iter_history` 는 페이지 간 `sleep_s=0.4` 를 둔다 — Toss API rate limit 보호.
- ❌ 컨텍스트 매니저를 벗기지 않는다 — HTTP 세션과 DB 커넥션은 결정론적으로 닫혀야 한다.

쓰기는 `upsert_many`(멱등)이므로 **재실행은 안전**하다.

## 6. 사용 예시

```bash
./.venv/bin/python -m src.ingest.backfill SOXL 1m --pages 3
./.venv/bin/python -m src.ingest.backfill SOXL 1d --pages 5
./.venv/bin/python -m src.ingest.backfill 005930 1m --pages 3 --page-size 200
```

## 7. 테스트

⚠️ **테스트 없음.** `tests/` 에 `test_ingest.py` / `test_backfill.py` 가 없고, `src.ingest` 를
import 하는 테스트도 없다. 협력자들은 각각 커버돼 있으나(`tests/test_toss_client.py`,
`tests/test_store.py`), `run()` 자체 — 500건 배치 로직과 자격증명 가드 — 는 미검증이다.
