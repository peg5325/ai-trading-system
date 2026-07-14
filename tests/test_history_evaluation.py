"""분리 평가 경로(PR 3-B) — 시장/세션/out-of-time split + random 금지(계약 §3-7)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.history import HistoryStore, quality_report
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
from src.history.store import ImportStats
from src.models import Candle


def _c(symbol: str, day: int, minute: int, close: float = 100.0,
       currency: str = "USD") -> Candle:
    ts = datetime(2026, 7, day, 14, minute, tzinfo=timezone.utc)
    return Candle.from_moment(symbol=symbol, interval="1m", moment=ts,
                             o=close, h=close + 1, low=close - 1, c=close, v=1000.0,
                             currency=currency, adjusted=True)


def _make_history(dbp: str, candles: list[Candle]) -> None:
    with HistoryStore(dbp) as store:
        st = ImportStats(source="toss_backfill")
        store.upsert_candles(candles, source="toss_backfill", run_id="test", stats=st)
        store.commit()


def test_quality_report_surfaces_benchmark_coverage(tmp_path):
    """세션 import(기존 경로) 후 benchmark QQQ coverage 가 품질 리포트에 나타난다(PR 3-B 수용)."""
    dbp = str(tmp_path / "market_history.sqlite")
    _make_history(dbp, [_c("QQQ", 7, m) for m in range(3)]
                       + [_c("NVDA", 7, m) for m in range(2)])
    rep = quality_report(dbp, interval="1m")
    syms = {s["symbol"] for s in rep["symbols"]}
    assert "QQQ" in syms  # benchmark coverage 노출
    qqq = next(s for s in rep["symbols"] if s["symbol"] == "QQQ")
    assert qqq["market"] == "US" and qqq["total_bars"] == 3


def test_list_slices_market_separated(tmp_path):
    dbp = str(tmp_path / "market_history.sqlite")
    _make_history(dbp, [_c("QQQ", 7, 0), _c("QQQ", 8, 0),
                        _c("005930", 7, 0, close=70000.0, currency="KRW")])
    slices = list_slices(dbp, "1m")
    assert all(isinstance(s, Slice) for s in slices)
    by = {(s.market, s.symbol): s for s in slices}
    assert ("US", "QQQ") in by and ("KR", "005930") in by  # 시장별 분리
    assert by[("US", "QQQ")].bars == 2 and by[("US", "QQQ")].days == 2
    assert by[("KR", "005930")].bars == 1


def test_group_by_session_deterministic(tmp_path):
    # 입력을 뒤섞어도(날짜 8 먼저) 세션 그룹은 날짜 정렬·내부 시간순
    candles = [_c("QQQ", 8, 1), _c("QQQ", 7, 1), _c("QQQ", 7, 0), _c("QQQ", 8, 0)]
    groups = group_by_session(candles)
    days = list(groups)
    assert days == sorted(days)  # 날짜 오름차순 결정론
    assert len(days) == 2
    for d in days:
        mins = [c.ts_utc.minute for c in groups[d]]
        assert mins == sorted(mins)  # 세션 내 시간순


def test_chronological_split_with_embargo():
    candles = [_c("QQQ", 7, m) for m in range(10)]
    train, test = chronological_split(candles, ratio=0.6, embargo_bars=2)
    assert [c.ts_utc.minute for c in train] == [0, 1, 2, 3, 4, 5]
    # k=6, embargo 2 → test 는 인덱스 8부터
    assert [c.ts_utc.minute for c in test] == [8, 9]
    # train 이 test 보다 시간상 앞(누수 없음)
    assert train[-1].ts_utc < test[0].ts_utc


def test_leave_one_session_out_no_leakage():
    candles = ([_c("QQQ", 7, 0), _c("QQQ", 7, 1)]
               + [_c("QQQ", 8, 0)] + [_c("QQQ", 9, 0)])
    folds = list(leave_one_session_out(candles))
    assert len(folds) == 3  # 세션(거래일) 3개
    for held, train, test in folds:
        test_days = {c.ts_exchange.date() for c in test}
        train_days = {c.ts_exchange.date() for c in train}
        assert test_days == {held}
        assert held not in train_days  # held-out 세션은 train 에 없음


def test_official_split_rejects_random():
    candles = [_c("QQQ", 7, m) for m in range(4)]
    with pytest.raises(ValueError) as ei:
        official_split(candles, "random")
    assert RANDOM_CV_WATERMARK in str(ei.value)  # 워터마크 명시
    assert "random" not in OFFICIAL_SPLIT_METHODS  # 공식 목록에 없음


def test_official_split_chronological_and_embargo():
    candles = [_c("QQQ", 7, m) for m in range(10)]
    tr, te = official_split(candles, "chronological", ratio=0.7)
    assert len(tr) == 7 and len(te) == 3
    # embargo 는 gap 을 강제 — embargo_bars=0 은 무의미하므로 거부
    with pytest.raises(ValueError):
        official_split(candles, "embargo", ratio=0.7, embargo_bars=0)
    tr2, te2 = official_split(candles, "embargo", ratio=0.7, embargo_bars=1)
    assert len(tr2) == 7 and len(te2) == 2  # 인덱스 7 embargo, 8~9 test


def test_official_split_unknown_method_raises():
    candles = [_c("QQQ", 7, m) for m in range(4)]
    with pytest.raises(ValueError):
        official_split(candles, "kfold_random")


def test_chronological_split_rejects_degenerate():
    # 빈 train (ratio 너무 작아 k=0)
    with pytest.raises(ValueError):
        chronological_split([_c("QQQ", 7, 0), _c("QQQ", 7, 1)], ratio=0.3)
    # 빈 test (embargo 가 잔여 봉 이상)
    with pytest.raises(ValueError):
        chronological_split([_c("QQQ", 7, m) for m in range(5)], ratio=0.6, embargo_bars=10)


def test_official_split_loso_requires_two_sessions():
    single = [_c("QQQ", 7, 0), _c("QQQ", 7, 1)]  # 세션(거래일) 1개
    with pytest.raises(ValueError):
        official_split(single, "loso")
    # 2개 세션이면 정상
    two = single + [_c("QQQ", 8, 0)]
    folds = official_split(two, "loso")
    assert len(folds) == 2


def test_random_split_diagnostic_deterministic_and_watermarked():
    candles = [_c("QQQ", 7, m) for m in range(10)]
    tr1, te1, wm = random_split_diagnostic(candles, ratio=0.7, seed=42)
    tr2, te2, _ = random_split_diagnostic(candles, ratio=0.7, seed=42)
    assert wm == RANDOM_CV_WATERMARK
    assert [c.ts_utc for c in tr1] == [c.ts_utc for c in tr2]  # seed 결정론
    assert len(tr1) == 7 and len(te1) == 3
    # train/test 무겹침(전체 커버)
    assert len(tr1) + len(te1) == len(candles)
    # 입력 순서와 무관(정렬 후 shuffle) — 뒤섞어 넣어도 동일 결과
    tr3, te3, _ = random_split_diagnostic(list(reversed(candles)), ratio=0.7, seed=42)
    assert [c.ts_utc for c in tr3] == [c.ts_utc for c in tr1]
