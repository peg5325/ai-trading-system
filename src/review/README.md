# src/review — 사후 회고 + 개선 제안 (AIReviewEngine)

## 1. 목적

거래 **전략**에 대한 사후 AI 회고. `ReplayContext` 를 LLM 에 먹여 과거 결정을 채점하고,
결과를 영속화한 뒤, 반복되는 `rule_improvement` 제안을 클러스터링해 전략 개선 후보를 만든다.
과거를 평가할 뿐, 실시간 주문에는 관여하지 않는다.

> 운영 품질(에러/지연/락) 회고는 별도 모듈 → [`src/system_review/`](../system_review/README.md)

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `ReplayContext`(engine) / `JournalStore` + `ReplaySystem` + `AIReviewEngine` + `ReviewStore`(batch) |
| 출력 | `AIReviewReport`, `ReviewRecord`(영속), `RunSummary`, `RuleImprovementReport`, JSONL export |

`decision_quality` ∈ `EXCELLENT / GOOD / ACCEPTABLE / POOR / MISTAKE`.

## 3. 파일별 역할

| 파일 | 내용 |
|---|---|
| `models.py` | `AIReviewReport` / `AIReviewLog`, `REVIEW_SCHEMA`, `QUALITIES`, `REVIEW_*` 실패 상수 |
| `providers.py` | `FakeReviewProvider` 만. 실호출은 `src.ai.providers.OpenAIProvider` 를 그대로 재사용 |
| `engine.py` | `AIReviewEngine.review(context)` — 입력 해시 → 스키마 강제 호출 → 검증 → `REVIEW_OK` / `REVIEW_FAILED` |
| `store.py` | `ReviewRecord` + `SqliteReviewStore`/`InMemoryReviewStore` (**SSoT**), resume 키 `(cycle_id, prompt_version, model_name, review_version)` |
| `batch_runner.py` | `ReviewBatchRunner.run()` — cycles → replay → review → persist, resume-skip |
| `aggregator.py` | `RuleImprovementAggregator` — Jaccard 클러스터링 → 빈도 랭킹, `compare_reports` |
| `exporters.py` | `ReviewJsonlExporter.export(records, path, *, meta=None) -> int` |

### 주요 시그니처

- `AIReviewEngine(provider, config=None, log_store=None, monotonic=time.monotonic)` — `engine.py:59`
- `.review(context) -> AIReviewReport` — `engine.py:67`
- `ReviewBatchRunner(journal, replay_system, review_engine, review_store, *, config=None, sleep=..., clock=..., now_iso=None)` — `batch_runner.py:67`
- `.run(*, start=None, end=None, cycle_ids=None, resume=True, limit=None) -> RunSummary` — `batch_runner.py:81`
- `RuleImprovementAggregator(review_store, *, similarity_threshold=0.6, top_n=10)` — `aggregator.py:95`

## 4. 다른 모듈과의 관계

`replay`(입력), `ai.providers`(실 LLM 재사용), `journal`(duck-typed).
`src/cli/inspect.py` 가 `aggregator` + `ReviewRecord` 를 read-only 로 읽는다.
스크립트: `scripts/run_reviews.py`, `scripts/aggregate_reviews.py`.

## 5. 절대 하면 안 되는 것

- ❌ **주문/브로커/파이프라인에 접근하지 않는다.** *"입력은 ReplayContext 만."*
  시스템 프롬프트에도 박혀 있다: *"You do NOT place orders, and you do NOT make real-time trading decisions."*
- ❌ **strategy.yaml / Rule Engine 을 자동 수정하지 않는다.** `aggregator.py:3-6`:
  *"**제안만 한다.** … 실제 변경은 사람 승인 후 별도."*
- ❌ **ReplayContext 를 변경하지 않는다** (`to_prompt_dict` 로 새 dict 만 읽는다).
- ❌ 실패를 예외로 터뜨리지 않는다 — timeout / rate limit / invalid JSON / schema mismatch 는
  전부 `REVIEW_FAILED` 로 안전 강등되고 **그대로 영속된다**(재실행 시 중복 호출 방지).
- ❌ 코어에 시각·난수를 넣지 않는다. `created_at`/`elapsed` 는 `now_iso`/`clock` 으로 주입.
- ❌ JSONL 을 진실의 원천으로 쓰지 않는다 — *"ReviewStore(SQLite)가 Single Source of Truth."*

## 6. 사용 예시

```python
from src.journal.sqlite_store import SqliteJournalStore
from src.replay.system import ReplaySystem
from src.review.batch_runner import ReviewBatchRunner
from src.review.engine import AIReviewEngine, ReviewConfig
from src.review.providers import FakeReviewProvider
from src.review.store import SqliteReviewStore
from src.review.aggregator import RuleImprovementAggregator

journal = SqliteJournalStore("data/journal.sqlite")
store   = SqliteReviewStore("data/reviews.sqlite")
runner  = ReviewBatchRunner(journal, ReplaySystem(journal),
                            AIReviewEngine(FakeReviewProvider(response={...}), ReviewConfig()),
                            store)
summary = runner.run(resume=True, limit=50)

report = RuleImprovementAggregator(store, top_n=10).aggregate(prompt_version="review_v1")
```

CLI: `scripts/run_reviews.py` / `scripts/aggregate_reviews.py`
조회: `./.venv/bin/python scripts/inspect_cli.py improvements --top 5`

## 7. 테스트

`tests/test_review.py`(engine), `tests/test_review_batch.py`(runner+store+exporter),
`tests/test_review_aggregator.py`(클러스터링/비교).
