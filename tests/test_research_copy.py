"""P3 research data copy. tmp databases only; the "sealed" rows are fake sentinels, no real data is read."""
from __future__ import annotations

import os
import sqlite3
import stat
from datetime import date, datetime, timezone

import pytest
from test_daytrade_v11_precount import CAL, SEG, _row, make_db

from src.daytrade_v11 import data as V
from src.evalpath.research_copy import (
    CopyRefused,
    allowlist_sha256,
    assert_covered,
    build_copy,
    manifest_path,
    verify_copy,
)
from src.history.oos_guard import OOSLockError, research_load
from src.models import ET

SENTINEL = 999.0


def allowlist(**kw):
    al = {"schema": "research_allowlist/v1", "id": "t1", "interval": "1m", "symbols": ["QQQ", "TQQQ", "SQQQ"],
          "ranges": [{"start": "2023-04-11", "end_exclusive": "2023-08-01"}],
          "approved_by": "test", "approved_ref": "test"}
    al.update(kw)
    return al


@pytest.fixture
def source(tmp_path):
    db = make_db(tmp_path / "src.sqlite")
    con = sqlite3.connect(db)
    fake = [_row(sym, d, lab, SENTINEL) for sym in ("QQQ", "TQQQ", "SQQQ")
            for d in (date(2026, 2, 2), date(2025, 8, 1), date(2023, 3, 1)) for lab in (600, 601)]
    con.executemany(f"INSERT INTO candle_history VALUES ({','.join('?' * 16)})", fake)
    con.commit()
    con.close()
    return db


@pytest.mark.parametrize("rng", [{"start": "2026-01-20", "end_exclusive": "2026-01-22"},
                                 {"start": "2026-03-01", "end_exclusive": "2026-04-01"},
                                 {"start": "2026-07-21", "end_exclusive": "2026-07-23"}])
def test_allowlist_touching_the_intraday_seal_is_refused_before_the_source_is_opened(rng, tmp_path):
    missing = tmp_path / "does_not_exist.sqlite"
    with pytest.raises(CopyRefused, match="sealed"):
        build_copy(missing, allowlist(ranges=[rng]), tmp_path / "c.sqlite")
    assert not (tmp_path / "c.sqlite").exists()


def test_daily_allowlist_also_respects_the_swing_seal(tmp_path):
    with pytest.raises(CopyRefused, match="swing"):
        build_copy(tmp_path / "x.sqlite", allowlist(interval="1d", ranges=[{"start": "2025-08-01",
                                                                            "end_exclusive": "2025-09-01"}]),
                   tmp_path / "c.sqlite")


def test_copy_holds_only_allowlisted_rows_and_verifies(source, tmp_path):
    out = tmp_path / "c.sqlite"
    man = build_copy(source, allowlist(), out)
    con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    rows = con.execute("SELECT symbol, ts_exchange, open FROM candle_history").fetchall()
    con.close()
    assert rows and SENTINEL not in {r[2] for r in rows}
    assert min(r[1][:10] for r in rows) >= "2023-04-11" and max(r[1][:10] for r in rows) < "2023-08-01"
    assert sum(man["row_counts"].values()) == len(rows)
    assert not os.stat(out).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    assert verify_copy(out)["copy_sha256"] == man["copy_sha256"]
    assert man["allowlist_sha256"] == allowlist_sha256(allowlist())
    assert man["other_track_seal_overlaps"] == []
    with pytest.raises(CopyRefused, match="write-once"):
        build_copy(source, allowlist(), out)


def test_copy_tampering_or_a_plain_db_is_refused(source, tmp_path):
    out = tmp_path / "c.sqlite"
    man = build_copy(source, allowlist(), out)
    with pytest.raises(CopyRefused, match="expected"):
        verify_copy(out, expected_copy_sha256="0" * 64)
    os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)
    con = sqlite3.connect(out)
    con.execute("DELETE FROM candle_history WHERE symbol='SQQQ'")
    con.commit()
    con.close()
    with pytest.raises(CopyRefused, match="SHA-256"):
        verify_copy(out)
    with pytest.raises(CopyRefused, match="no manifest"):
        verify_copy(source)  # the source DB is not a research copy
    assert manifest_path(out).exists() and man["copy_file"] == "c.sqlite"


def test_requests_outside_the_allowlist_are_refused(source, tmp_path):
    man = build_copy(source, allowlist(), tmp_path / "c.sqlite")
    ok = (datetime(2023, 5, 1, tzinfo=ET), datetime(2023, 7, 1, tzinfo=ET))
    assert_covered(man, "QQQ", "1m", *ok)
    for sym, iv, lo, hi in [("SPY", "1m", *ok), ("QQQ", "1d", *ok), ("QQQ", "1m", None, ok[1]),
                            ("QQQ", "1m", datetime(2023, 3, 1, tzinfo=ET), ok[1])]:
        with pytest.raises(CopyRefused):
            assert_covered(man, sym, iv, lo, hi)


def test_one_minute_copy_records_but_allows_dates_in_the_daily_seal(source, tmp_path):
    al = allowlist(ranges=[{"start": "2025-07-28", "end_exclusive": "2025-08-05"}])
    man = build_copy(source, al, tmp_path / "c.sqlite")
    assert len(man["other_track_seal_overlaps"]) == 1
    assert man["other_track_seal_overlaps"][0]["track"] == "swing"


def test_research_steps_on_the_copy_match_the_source(source, tmp_path):
    out = tmp_path / "c.sqlite"
    build_copy(source, allowlist(), out)
    a = V.load_segment(source, SEG)
    b = V.load_segment(out, SEG)
    assert a.row_sha256 == b.row_sha256 and a.row_counts == b.row_counts
    assert V.build_mask([a], CAL, {"t": 1}) == V.build_mask([b], CAL, {"t": 1})
    # the copy holds nothing sealed, and the gate still refuses a sealed request on it
    with pytest.raises(OOSLockError):
        research_load(str(out), "QQQ", "1m", start=datetime(2026, 2, 1, tzinfo=timezone.utc),
                      end=datetime(2026, 2, 3, tzinfo=timezone.utc))
