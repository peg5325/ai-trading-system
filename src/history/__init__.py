"""통합 히스토리 캔들 저장소(market_history.sqlite).

session별로 파편화된 candle 을 단일 저장소로 병합한다(read-only import + idempotent upsert).
Runtime/Paper Trading 과 무관한 오프라인 데이터 인프라. 실주문/외부 API 호출 없음.
설계: docs/MARKET_HISTORY_BACKFILL_DESIGN.md
"""
from src.history.evaluation import (
    OFFICIAL_SPLIT_METHODS,
    RANDOM_CV_WATERMARK,
    Slice,
    chronological_split,
    group_by_session,
    leave_one_session_out,
    list_slices,
    official_split,
    random_split_diagnostic,
)
from src.history.loader import load_candles
from src.history.quality import quality_report
from src.history.store import HistoryStore, ImportStats

__all__ = [
    "HistoryStore", "ImportStats", "quality_report", "load_candles",
    "Slice", "list_slices", "group_by_session", "chronological_split",
    "leave_one_session_out", "official_split", "random_split_diagnostic",
    "OFFICIAL_SPLIT_METHODS", "RANDOM_CV_WATERMARK",
]
