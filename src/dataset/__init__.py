"""AI Learning Dataset Builder.

Journal / Candle 을 읽는 read-only batch processor. 실시간 매매에 절대 관여하지 않는다.
각 row = 특정 cycle×symbol 의 진입 관찰 지점에 대한
  FeatureRecord(진입 시점 상태) + LabelRecord(사후 outcome) + MetaRecord(재현성 버전).

공통 Dataset 객체를 유지하고 DatasetExporter 로 JSONL/Parquet 를 내보낸다.
"""
from src.dataset.builder import DatasetBuilder, LabelConfig
from src.dataset.exporters import JsonlExporter, ParquetExporter
from src.dataset.models import (
    DATASET_VERSION,
    FEATURE_VERSION,
    Dataset,
    DatasetRecord,
    FeatureRecord,
    LabelRecord,
    MetaRecord,
)

__all__ = [
    "DatasetBuilder", "LabelConfig",
    "JsonlExporter", "ParquetExporter",
    "Dataset", "DatasetRecord", "FeatureRecord", "LabelRecord", "MetaRecord",
    "FEATURE_VERSION", "DATASET_VERSION",
]
