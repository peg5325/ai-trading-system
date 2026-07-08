# src/dataset — AI 학습 데이터셋 빌더 (DatasetBuilder)

## 1. 목적

**읽기 전용 배치 처리기.** Trading Journal 과 저장된 캔들을 조인해 ML/LLM 학습 데이터셋을 만든다.
한 행 = 한 `cycle × symbol` 진입 평가 시점:
진입 시점 상태(feature) + 사후 forward 결과(label) + 재현용 출처(meta).

## 2. 입력 / 출력

| | |
|---|---|
| 입력 | `journal_store`(duck-typed: `.get_by_time_range`), (옵션) `candle_store`(`.query`) |
| 출력 | `Dataset(records: list[DatasetRecord], meta: MetaRecord)` → JSONL(중첩) / Parquet(평탄) |

## 3. 주요 클래스/함수

- `DatasetBuilder(journal_store, candle_store=None, *, config=None, interval="1m", strategy_version="screener-1.0", built_at=None)` — `builder.py:61`
- `.build(*, start=None, end=None, symbols=None, source=None) -> Dataset` — `builder.py:76`
- `LabelConfig` — `builder.py:32`: `windows_min=(5,15,30,60), primary_window_min=30,
  win_threshold=0.002, loss_threshold=0.002, require_entry_price=True, drop_incomplete_labels=False`
- `JsonlExporter(include_meta_header=True, ensure_ascii=False).export(dataset, path) -> int` — `exporters.py:24`
- `ParquetExporter(compression="snappy")` — `exporters.py:44`
- 모델: `FeatureRecord`, `ForwardMetric`, `LabelRecord`, `MetaRecord`, `DatasetRecord`, `Dataset`;
  `FEATURE_VERSION = "1.0"`, `DATASET_VERSION = "1.0"`

## 4. 다른 모듈과의 관계

`src/` 의존성이 사실상 **0** — 스토어는 전부 duck-typed 로 주입받는다.
`scripts/build_dataset.py` 만 이 모듈을 쓴다. 도크스트링대로 *"Replay/Review/Runtime 은 Dataset 을 모른다."*

## 5. 절대 하면 안 되는 것

- ❌ **원본을 변경하지 않는다.** Journal / Candle 은 read-only. 실시간 매매에 관여하지 않는다.
- ❌ **Look-ahead 금지.** forward window 는 `bisect_right(ts_list, as_of)` 에서 시작한다
  — **진입 봉 자체를 제외**한다(`builder.py:184`, `test_lookahead_excludes_entry_bar` 로 고정).
- ❌ 현재 시각·난수 사용 금지. `built_at` 은 밖에서 주입한다.
- ❌ **스키마를 조용히 바꾸지 않는다.** 변경 시 `FEATURE_VERSION` / `DATASET_VERSION` 을 올린다
  (모든 레코드가 자기 `MetaRecord` 를 들고 다닌다).
- ❌ Parquet 컬럼 순서를 비결정적으로 만들지 않는다 — `_columns()` 가 키 합집합을 정렬해 고정한다.

> MVP 갭(문서화됨): `LabelRecord.realized_pnl` / `exit_reason` 은 아직 채워지지 않는다
> (청산 매칭은 후속 과제, `models.py:91`).

## 6. 사용 예시

```python
from src.dataset.builder import DatasetBuilder, LabelConfig
from src.dataset.exporters import JsonlExporter

builder = DatasetBuilder(
    journal_store, candle_store,
    config=LabelConfig(windows_min=(5, 15, 30, 60), primary_window_min=30),
    interval="1m",
    built_at="2026-07-08T00:00:00Z",     # 결정론 — 밖에서 주입
)
dataset = builder.build(symbols={"SOXL"}, source="journal.db")
n = JsonlExporter().export(dataset, "datasets/dataset.jsonl")
```

CLI: `./.venv/bin/python scripts/build_dataset.py`

## 7. 테스트

`tests/test_dataset.py` — forward metric, look-ahead 제외, WIN/LOSS/NEUTRAL 분류,
incomplete label, feature/meta 필드, 심볼 필터 + 결정론, JSONL/Parquet 라운드트립.
