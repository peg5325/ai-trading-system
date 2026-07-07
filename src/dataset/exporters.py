"""DatasetExporter — 공통 Dataset 을 파일 포맷으로 내보낸다.

포맷별 역할:
- JSONL  : GPT/Claude Review, few-shot prompt, 회귀 평가, LLM 분석, Explainable AI.
           각 줄 = {feature, label, meta} 중첩 dict (자기기술적).
- Parquet: Pandas / DuckDB / Polars, feature engineering, 통계, 향후 ML(XGBoost/LightGBM).
           평탄화된 컬럼(list→콤마 문자열, forward 창→개별 컬럼).
CSV 는 MVP 후순위(Parquet 가 대체). 필요 시 추후 추가.
"""
from __future__ import annotations

import json
from pathlib import Path

from src.dataset.models import Dataset


class DatasetExporter:
    """인터페이스. export(dataset, path) → 기록한 row 수 반환."""
    def export(self, dataset: Dataset, path: str | Path) -> int:
        raise NotImplementedError


class JsonlExporter(DatasetExporter):
    """한 줄 = 한 record 의 중첩 JSON. 옵션으로 meta 헤더 라인 포함."""
    def __init__(self, include_meta_header: bool = True, ensure_ascii: bool = False):
        self._meta_header = include_meta_header
        self._ensure_ascii = ensure_ascii

    def export(self, dataset: Dataset, path: str | Path) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with open(path, "w", encoding="utf-8") as f:
            if self._meta_header:
                f.write(json.dumps({"__meta__": dataset.to_meta_dict()},
                                   ensure_ascii=self._ensure_ascii) + "\n")
            for rec in dataset:
                f.write(json.dumps(rec.to_dict(), ensure_ascii=self._ensure_ascii) + "\n")
                n += 1
        return n


class ParquetExporter(DatasetExporter):
    """평탄화된 테이블을 Parquet 로 저장. pandas + pyarrow 필요."""
    def __init__(self, compression: str = "snappy"):
        self._compression = compression

    def export(self, dataset: Dataset, path: str | Path) -> int:
        try:
            import pandas as pd
        except ImportError as e:   # pragma: no cover
            raise RuntimeError("ParquetExporter 는 pandas 가 필요합니다: pip install pandas pyarrow") from e
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = dataset.to_flat_dicts()
        df = pd.DataFrame(rows, columns=_columns(rows))
        try:
            df.to_parquet(path, compression=self._compression, index=False)
        except (ImportError, ValueError) as e:   # pyarrow 미설치 등
            raise RuntimeError("Parquet 저장 실패 — pyarrow 설치 필요: pip install pyarrow") from e
        return len(rows)


def _columns(rows: list[dict]) -> list[str]:
    """모든 row 의 키 합집합을 결정론적 순서로. (forward 창 컬럼이 row마다 동일하도록)"""
    seen: dict[str, None] = {}
    for r in rows:
        for k in r:
            seen.setdefault(k, None)
    return list(seen.keys())
