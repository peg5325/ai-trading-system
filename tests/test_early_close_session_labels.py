"""조기폐장일 세션 라벨(사용자 결정 2026-10-15) + 재라벨 스크립트 테스트."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import relabel_early_close_sessions as relabel
from src.history.store import HistoryStore
from src.models import (
    ET,
    KST,
    MARKET_KR,
    MARKET_US,
    SESSION_POST,
    SESSION_REGULAR,
    Candle,
    classify_session,
    in_eod_flatten_window,
)


def _et(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=ET)


# ---- classify_session ----

@pytest.mark.parametrize("iso,expected", [
    ("2022-11-25T09:30", SESSION_REGULAR),
    ("2022-11-25T12:59", SESSION_REGULAR),
    ("2022-11-25T13:00", SESSION_POST),
    ("2022-11-25T15:59", SESSION_POST),
    ("2022-11-25T16:00", SESSION_POST),
    ("2022-11-25T19:59", SESSION_POST),
    ("2024-12-24T13:00", SESSION_POST),
    ("2022-11-28T12:59", SESSION_REGULAR),   # 정상일
    ("2022-11-28T13:00", SESSION_REGULAR),
    ("2022-11-28T15:59", SESSION_REGULAR),
    ("2022-11-28T16:00", SESSION_POST),
])
def test_classify_session_early_close_vs_normal_day(iso, expected):
    assert classify_session(_et(iso), MARKET_US) == expected


def test_classify_session_outside_calendar_range_keeps_fixed_windows():
    """캘린더 구간 밖(2027-11-26 = 2027 Black Friday 예정)은 예외 없이 고정 창(REGULAR ~16:00)."""
    assert classify_session(_et("2027-11-26T14:00"), MARKET_US) == SESSION_REGULAR
    assert classify_session(_et("2027-01-01T10:00"), MARKET_US) == SESSION_REGULAR
    assert classify_session(_et("2020-11-27T14:00"), MARKET_US) == SESSION_REGULAR
    c = Candle.from_moment("QQQ", "1m", _et("2027-01-01T13:30"), 1, 1, 1, 1, 1, "USD", True)
    assert c.session == SESSION_REGULAR


def test_classify_session_kr_unchanged_on_us_early_close_date():
    ts = datetime(2022, 11, 25, 14, 0, tzinfo=KST)
    assert classify_session(ts, MARKET_KR) == SESSION_REGULAR


def test_candle_from_moment_labels_early_close_post():
    c = Candle.from_moment("QQQ", "1m", _et("2022-11-25T13:01"), 1, 1, 1, 1, 1, "USD", True)
    assert c.session == SESSION_POST


def test_eod_window_uses_1300_close_on_early_close_day():
    def c(iso):
        return Candle.from_moment("QQQ", "1m", _et(iso), 1, 1, 1, 1, 1, "USD", True)
    assert in_eod_flatten_window(c("2022-11-25T12:50"), 10) is True
    assert in_eod_flatten_window(c("2022-11-25T12:40"), 10) is False
    assert in_eod_flatten_window(c("2022-11-28T12:50"), 10) is False   # 정상일은 16:00 기준
    assert in_eod_flatten_window(c("2022-11-28T15:50"), 10) is True


# ---- relabel script ----

def _row(sym: str, iso_et: str, session: str) -> dict:
    ts = _et(iso_et)
    return {"symbol": sym, "market": "US", "interval": "1m",
            "ts_utc": ts.astimezone(timezone.utc).isoformat(),
            "ts_exchange": ts.isoformat(), "session": session,
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
            "currency": "USD", "adjusted": 1, "source": "test",
            "created_at": "2020-01-01T00:00:00+00:00", "updated_at": "2020-01-01T00:00:00+00:00"}


def _make_db(tmp_path):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "market_history.sqlite"
    HistoryStore(str(db)).close()          # canonical schema + indexes
    rows = [
        _row("QQQ", "2022-11-25T12:59", "REGULAR"),
        _row("QQQ", "2022-11-25T13:00", "REGULAR"),    # target
        _row("QQQ", "2022-11-25T15:59", "REGULAR"),    # target
        _row("QQQ", "2022-11-25T16:00", "POST"),
        _row("SPY", "2024-12-24T13:30", "REGULAR"),    # target
        _row("QQQ", "2022-11-28T13:30", "REGULAR"),    # 정상일 — 유지
    ]
    kr = _row("005930", "2022-11-25T13:30", "REGULAR")
    kr["market"] = "KR"                                 # US 아님 — 유지
    rows.append(kr)
    d1 = _row("QQQ", "2022-11-25T13:00", "REGULAR")
    d1["interval"] = "1d"                               # 1m 아님 — 유지
    rows.append(d1)
    con = sqlite3.connect(db)
    cols = list(rows[0])
    con.executemany(f"INSERT INTO candle_history ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [tuple(r[c] for c in cols) for r in rows])
    con.commit()
    con.close()
    return db


def _dump(db):
    con = sqlite3.connect(db)
    try:
        return sorted(con.execute(
            "SELECT symbol, market, interval, ts_exchange, session, updated_at FROM candle_history"))
    finally:
        con.close()


def test_relabel_dry_run_changes_nothing(tmp_path, capsys):
    db = _make_db(tmp_path)
    before = _dump(db)
    assert relabel.main(["--db", str(db), "--root", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry_run"
    assert out["target_rows"] == 3
    assert out["before"]["2022-11-25"] == {"QQQ": 2}
    assert out["before"]["2024-12-24"] == {"SPY": 1}
    assert _dump(db) == before
    assert not (tmp_path / "reports").exists()


def test_relabel_apply_refuses_without_backup(tmp_path):
    db = _make_db(tmp_path)
    before = _dump(db)
    with pytest.raises(SystemExit):
        relabel.main(["--db", str(db), "--root", str(tmp_path), "--apply"])
    with pytest.raises(SystemExit):
        relabel.main(["--db", str(db), "--root", str(tmp_path), "--apply",
                      "--backup-verified", str(tmp_path / "missing.sqlite")])
    assert _dump(db) == before


def test_relabel_apply_targets_only_and_is_idempotent(tmp_path, capsys):
    db = _make_db(tmp_path)
    backup = tmp_path / "backup.sqlite"
    backup.write_bytes(db.read_bytes())
    before = {r[:4]: r for r in _dump(db)}
    args = ["--db", str(db), "--root", str(tmp_path), "--apply", "--backup-verified", str(backup)]
    assert relabel.main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["updated_rows"] == 3 and out["remaining_regular_at_or_after_1300"] == 0
    receipt = json.loads(Path(out["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["updated_rows"] == 3 and receipt["db_path"] == str(db)
    assert "reports/data-ops" in out["receipt_path"].replace("\\", "/")

    after = {r[:4]: r for r in _dump(db)}
    changed = {k for k in after if after[k] != before[k]}
    assert changed == {
        ("QQQ", "US", "1m", "2022-11-25T13:00:00-05:00"),
        ("QQQ", "US", "1m", "2022-11-25T15:59:00-05:00"),
        ("SPY", "US", "1m", "2024-12-24T13:30:00-05:00"),
    }
    for k in changed:
        assert after[k][4] == "POST" and after[k][5] != before[k][5]

    capsys.readouterr()
    assert relabel.main(args) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["target_rows"] == 0 and again["updated_rows"] == 0
    assert {r[:4]: r for r in _dump(db)} == after


def _backup_of(db, tmp_path):
    backup = tmp_path / "backup.sqlite"
    backup.write_bytes(db.read_bytes())
    return backup


def test_relabel_receipt_records_backup_check(tmp_path, capsys):
    db = _make_db(tmp_path)
    backup = _backup_of(db, tmp_path)
    assert relabel.main(["--db", str(db), "--root", str(tmp_path), "--apply",
                         "--backup-verified", str(backup)]) == 0
    out = json.loads(capsys.readouterr().out)
    check = out["backup_check"]
    assert check["quick_check"] == "ok" and check["size_bytes"] == backup.stat().st_size
    assert check["mtime_utc"]
    iv = out["independent_verification"]
    assert iv["remaining_regular_at_or_after_1300"] == 0
    assert iv["updated_rows_seen"] == 3 and iv["classify_sample_checked"] == 3
    assert iv["classify_sample_mismatch"] == []


@pytest.mark.parametrize("content", [b"not a sqlite database" * 100, b""])
def test_relabel_refuses_corrupt_or_non_history_backup(tmp_path, content):
    db = _make_db(tmp_path)
    before = _dump(db)
    bad = tmp_path / "bad_backup.sqlite"
    bad.write_bytes(content)      # garbage bytes, or an empty file (valid SQLite, but no candle_history)
    with pytest.raises(SystemExit):
        relabel.main(["--db", str(db), "--root", str(tmp_path), "--apply", "--backup-verified", str(bad)])
    assert _dump(db) == before


def test_relabel_refuses_db_outside_root_data(tmp_path, capsys):
    db = _make_db(tmp_path)
    other_root = tmp_path / "other_root"
    other_root.mkdir()
    with pytest.raises(SystemExit):
        relabel.main(["--db", str(db), "--root", str(other_root)])
    assert relabel.main(["--db", str(db), "--root", str(other_root), "--allow-db-outside-root"]) == 0
    assert json.loads(capsys.readouterr().out)["target_rows"] == 3


def test_relabel_prints_receipt_even_when_receipt_write_fails(tmp_path, capsys, monkeypatch):
    db = _make_db(tmp_path)
    backup = _backup_of(db, tmp_path)

    def fail(path, receipt):
        raise OSError("disk full")
    monkeypatch.setattr(relabel, "write_receipt", fail)
    rc = relabel.main(["--db", str(db), "--root", str(tmp_path), "--apply", "--backup-verified", str(backup)])
    captured = capsys.readouterr()
    assert rc != 0
    out = json.loads(captured.out)
    assert out["updated_rows"] == 3 and out["receipt_path"]
    assert "disk full" in captured.err
    assert sum(1 for r in _dump(db) if r[4] == "POST") == 4    # committed: 3 relabeled + existing 16:00 POST


def test_relabel_rolls_back_when_independent_verification_fails(tmp_path, monkeypatch):
    db = _make_db(tmp_path)
    backup = _backup_of(db, tmp_path)
    before = _dump(db)
    monkeypatch.setattr(relabel, "independent_verify", lambda *a: {
        "remaining_regular_at_or_after_1300": 1, "updated_rows_seen": 3,
        "classify_sample_checked": 0, "classify_sample_mismatch": []})
    with pytest.raises(RuntimeError, match="verification failed"):
        relabel.main(["--db", str(db), "--root", str(tmp_path), "--apply", "--backup-verified", str(backup)])
    assert _dump(db) == before


def test_out_of_calendar_fallback_warns_once(monkeypatch, caplog):
    from src import models
    monkeypatch.setattr(models, "_fallback_warned", False)
    with caplog.at_level("WARNING", logger="src.models"):
        assert classify_session(_et("2027-11-26T14:00"), MARKET_US) == SESSION_REGULAR
        assert classify_session(_et("2027-12-01T14:00"), MARKET_US) == SESSION_REGULAR
        assert classify_session(_et("2022-11-25T14:00"), MARKET_US) == SESSION_POST
    warnings = [r for r in caplog.records if r.name == "src.models" and r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "2027-11-26" in warnings[0].getMessage() and "2026-12-31" in warnings[0].getMessage()
