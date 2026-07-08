# src/journal — 감사 로그 (TradingJournal)

## 1. 목적

모든 의사결정 사이클을 **주문이 없었던 사이클까지 포함해** 영속 기록한다.
매매 판단은 하지 않는다. Replay / PnL / Review / Dataset 이 전부 이 위에 선다.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `TradingPipeline` 결과(`JournalCycle.from_pipeline`), portfolio/market 스냅샷(`.to_dict()` 가진 아무 객체) |
| 출력 | `JournalCycle`(JSON 직렬화 가능), 심볼별 outcome dict, 기간 조회 `list[JournalCycle]` |

## 3. 주요 클래스/함수

- `JournalCycle` — `models.py:29`: `cycle_id, cycle_ts, session, universe, status, reason, error,
  outcomes=[], order_count=0, portfolio=None, market=None`
- `JournalCycle.from_pipeline(cycle_id, session, universe, result, *, cycle_ts=None, status="RAN", ...)` — `models.py:61`
- `JournalCycle.error_cycle(cycle_id, universe, error, *, reason="PIPELINE_ERROR", ...)` — `models.py:70`
- `JournalStore` Protocol — `models.py:84`: `save_cycle`, `get_cycle`, `get_by_symbol`, `get_by_time_range`
- `InMemoryJournalStore()` — `models.py:94` / `SqliteJournalStore(db_path)` — `sqlite_store.py:40`
- `ts_from_cycle_id(cycle_id)` — `models.py:18` (`'20260706T144500Z'` → `'2026-07-06T14:45:00Z'`)

## 4. 다른 모듈과의 관계

`src/` 의존성 **0** 인 leaf.
직접 import: `runtime/metrics_store.py`, `runtime/paper_runtime.py`, `live/paper_loop.py`, `cli/inspect.py`.
duck-typed 로 소비: `replay/system.py`, `review/batch_runner.py`, `system_review/window.py`,
`pnl/journal_pnl.py`, `dataset/builder.py`.

## 5. 절대 하면 안 되는 것

- ❌ **매매 판단을 하지 않는다.** 도크스트링: *"오직 사이클별 의사결정 과정을 기록/추적한다."*
- ❌ **주문 없는 사이클을 버리지 않는다.** *"주문이 없었던 사이클도 반드시 저장한다."*
  (스킵/에러 사이클이 사라지면 회고가 거짓말을 한다.)
- ❌ JSON 직렬화 불가능한 객체를 넣지 않는다.
- ❌ `JournalStore` 인터페이스를 우회해 SQLite 를 직접 만지지 않는다 (PostgreSQL 교체 가능성 유지).
- ❌ **같은 `cycle_id` 를 재사용하지 않는다** — 아래 참조.

> ⚠️ **저널은 append-only 가 아니다.** `SqliteJournalStore.save_cycle`(`sqlite_store.py:55-75`)은
> `INSERT OR REPLACE INTO journal_cycle` 후 outcome 행을 `DELETE` → 재삽입한다
> (주석: `# symbol 별 outcome 재기록 (멱등)`). 즉 **`cycle_id` 키의 멱등 upsert** 다.
> `cycle_id` 가 사이클마다 유일하기 때문에 *실질적으로만* append-only 처럼 동작한다.
> 같은 id 로 다시 저장하면 조용히 덮어쓴다.

## 6. 사용 예시

```python
from src.journal.models import JournalCycle
from src.journal.sqlite_store import SqliteJournalStore

store = SqliteJournalStore("data/journal.sqlite")
store.save_cycle(JournalCycle(
    cycle_id="20260706T144500Z", cycle_ts="2026-07-06T14:45:00Z",
    session="REGULAR", universe=["AAPL"], status="RAN",
    reason=None, error=None,
    outcomes=[{"symbol": "AAPL", "action": "ORDERED"}], order_count=1,
))
cycles = store.get_by_time_range(start=None, end=None)
```

조회: `./.venv/bin/python scripts/inspect_cli.py cycles --limit 20`

## 7. 테스트

`tests/test_journal.py` (주 테스트). `test_pnl.py`, `test_replay.py`, `test_dataset.py`,
`test_review_batch.py`, `test_system_review.py`, `test_runtime.py` 등이 저널 위에서 돈다.
