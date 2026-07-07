"""System Review Engine — 시스템 자체(운영 품질)를 기간 단위로 AI 회고.

매매 전략을 평가하지 않는다(그건 AIReviewEngine 담당). 오직 **운영 품질**만 본다:
runtime health / api_errors / exceptions / sqlite_locks / loop latency / candidate·order·
AI 비율 / risk block / skipped / degraded 등. 제안만 하며 주문/Rule Engine/strategy 에
절대 영향 주지 않는다.

입력은 RuntimeMetricsStore(read-only) + JournalStore 뿐. Replay/TradingPipeline 미사용.
"""
from src.system_review.engine import SystemReviewConfig, SystemReviewEngine
from src.system_review.models import (
    GRADES,
    SYSTEM_REVIEW_FAILED,
    SYSTEM_REVIEW_OK,
    SystemReviewLog,
    SystemReviewReport,
)
from src.system_review.store import (
    InMemorySystemReviewStore,
    SqliteSystemReviewStore,
    SYSTEM_REVIEW_VERSION,
)
from src.system_review.window import build_window_summary

__all__ = [
    "SystemReviewEngine", "SystemReviewConfig",
    "SystemReviewReport", "SystemReviewLog",
    "GRADES", "SYSTEM_REVIEW_OK", "SYSTEM_REVIEW_FAILED",
    "SqliteSystemReviewStore", "InMemorySystemReviewStore", "SYSTEM_REVIEW_VERSION",
    "build_window_summary",
]
