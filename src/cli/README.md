# src/cli — 조회 CLI (inspect, read-only)

## 1. 목적

4대 SQLite SSoT(journal / review / metrics / system_review)에 대한 **엄격한 읽기 전용** 조회 CLI.
Paper 루프가 같은 파일에 쓰는 **가동 중에도 안전하게** 조회할 수 있다.

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `--config <runtime yaml>`(기본 `config/runtime_mixed.yaml`), `--db`, `--json`, `--limit/--symbol/--since/--last/--top/...` |
| 출력 | stdout — 정렬된 텍스트 테이블 또는 `--json`. 종료코드 `0` OK / `1` `DBError` |

시간 문자열: `today` \| `Nh` \| `Nm` \| `Nd` \| ISO `YYYY-MM-DDTHH:MM:SSZ`

서브커맨드(`inspect.py:744-788`):
`summary, cycles, latest, errors, candidates, orders, reviews, improvements, metrics, system-review, health, pnl`

## 3. 주요 함수

- `_connect_ro(db_path) -> sqlite3.Connection` — `inspect.py:45`.
  `file:{path}?mode=ro` + `uri=True` + `PRAGMA query_only=ON` (이중 잠금)
- `read_summary(cfg, *, journal_db=None) -> dict` — `:107`
- `read_cycles(journal_db, *, limit=20, newest_first=False, start=None, end=None)` — `:198`
- `read_orders(journal_db, *, limit=20, symbol=None)` — `:273` (`status == "FILLED"` 만)
- `read_metrics(metrics_db, *, start=None, end=None)` — `:361` / `read_health(...)` — `:434`
- `read_improvements(review_db, *, top_n=10, ...)` — `:347` / `read_pnl(journal_db, ...)` — `:656`
- `_ReadOnlyReviewAdapter` — `:326` / `_RoJournalSource` — `:634`
- `main(argv) -> int` — `:843`

## 4. 다른 모듈과의 관계

`src/review/aggregator`, `src/review/store`(타입만), `src/runtime/config` 를 import.
`src/journal/models` 와 `src/pnl/journal_pnl` 은 함수 안에서 lazy import.
**`src/` 안에서 이 모듈을 import 하는 곳은 없다** — leaf. 진입점은 `scripts/inspect_cli.py`.

## 5. 절대 하면 안 되는 것

- ❌ **쓰기 금지 — 구조적으로 강제된다.** 모든 연결은 `mode=ro` + `PRAGMA query_only=ON`.
- ❌ **Store 클래스를 인스턴스화하지 않는다.** `SqliteReviewStore` / `SqliteJournalStore` 의 생성자는
  RW 연결로 `CREATE TABLE` 을 실행한다 — *"라이브 DB 조회에 부적합."*
  그래서 `_ReadOnlyReviewAdapter` 와 `_RoJournalSource` 어댑터가 따로 존재한다.
- ❌ **매매/주문/Runtime 제어 기능을 넣지 않는다.** *"조회 전용."*
- ❌ **reader 함수 안에서 `datetime.now()` 를 부르지 않는다.** `now()` 는 argparse 계층의
  `_parse_since` 단 한 곳에만 있다 — *"결정론적 테스트를 위함."*
- ❌ DB 가 없다고 죽지 않는다 — `read_summary`/`read_errors` 는 `{"available": False, ...}` 로 강등한다.

> 진입점이 `scripts/inspect.py` 가 **아닌** 이유: `scripts/` 가 `sys.path[0]` 이라 `inspect.py` 는
> stdlib `inspect`(dataclasses 가 내부적으로 import)를 가려 `run_paper.py` 를 깨뜨린다.

## 6. 사용 예시

```bash
./.venv/bin/python scripts/inspect_cli.py summary
./.venv/bin/python scripts/inspect_cli.py pnl               # 실현손익(FIFO)
./.venv/bin/python scripts/inspect_cli.py latest --limit 5
./.venv/bin/python scripts/inspect_cli.py metrics --last 24h
./.venv/bin/python scripts/inspect_cli.py errors --since today
./.venv/bin/python scripts/inspect_cli.py improvements --top 5
./.venv/bin/python scripts/inspect_cli.py system-review --latest --json

# 동등:
./.venv/bin/python -m src.cli.inspect summary
```

## 7. 테스트

`tests/test_inspect_cli.py`. 실제 Store 로 임시 DB 를 시딩한 뒤 닫고, read-only reader 로 읽는다.
⚠️ `read_pnl` 은 import/테스트되지 않아 `pnl` 서브커맨드 경로가 미커버 상태다.
