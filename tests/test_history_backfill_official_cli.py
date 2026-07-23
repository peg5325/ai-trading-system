"""공식 P0-US30 backfill CLI 계약(이슈 #72).

fail-closed 불변식(자격증명/ API/DB 접근 전에 거부):
- --completed-through 누락 → exit 2.
- 형식 오류 → exit 2.
- 미완료(오늘 마감 전)·미래·비거래일 cutoff → exit 2.
- 캘린더 범위 밖 cutoff → exit 2.
- P0-US30(US) 유니버스 밖 심볼 → exit 2.
happy-path: 검증된 cutoff·유니버스 기본 심볼이 backfill 로 전달.

load_settings 를 '접근 시 실패'로 몽키패치해 창구 전 fail-closed 를 증명한다(실 creds/ API/DB 0).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import scripts.history_backfill_official as off


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


@pytest.fixture
def guard_creds(monkeypatch):
    """load_settings/Provider/Store/backfill 접근 시 즉시 실패 — 창구 전 fail-closed 증명."""
    def _boom(*a, **k):
        raise AssertionError("자격증명/ API/DB 창구가 fail-closed 전에 접근됨")
    monkeypatch.setattr(off, "load_settings", _boom)
    monkeypatch.setattr(off, "TossMarketDataProvider", _boom)
    monkeypatch.setattr(off, "HistoryStore", _boom)
    monkeypatch.setattr(off, "backfill_symbols", _boom)


# 1m 기본 수집은 POST(애프터마켓)까지 포함 → 완료 경계 20:00 ET. 그 뒤 시각을 기준으로 둔다
# (2025-07-15 21:00 EDT = 2025-07-16 01:00 UTC) → 2025-07-15 는 POST 까지 완료.
AFTER_CLOSE = _utc("2025-07-16T01:00")


def test_missing_completed_through_fails_closed(guard_creds, capsys):
    assert off.main(["--interval", "1m"], now_utc=AFTER_CLOSE) == 2
    assert "completed-through" in capsys.readouterr().err


def test_bad_format_fails_closed(guard_creds):
    assert off.main(["--completed-through", "2025/07/15"], now_utc=AFTER_CLOSE) == 2


def test_incomplete_today_before_close_fails_closed(guard_creds):
    # 오늘(2025-07-15) 15:00 EDT — 아직 마감 전 → 오늘을 cutoff 로 요청하면 거부.
    before_close = _utc("2025-07-15T19:00")
    assert off.main(["--completed-through", "2025-07-15"], now_utc=before_close) == 2


def test_future_date_fails_closed(guard_creds):
    assert off.main(["--completed-through", "2025-07-16"], now_utc=AFTER_CLOSE) == 2


def test_non_trading_day_fails_closed(guard_creds):
    # 2025-07-19 토요일
    assert off.main(["--completed-through", "2025-07-19"], now_utc=_utc("2025-07-21T20:30")) == 2


def test_out_of_calendar_range_fails_closed(guard_creds):
    assert off.main(["--completed-through", "2022-12-31"], now_utc=AFTER_CLOSE) == 2


def test_non_us_universe_symbol_rejected(guard_creds):
    # 005930(코스피)은 P0-US30(US) 유니버스 밖 → 거부(NYSE 규칙 추측 금지).
    assert off.main(["--completed-through", "2025-07-15", "--symbols", "005930"],
                    now_utc=AFTER_CLOSE) == 2


def test_unknown_symbol_rejected(guard_creds):
    assert off.main(["--completed-through", "2025-07-15", "--symbols", "QQQ,FAKE"],
                    now_utc=AFTER_CLOSE) == 2


def test_happy_path_passes_validated_cutoff_and_universe(monkeypatch):
    """검증 통과 시: 유니버스 기본 심볼·검증된 cutoff(date)·1m 세션필터가 backfill 로 전달."""
    from datetime import date as _date

    captured = {}

    class FakeSettings:
        has_credentials = True

    class FakeReport:
        def as_dict(self):
            return {"interval": "1m", "results": []}
        results = []

    class FakeStore:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeProv:
        def __init__(self, settings): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_backfill(store, prov, symbols, interval, **kw):
        captured["symbols"] = symbols
        captured["interval"] = interval
        captured["completed_through"] = kw.get("completed_through")
        captured["session_filter"] = kw.get("session_filter")
        return FakeReport()

    monkeypatch.setattr(off, "load_settings", lambda: FakeSettings())
    monkeypatch.setattr(off, "TossMarketDataProvider", FakeProv)
    monkeypatch.setattr(off, "HistoryStore", lambda db: FakeStore())
    monkeypatch.setattr(off, "backfill_symbols", fake_backfill)

    code = off.main(["--completed-through", "2025-07-15", "--json"], now_utc=AFTER_CLOSE)
    assert code == 0
    # 기본 심볼 = P0-US30 유니버스 30종목
    assert len(captured["symbols"]) == 30
    assert "QQQ" in captured["symbols"] and "NVDA" in captured["symbols"]
    # 검증된 cutoff 가 date 로 전달
    assert captured["completed_through"] == _date(2025, 7, 15)
    # 1m 세션 필터 기본(PRE/REGULAR/POST)
    assert captured["session_filter"] is not None
