# src/replay — 사이클 재생 (ReplaySystem, read-only)

## 1. 목적

`cycle_id` 하나로 그 시점의 상태를 완전 복원해 **읽기 전용 `ReplayContext` 스냅샷**으로 만든다.
재생만 한다 — 분석·판단은 하지 않는다(그건 GPT/Review 담당).

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `cycle_id: str`, `JournalStore`(read-only), (옵션) `CandleStore`(read-only) |
| 출력 | `ReplayContext` (JSON 직렬화 가능, GPT 입력용). 없으면 `CycleNotFoundError` |

## 3. 주요 클래스/함수

- `ReplaySystem(journal, candle_store=None, source_interval="1m", resample_rule="5min")` — `system.py:35`
- `.replay(cycle_id) -> ReplayContext` — `system.py:42`
- `ReplayContext` — `models.py:17`: `cycle_id, cycle_ts, session, status, universe,
  market_snapshot, portfolio, symbols=[], journal=None, realized_pnl=None`
- `.to_prompt_dict(max_candles=20) -> dict` — `models.py:47`
- `CycleNotFoundError(KeyError)` — `models.py:12`

## 4. 다른 모듈과의 관계

`src/engines/indicator_engine`(`candles_to_frame`, `resample`, `frame_to_candles`) 을 import.
리샘플·변환은 연구/라이브와 **동일한 canonical core** 를 쓴다(이슈 #76,
`docs/research/RESAMPLER_PARITY_CONTRACT.md`).
`src/pnl/journal_pnl`(`:87`)과 `src/live/paper_loop.frame_to_candles`(`:103`)는 **함수 안에서 lazy import** 한다.
`paper_loop` 를 최상위에서 import 하면 `src.pipeline.models` 가 전이적으로 딸려 들어와
`system.py:5` 의 *"TradingPipeline 과 완전히 분리(파이프라인 미import)"* 불변식이 깨진다.
(순환 import 는 없다 — `pnl`/`live` 모두 `replay` 를 import 하지 않는다.)

사용처: `src/review/batch_runner.py`(duck-typed 주입), `scripts/run_reviews.py`.

## 5. 절대 하면 안 되는 것

- ❌ **상태를 절대 변경하지 않는다.** 도크스트링: *"원본 상태를 절대 변경하지 않는다. 주문 기능 없음."*
  Journal / CandleStore 모두 read-only 로만 접근한다(`system.py:43`, `:99`).
  `to_prompt_dict` 조차 `dict(s)` 로 복사해서 반환한다 — *"원본은 변경하지 않는다."*
- ❌ **TradingPipeline 을 import 하지 않는다.** *"TradingPipeline 과 완전히 분리."*
- ❌ **Look-ahead 금지.** 실현손익은 `as_of`(현재 `cycle_ts`)까지만 FIFO 로 확정하고(`system.py:86`),
  캔들도 `c.ts_utc <= as_of` 만 남긴다(`system.py:106`).
- ❌ 분석/평가/개선안 생성 금지 — Replay 는 재생기지 평론가가 아니다.

## 6. 사용 예시

```python
from src.journal.sqlite_store import SqliteJournalStore
from src.replay.system import ReplaySystem

journal = SqliteJournalStore("data/journal.sqlite")
replay = ReplaySystem(journal, candle_store=None, source_interval="1m", resample_rule="5min")

ctx = replay.replay("20260706T144500Z")     # -> ReplayContext
payload = ctx.to_prompt_dict(max_candles=20)   # GPT 입력용 dict
```

## 7. 테스트

`tests/test_replay.py`. `tests/test_review.py`, `tests/test_review_batch.py` 도 Replay 를 태워 돈다.
