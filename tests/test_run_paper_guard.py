"""run_paper.main() 통합: startup guard 가 스토어 생성 '이전'에 실행되는 순서 불변식.

이 순서가 PR 1-A 의 핵심(과거 DB namespace 재사용 방지)이다. 스토어는 open 만으로
파일을 만들기 때문에, guard 를 스토어보다 뒤에 두면 실패해도 이미 DB 가 생긴다.
누군가 순서를 뒤집으면 이 회귀 테스트가 깨지도록 고정한다. 실주문/실 Toss/AI 없음.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_run_paper():
    path = os.path.join(_REPO, "scripts", "run_paper.py")
    spec = importlib.util.spec_from_file_location("run_paper_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Boom:
    """스토어/프로바이더가 guard 통과 전에 생성되면 즉시 실패."""
    def __init__(self, *a, **k):
        raise AssertionError("store/provider constructed before guard passed")


def test_main_runs_guard_before_constructing_stores(tmp_path, monkeypatch):
    from src.runtime.config import RuntimeConfig
    from src.runtime.isolation import NamespaceCollisionError

    monkeypatch.setenv("TOSS_CLIENT_ID", "id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "secret")

    journal = str(tmp_path / "journal.sqlite")
    market_db = str(tmp_path / "market.sqlite")
    metrics_db = str(tmp_path / "metrics.sqlite")
    # namespace 를 이미 점유(과거 세션 DB 존재) → FRESH 시작은 거부돼야 한다
    with open(journal, "wb") as f:
        f.write(b"SQLite format 3\x00old-006-data")

    cfg = RuntimeConfig(
        symbols=["QQQ"], initial_capital=2000.0,
        journal_db=journal, candle_db=market_db, metrics_db=metrics_db,
        session_id="006", run_id="006", start_mode="FRESH", market="US", currency="USD",
        log_dir=str(tmp_path / "logs"), status_path=str(tmp_path / "s.json"),
        reports_dir=str(tmp_path / "reports"))

    rp = _load_run_paper()
    monkeypatch.setattr(rp, "load_yaml", lambda _p: cfg)
    for name in ("TossMarketDataProvider", "CandleStore", "SqliteJournalStore",
                 "SqliteMetricsStore"):
        monkeypatch.setattr(rp, name, _Boom)

    # guard 가 스토어보다 먼저 실행 → NamespaceCollisionError(스토어 미생성).
    # 순서가 뒤집히면 _Boom 의 AssertionError 로 이 테스트가 깨진다.
    with pytest.raises(NamespaceCollisionError):
        rp.main("ignored.yaml")

    # 스토어가 생성되지 않았으므로 새 DB 파일이 생기지 않는다
    assert not os.path.exists(market_db)
    assert not os.path.exists(metrics_db)
