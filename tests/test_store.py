"""CandleStore 저장/조회 라운드트립 (네트워크 없음)."""
from __future__ import annotations

from datetime import datetime

from src.models import Candle
from src.storage.candle_store import CandleStore


def _candle(minute: int, close: float) -> Candle:
    moment = datetime.fromisoformat(f"2026-07-06T09:{minute:02d}:00-04:00")  # ET
    return Candle.from_moment(
        symbol="SOXL", interval="1m", moment=moment,
        o=close, h=close, low=close, c=close, v=100,
        currency="USD", adjusted=True,
    )


def test_upsert_count_range_latest(tmp_path):
    db = tmp_path / "t.sqlite"
    with CandleStore(db) as store:
        store.upsert_many([_candle(30, 1.0), _candle(31, 2.0), _candle(32, 3.0)])
        assert store.count("SOXL", "1m") == 3

        lo, hi = store.range("SOXL", "1m")
        assert lo is not None and hi is not None and lo < hi

        latest = store.latest("SOXL", "1m", limit=1)
        assert latest[0].close == 3.0  # 09:32 가 가장 최신
        assert latest[0].session == "REGULAR"


def test_upsert_is_idempotent(tmp_path):
    db = tmp_path / "t.sqlite"
    with CandleStore(db) as store:
        store.upsert_many([_candle(30, 1.0)])
        store.upsert_many([_candle(30, 9.9)])  # 같은 키 -> 갱신
        assert store.count("SOXL", "1m") == 1
        assert store.latest("SOXL", "1m", 1)[0].close == 9.9
