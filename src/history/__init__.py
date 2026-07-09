"""통합 히스토리 캔들 저장소(market_history.sqlite).

session별로 파편화된 candle 을 단일 저장소로 병합한다(read-only import + idempotent upsert).
Runtime/Paper Trading 과 무관한 오프라인 데이터 인프라. 실주문/외부 API 호출 없음.
설계: docs/MARKET_HISTORY_BACKFILL_DESIGN.md
"""
from src.history.quality import quality_report
from src.history.store import HistoryStore, ImportStats

__all__ = ["HistoryStore", "ImportStats", "quality_report"]
