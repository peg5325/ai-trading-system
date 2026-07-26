"""하위호환 shim — 집계 core 는 `src/bars/resample.py` 로 이관됐다(이슈 #76 리뷰 R1).

기존 연구 호출부(`from src.history.resample import resample_1m_to_5m` 등)를 그대로 유지하기
위한 re-export 다. **새 코드는 `src.bars.resample` 을 직접 import 한다.**

이관 이유: `src/engines`(라이브)와 `src/history`(오프라인 인프라)가 같은 kernel 을 쓰는데,
core 가 `src/history` 안에 있으면 라이브 import 경로가 `src/history/__init__.py`(loader·store·
quality·sqlite3)를 실행하고, `src/history/candidate_stream` 이 engines 를 되짚어 import 하므로
패키지 초기화 순환에 노출된다.
"""
from src.bars.resample import (  # noqa: F401
    SUPPORTED_BUCKETS,
    bucket_minutes_from_rule,
    opening_range,
    resample_1m,
    resample_1m_to_5m,
    resample_1m_to_15m,
)

__all__ = [
    "SUPPORTED_BUCKETS", "bucket_minutes_from_rule", "opening_range",
    "resample_1m", "resample_1m_to_5m", "resample_1m_to_15m",
]
