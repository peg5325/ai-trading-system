# src/runtime — 운영 모드 (PaperRuntime)

## 1. 목적

`PaperTradingLoop` 를 감싸는 **운영 래퍼**: 라이프사이클(run/stop/pause/resume), 헬스체크,
`status.json` 대시보드, 크래시 복구, 사이클 메트릭 영속화, 종료 시 마크다운 리포트.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `loop`, `config: RuntimeConfig`, (옵션) `loggers`, `metrics_store`, 주입 가능한 `monotonic`/`sleep`/`now_iso` |
| 출력 | `run()` → 리포트 경로 `str` |
| 부수효과 | `status*.json`(atomic write), rotating logs, `metric_cycle` 행, `reports/paper_report_YYYYMMDD.md` |

## 3. 파일별 역할

| 파일 | 내용 |
|---|---|
| `config.py` | 중첩 `runtime.yaml` → 평탄한 `RuntimeConfig`. `load_yaml()` 이 PyYAML lazy import |
| `logging_setup.py` | `runtime`/`error`/`trade`/`review` rotating logger 생성 |
| `metrics.py` | `RuntimeMetrics` — 인메모리 **누적** 카운터 (cycles, AI approve/wait/reject, orders, errors …) |
| `metrics_store.py` | **사이클별 delta**(`CycleMetric`)를 SQLite 에 append. System Review·대시보드의 원천 |
| `paper_runtime.py` | `PaperRuntime` — 루프 구동, SQLite lock 재시도, 예외 흡수, 헬스체크, graceful shutdown |
| `report.py` | `generate_markdown()` / `write_report()` — 종료 리포트 |

### 주요 시그니처

- `PaperRuntime(loop, config, loggers=None, monotonic=..., sleep=..., metrics_store=None, now_iso=None)` — `paper_runtime.py:36`
- `.run(max_iterations=None, install_signal_handler=False, after_cycle=None) -> str` — `paper_runtime.py:56`
- `.health_check() -> dict` — `paper_runtime.py:143` → `{"status": "OK"|"DEGRADED", "api", "ai", "broker", "journal", "replay"}`
- `RuntimeConfig.from_dict(d)` / `load_yaml(path)` — `config.py:49`, `:95`
- `SqliteMetricsStore(db_path)` `.record()` `.query()` `.aggregate()` — `metrics_store.py:154`

## 4. 다른 모듈과의 관계

`live`(감싸는 대상), `journal`, `ai`, `pipeline`, `risk`, `order`, `exits` 를 참조.
`pnl.journal_pnl` / `portfolio.performance` 는 lazy import.
`scripts/run_paper.py` 가 진입점이고, `src/cli/inspect.py` 는 `RuntimeConfig` 만 빌려 쓴다.

## 5. 절대 하면 안 되는 것

- ❌ **주문을 내지 않는다.** 관측·운영 전용 (`metrics_store.py:6`: *"매매/주문 없음"*).
- ❌ **관측 실패가 매매를 멈추면 안 된다.** `metrics_store.record` / 실현손익 / 성과 요약 실패는
  모두 catch 후 로깅만 하고 넘어간다 (`paper_runtime.py:139`, `:278`, `:291`).
- ❌ **`status.json` 을 직접 덮어쓰지 않는다.** 임시파일 + `os.replace` 로 원자적으로 쓴다(`:195-198`).
- ❌ **`position_store` 를 실현손익의 원천으로 삼지 않는다.** `_paper_pnl()` 은 `loop.account`
  (`PortfolioAccount`, FIFO)를 읽는다 — *"position_store 와의 desync 방지"* (`:201`).
- ❌ 코어에 현재 시각을 박지 않는다. `created_at` 은 `now_iso` 로 주입한다.

> `RuntimeMetrics` 는 **누적 총합**, `metric_cycle` 행은 **사이클 delta**(`cycle_delta(before, after)`).
> 둘을 혼동하지 말 것.

## 6. 사용 예시

```bash
./.venv/bin/python scripts/run_paper.py config/runtime_mixed.yaml   # Ctrl+C = graceful shutdown
```

```python
from src.runtime.config import load_yaml
from src.runtime.metrics_store import SqliteMetricsStore
from src.runtime.paper_runtime import PaperRuntime

cfg = load_yaml("config/runtime.yaml")
runtime = PaperRuntime(loop, cfg, metrics_store=SqliteMetricsStore(cfg.metrics_db))
report_path = runtime.run(install_signal_handler=True)   # -> reports/paper_report_YYYYMMDD.md
```

## 7. 테스트

`tests/test_runtime.py`, `tests/test_metrics_store.py`.
