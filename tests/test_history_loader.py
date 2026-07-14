"""history loader(PR 3-B) — market_history.sqlite read-only 로더 검증.

- read-only(원본 DB 무변경), 시간순 정렬, 기간 필터, market/필드 복원.
- BacktestEngine 입력용 canonical Candle 을 그대로 반환.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.history import HistoryStore, load_candles
from src.history.store import ImportStats
from src.models import Candle


def _c(symbol: str, day: int, minute: int, close: float = 100.0,
       currency: str = "USD") -> Candle:
    ts = datetime(2026, 7, day, 14, minute, tzinfo=timezone.utc)
    return Candle.from_moment(symbol=symbol, interval="1m", moment=ts,
                             o=close, h=close + 1, low=close - 1, c=close, v=1000.0,
                             currency=currency, adjusted=True)


def _make_history(dbp: str, candles: list[Candle], source: str = "toss_backfill") -> None:
    with HistoryStore(dbp) as store:
        st = ImportStats(source=source)
        store.upsert_candles(candles, source=source, run_id="test", stats=st)
        store.commit()


def test_load_candles_ordered_and_typed(tmp_path):
    dbp = str(tmp_path / "market_history.sqlite")
    # 입력 순서를 일부러 뒤섞어 저장해도 로더는 시간순 정렬로 반환
    _make_history(dbp, [_c("QQQ", 7, 2), _c("QQQ", 7, 0), _c("QQQ", 7, 1)])
    out = load_candles(dbp, "QQQ", "1m")
    assert [c.ts_utc.minute for c in out] == [0, 1, 2]  # 오름차순
    assert all(isinstance(c, Candle) for c in out)
    assert out[0].market == "US" and out[0].currency == "USD"
    assert out[0].symbol == "QQQ" and out[0].interval == "1m"


def test_load_candles_start_end_filter(tmp_path):
    dbp = str(tmp_path / "market_history.sqlite")
    _make_history(dbp, [_c("QQQ", 7, m) for m in range(5)])  # 14:00~14:04
    start = datetime(2026, 7, 7, 14, 1, tzinfo=timezone.utc)
    end = datetime(2026, 7, 7, 14, 3, tzinfo=timezone.utc)  # [start, end)
    out = load_candles(dbp, "QQQ", "1m", start=start, end=end)
    assert [c.ts_utc.minute for c in out] == [1, 2]  # end 는 배타적


def test_load_candles_read_only_no_mutation(tmp_path):
    dbp = str(tmp_path / "market_history.sqlite")
    _make_history(dbp, [_c("QQQ", 7, m) for m in range(3)])
    with HistoryStore(dbp) as store:
        before = store.total_rows()
    load_candles(dbp, "QQQ", "1m")
    load_candles(dbp, "QQQ", "1m", start=datetime(2026, 7, 7, 14, 1, tzinfo=timezone.utc))
    with HistoryStore(dbp) as store:
        assert store.total_rows() == before  # 로더는 원본을 변경하지 않는다


def test_load_candles_market_restored_for_kr(tmp_path):
    dbp = str(tmp_path / "market_history.sqlite")
    _make_history(dbp, [_c("005930", 7, 0, close=70000.0, currency="KRW")])
    out = load_candles(dbp, "005930", "1m")
    assert len(out) == 1 and out[0].market == "KR" and out[0].currency == "KRW"


def test_load_candles_unknown_symbol_empty(tmp_path):
    dbp = str(tmp_path / "market_history.sqlite")
    _make_history(dbp, [_c("QQQ", 7, 0)])
    assert load_candles(dbp, "NOPE", "1m") == []


def test_load_candles_market_filter(tmp_path):
    """market 지정 시 해당 시장으로 격리(시장 분리 평가 — 통화/시장 혼재 방지)."""
    dbp = str(tmp_path / "market_history.sqlite")
    _make_history(dbp, [_c("QQQ", 7, 0)])  # US
    assert len(load_candles(dbp, "QQQ", "1m", market="US")) == 1
    assert load_candles(dbp, "QQQ", "1m", market="KR") == []  # 다른 시장 격리
