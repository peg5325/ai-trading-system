"""resampler_parity_check CLI 계약 (이슈 #76).

- 공식 게이트(`research_load(purpose="coverage")`) 경유·성과 미계산
- tracked snapshot identity 대조 fail-closed
- blind OOS 날짜는 명시 플래그 없이 거부
- receipt sanitized(절대경로 없음)·결정론 필드
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from scripts.resampler_parity_check import canon, main
from src.history.store import HistoryStore, ImportStats
from src.models import ET, Candle

DAY = "2026-01-20"          # blind OOS(2026-01-21~) 직전 거래일
OOS_DAY = "2026-03-02"      # blind OOS 안쪽


def _seed(db, symbol="SPY", day=(2026, 1, 20), minutes=45):
    start = datetime(*day, 9, 30, tzinfo=ET)
    candles = [
        Candle.from_moment(symbol, "1m", start + timedelta(minutes=i),
                           100.0 + i, 102.0 + i, 99.0 + i, 101.0 + i, 10.0 * (i + 1),
                           "USD", True)
        for i in range(minutes)
    ]
    with HistoryStore(str(db)) as store:
        store.upsert_candles(candles, source="test", run_id="RUN-76",
                             stats=ImportStats(source="test"))
        store.commit()
    return candles


def test_cli_reports_parity_on_seeded_day(tmp_path):
    db = tmp_path / "snap.sqlite"
    _seed(db)
    out = tmp_path / "receipt.json"

    assert main(["--db", str(db), "--symbols", "SPY", "--days", DAY,
                 "--out", str(out), "--skip-identity-check"]) == 0

    receipt = json.loads(out.read_text(encoding="utf-8"))
    assert receipt["verdict"] == "PARITY_OK"
    assert receipt["performance_computed"] is False
    assert receipt["purpose"] == "coverage"
    assert receipt["summary"]["cells_mismatched"] == 0
    assert receipt["summary"]["cells_with_data"] == 1
    assert receipt["summary"]["bars_1m_total"] == 45
    assert receipt["summary"]["bars_5m_total"] == 8      # 09:30..10:14 → 닫힌 5m 8개
    assert receipt["summary"]["bars_15m_total"] == 2     # 닫힌 15m 2개
    assert receipt["blind_oos"]["days_inside_window"] == 0
    assert set(receipt["compared_paths"]) == {
        "research_core", "live_adapter", "candidate_stream", "replay"}


def test_receipt_is_sanitized_and_has_no_absolute_paths(tmp_path):
    db = tmp_path / "snap.sqlite"
    _seed(db)
    out = tmp_path / "receipt.json"
    main(["--db", str(db), "--symbols", "SPY", "--days", DAY,
          "--out", str(out), "--skip-identity-check"])

    text = out.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "/Users/" not in text and "/home/" not in text
    receipt = json.loads(text)
    assert receipt["snapshot_basename_checked"] == "snap.sqlite"


def test_identity_check_rejects_foreign_snapshot(tmp_path):
    """tracked snapshot basename 과 다르면 거부(엉뚱한 DB 로 parity 를 주장하지 못하게)."""
    db = tmp_path / "not-the-snapshot.sqlite"
    _seed(db)
    with pytest.raises(SystemExit):
        main(["--db", str(db), "--symbols", "SPY", "--days", DAY,
              "--out", str(tmp_path / "r.json")])


def test_blind_oos_day_requires_explicit_flag(tmp_path):
    """blind OOS 날짜는 coverage 목적이라도 기본 거부(개봉 실수 방지)."""
    db = tmp_path / "snap.sqlite"
    _seed(db, day=(2026, 3, 2))
    out = tmp_path / "r.json"
    with pytest.raises(SystemExit):
        main(["--db", str(db), "--symbols", "SPY", "--days", OOS_DAY,
              "--out", str(out), "--skip-identity-check"])
    assert not out.exists()

    assert main(["--db", str(db), "--symbols", "SPY", "--days", OOS_DAY,
                 "--out", str(out), "--skip-identity-check",
                 "--allow-oos-coverage"]) == 0
    receipt = json.loads(out.read_text(encoding="utf-8"))
    assert receipt["blind_oos"]["days_inside_window"] == 1
    assert receipt["performance_computed"] is False


def test_no_data_cell_is_reported_not_silently_passed(tmp_path):
    db = tmp_path / "snap.sqlite"
    _seed(db)
    out = tmp_path / "r.json"
    rc = main(["--db", str(db), "--symbols", "ZZZZ", "--days", DAY,
               "--out", str(out), "--skip-identity-check"])
    receipt = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 1 and receipt["verdict"] == "NO_DATA"
    assert receipt["summary"]["cells_no_data"] == 1


def test_canon_has_no_performance_fields():
    """직렬화 대상에 수익률·PnL 계열 필드가 없다(성과 열람 경계)."""
    start = datetime(2026, 1, 20, 9, 30, tzinfo=ET)
    c = [Candle.from_moment("SPY", "1m", start, 1.0, 2.0, 0.5, 1.5, 10.0, "USD", True)]
    row = canon(c)[0]
    assert len(row) == 13
    assert row[0] == "SPY" and row[1] == "1m" and row[4] == "REGULAR"
