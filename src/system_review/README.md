# src/system_review — 운영 품질 회고 (SystemReviewEngine)

## 1. 목적

**시스템 자체의 운영 품질**을 기간 단위로 AI 회고하고 A–F 등급을 매긴다.
에러/지연/sqlite lock/degraded 사이클/AI 승인비율 같은 **인프라 지표만** 본다.
매매 전략이나 손익은 평가하지 않는다.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `RuntimeMetricsStore`(read-only, `.aggregate(start, end)`), `JournalStore`(`.get_by_time_range`), `AIProvider`, `[start, end]` |
| 출력 | `SystemReviewReport(grade, summary, major_issues[], recommendations[], confidence, runtime_window)` |

`grade` ∈ `A/B/C/D/F` 또는 `UNKNOWN`.

## 3. 파일별 역할

| 파일 | 내용 |
|---|---|
| `models.py` | `SystemReviewReport` / `SystemReviewLog`, `SYSTEM_REVIEW_SCHEMA`, `GRADES`, 실패 상수 |
| `window.py` | `build_window_summary()` — metrics + journal → `{window, runtime, journal, data_quality}` (결정론, read-only) |
| `engine.py` | `SystemReviewEngine.review(start=, end=)` — 윈도우 구성 → 해시 → GPT → 검증 → 안전 강등 |
| `store.py` | `SqliteSystemReviewStore` / `InMemorySystemReviewStore` — **순수 append** 로그 |

### 주요 시그니처

- `SystemReviewEngine(provider, metrics_store, journal_store, config=None, log_store=None, monotonic=..., now_iso=None)` — `engine.py:62`
- `.review(*, start=None, end=None) -> SystemReviewReport` — `engine.py:75`
- `SystemReviewConfig(prompt_version="system_review_v1", temperature=0.0, timeout_s=20.0)` — `engine.py:54`
- `build_window_summary(metrics_store, journal_store, *, start=None, end=None) -> dict` — `window.py:13`

## 4. 다른 모듈과의 관계

`src/` 에서 유일하게 import 하는 것은 `src.ai.models`(예외 타입 3개)뿐이다.
`MetricsStore` / `JournalStore` 는 **duck-typed 로 주입**받으므로 `runtime`·`journal` 패키지에
소스 레벨 의존이 없다.

**이 모듈을 import 하는 `src/` 모듈은 없다.** 소비자는 `scripts/system_review.py`(실행)와
`src/cli/inspect.py`(`system-review` 서브커맨드가 DB 를 read-only 로 직접 조회)다.
`src/review/` 와도 소스 레벨 import 의존이 **양방향 모두 없다**(테스트에서만 `FakeReviewProvider` 를 공유).

### `review` 와 무엇이 다른가

| | `src/review/` | `src/system_review/` |
|---|---|---|
| 단위 | `cycle_id` 하나 | 시간 **윈도우** `[start, end]` |
| 입력 | `ReplayContext` | `MetricsStore.aggregate()` + `Journal` |
| Replay 사용 | 필요 | **미사용** |
| 평가 대상 | 매매 결정 품질 | 운영/인프라 품질 |
| 판정 | `decision_quality` (EXCELLENT…MISTAKE) | `grade` (A…F) |
| 저장 | resume 키 upsert | **append-only** 로그 |
| 개선 출구 | `rule_improvement` → 전략 변경 후보 | `recommendations` → 인프라 수정 |

> 전략 **개선 루프**는 `src/review/aggregator.py` 에 있다. 이 모듈은 운영 권고만 낸다.
> 두 패키지는 소스 레벨 import 의존이 **없다**.

## 5. 절대 하면 안 되는 것

- ❌ **매매 전략을 평가하지 않는다.** `__init__.py`: *"매매 전략을 평가하지 않는다(그건 AIReviewEngine 담당).
  오직 **운영 품질**만 본다."*
- ❌ **주문 / Rule Engine / strategy 에 절대 영향 주지 않는다.** 제안만 한다.
- ❌ **Replay / TradingPipeline 을 사용하지 않는다.** 입력은 MetricsStore + JournalStore 뿐.
- ❌ MetricsStore 에 쓰지 않는다 — `aggregate()` read-only 접근만(`window.py:16`).
- ❌ `input_hash` 에 `created_at`/`latency` 를 넣지 않는다 — window+prompt+model 로만 계산(재현성).
- ❌ 권고는 **운영/신뢰성/관측성 수정**으로 제한한다(시스템 프롬프트가 강제).

## 6. 사용 예시

```python
from datetime import datetime, timezone
from src.journal.sqlite_store import SqliteJournalStore
from src.review.providers import FakeReviewProvider
from src.runtime.metrics_store import SqliteMetricsStore
from src.system_review.engine import SystemReviewConfig, SystemReviewEngine
from src.system_review.store import SqliteSystemReviewStore

engine = SystemReviewEngine(
    FakeReviewProvider(response={"grade": "A", "summary": "stable",
                                 "major_issues": [], "recommendations": [], "confidence": 90}),
    SqliteMetricsStore("data/metrics.sqlite"),      # read-only 로만 사용
    SqliteJournalStore("data/journal.sqlite"),
    config=SystemReviewConfig(timeout_s=20.0),
    log_store=SqliteSystemReviewStore("data/system_reviews.sqlite"),
)
report = engine.review(start=datetime(2026, 7, 7, tzinfo=timezone.utc),
                       end=datetime(2026, 7, 8, tzinfo=timezone.utc))
report.grade    # "A"
```

CLI: `./.venv/bin/python scripts/system_review.py` (기본 최근 24h, 기본 fake provider)
조회: `./.venv/bin/python scripts/inspect_cli.py system-review --latest`

## 7. 테스트

`tests/test_system_review.py` — 정상 / 빈 데이터 / timeout / invalid json / rate limit /
schema mismatch / 저장 / roundtrip / `input_hash` 재현성 / window 요약 / store CRUD.
