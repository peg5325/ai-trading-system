"""ORB clean-session immutable research snapshot 계약 테스트 (#84)."""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from scripts.history_research_snapshot import _write_receipt_atomic
from src.history.ingest_provenance import BACKFILL_SOURCE
from src.history.research_snapshot import (
    ResearchSnapshotError,
    content_sha256,
    create_clean_snapshot,
)
from src.history.store import HistoryStore, ImportStats


SYMBOLS = ["AAA", "BBB"]
AS_OF = "2026-07-21"
RUN_ID = "RUN-84"
ALLOWED = ["PRE", "REGULAR", "POST"]


def _insert_candle(con, *, symbol: str, day: str, session: str,
                   minute: str, source: str = BACKFILL_SOURCE):
    ts = f"{day}T{minute}:00+00:00"
    con.execute(
        "INSERT INTO candle_history "
        "(symbol,market,interval,ts_utc,ts_exchange,session,open,high,low,close,volume,"
        "currency,adjusted,source,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (symbol, "US", "1m", ts, ts, session, 100.0, 101.0, 99.0, 100.5, 1000.0,
         "USD", 1, source, "2026-07-22T00:00:00Z", "2026-07-22T00:00:00Z"),
    )


def _source_db(tmp_path) -> str:
    path = str(tmp_path / "source.sqlite")
    with HistoryStore(path) as store:
        for symbol in [*SYMBOLS, "EXTRA"]:
            _insert_candle(
                store._con, symbol=symbol, day="2023-07-21",
                session="PRE", minute="08:00")
            _insert_candle(
                store._con, symbol=symbol, day=AS_OF,
                session="REGULAR", minute="14:30")
            _insert_candle(
                store._con, symbol=symbol, day=AS_OF,
                session="POST", minute="20:00")
            _insert_candle(
                store._con, symbol=symbol, day=AS_OF,
                session="OVERNIGHT", minute="23:00")
            _insert_candle(
                store._con, symbol=symbol, day="2026-07-22",
                session="PRE", minute="08:00")
        for symbol in SYMBOLS:
            stats = ImportStats(source=BACKFILL_SOURCE)
            store.write_ingest_log(
                RUN_ID, source=BACKFILL_SOURCE, stats=stats,
                rows_seen=5, earliest="2023-07-21T08:00:00+00:00",
                latest="2026-07-22T08:00:00+00:00",
                note=("reason=max_pages; sessions_allowed=POST,PRE,REGULAR; "
                      "completed_through=2026-07-21; cursor_persisted=false"),
                symbol=symbol, interval="1m",
            )
            store.save_cursor(
                symbol, "1m", oldest_ts_utc="2023-07-21T08:00:00+00:00",
                next_before=None, exhausted=True,
            )
        store.commit()
        store._con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path


def _create(source: str, output: str):
    return create_clean_snapshot(
        source_db=source, output_db=output, symbols=SYMBOLS,
        as_of=AS_OF, allowed_sessions=ALLOWED,
        collection_run_id=RUN_ID, generated_at_utc="2026-07-26T08:00:00Z",
        source_commit="abc123", raw_content_sha256="a" * 64,
    )


def test_clean_snapshot_filters_symbol_session_and_as_of(tmp_path):
    source = _source_db(tmp_path)
    output = str(tmp_path / "clean.sqlite")
    result = _create(source, output)

    assert result.receipt["status"] == "COMPLETE"
    assert result.receipt["source"]["unchanged"] is True
    assert result.receipt["snapshot"]["row_count"] == 6
    assert result.receipt["snapshot"]["distinct_symbols"] == 2
    assert result.receipt["snapshot"]["sessions"] == {
        "POST": 2, "PRE": 2, "REGULAR": 2,
    }
    assert result.receipt["snapshot"]["disallowed_sessions"] == []
    assert result.receipt["snapshot"]["bars_beyond_as_of"] == 0
    assert (
        result.receipt["provenance_verification"]["provenance_status"]
        == "VERIFIED"
    )
    assert not os.path.exists(output + ".partial")

    con = sqlite3.connect(f"file:{output}?mode=ro", uri=True)
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM candle_history WHERE symbol='EXTRA'"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM candle_history WHERE session='OVERNIGHT'"
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM candle_history "
            "WHERE substr(ts_exchange,1,10)>'2026-07-21'"
        ).fetchone()[0] == 0
        metadata = dict(con.execute(
            "SELECT key,value FROM research_snapshot_metadata"
        ).fetchall())
        assert metadata["source_basename"] == "source.sqlite"
        assert str(tmp_path) not in "\n".join(metadata.values())
        assert content_sha256(con) == result.receipt["snapshot"]["content_sha256"]
    finally:
        con.close()


def test_snapshot_refuses_overwrite_same_source_and_stale_partial(tmp_path):
    source = _source_db(tmp_path)
    output = str(tmp_path / "clean.sqlite")
    _create(source, output)
    with pytest.raises(ResearchSnapshotError, match="overwrite"):
        _create(source, output)
    with pytest.raises(ResearchSnapshotError, match="같은 파일"):
        _create(source, source)

    other = str(tmp_path / "other.sqlite")
    open(other + ".partial", "wb").close()
    with pytest.raises(ResearchSnapshotError, match="stale partial"):
        _create(source, other)


@pytest.mark.parametrize("raw_hash", [None, "", "raw-hash", "A" * 64])
def test_snapshot_requires_canonical_raw_sha256(tmp_path, raw_hash):
    source = _source_db(tmp_path)
    output = str(tmp_path / "clean.sqlite")
    with pytest.raises(ResearchSnapshotError, match="raw_content_sha256"):
        create_clean_snapshot(
            source_db=source, output_db=output, symbols=SYMBOLS,
            as_of=AS_OF, allowed_sessions=ALLOWED,
            collection_run_id=RUN_ID,
            generated_at_utc="2026-07-26T08:00:00Z",
            source_commit="abc123", raw_content_sha256=raw_hash,
        )
    assert not os.path.exists(output)
    assert not os.path.exists(output + ".partial")


def test_snapshot_rebuild_is_byte_deterministic(tmp_path):
    source = _source_db(tmp_path)
    first = _create(source, str(tmp_path / "first.sqlite"))
    second = _create(source, str(tmp_path / "second.sqlite"))
    assert first.receipt["snapshot"]["content_sha256"] == (
        second.receipt["snapshot"]["content_sha256"])
    assert first.receipt["snapshot"]["file_sha256"] == (
        second.receipt["snapshot"]["file_sha256"])


def test_missing_collection_provenance_fails_closed(tmp_path):
    source = _source_db(tmp_path)
    con = sqlite3.connect(source)
    con.execute("DELETE FROM ingest_log WHERE symbol='BBB'")
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()

    output = str(tmp_path / "clean.sqlite")
    with pytest.raises(Exception, match="MISSING_SYMBOLS"):
        _create(source, output)
    assert not os.path.exists(output)
    assert not os.path.exists(output + ".partial")


def test_same_run_extra_symbol_conflicting_cutoff_fails_before_filter(tmp_path):
    source = _source_db(tmp_path)
    with HistoryStore(source) as store:
        stats = ImportStats(source=BACKFILL_SOURCE)
        store.write_ingest_log(
            RUN_ID, source=BACKFILL_SOURCE, stats=stats,
            rows_seen=1, earliest="2026-07-20T14:30:00+00:00",
            latest="2026-07-20T14:30:00+00:00",
            note=("reason=max_pages; sessions_allowed=POST,PRE,REGULAR; "
                  "completed_through=2026-07-20; cursor_persisted=false"),
            symbol="EXTRA", interval="1m",
        )
        store.commit()
        store._con.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    output = str(tmp_path / "clean.sqlite")
    with pytest.raises(ResearchSnapshotError, match="CONFLICTING_CUTOFF"):
        _create(source, output)
    assert not os.path.exists(output)
    assert not os.path.exists(output + ".partial")


def test_snapshot_copies_legacy_ingest_log_column_order_by_name(tmp_path):
    """ALTER migration으로 column order가 다른 운영 DB도 provenance를 정확히 복사한다."""
    source = _source_db(tmp_path)
    con = sqlite3.connect(source)
    con.executescript(
        """
        CREATE TABLE ingest_log_legacy (
            run_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            source TEXT NOT NULL,
            rows_seen INTEGER NOT NULL,
            rows_inserted INTEGER NOT NULL,
            rows_updated INTEGER NOT NULL,
            rows_duplicate INTEGER NOT NULL,
            rows_conflict INTEGER NOT NULL,
            earliest TEXT,
            latest TEXT,
            note TEXT,
            symbol TEXT,
            interval TEXT
        );
        INSERT INTO ingest_log_legacy
            (run_id,ts,source,rows_seen,rows_inserted,rows_updated,
             rows_duplicate,rows_conflict,earliest,latest,note,symbol,interval)
        SELECT run_id,ts,source,rows_seen,rows_inserted,rows_updated,
               rows_duplicate,rows_conflict,earliest,latest,note,symbol,interval
        FROM ingest_log;
        DROP TABLE ingest_log;
        ALTER TABLE ingest_log_legacy RENAME TO ingest_log;
        """
    )
    con.commit()
    con.close()

    result = _create(source, str(tmp_path / "clean.sqlite"))
    assert (
        result.receipt["provenance_verification"]["provenance_status"]
        == "VERIFIED"
    )


def test_source_change_during_snapshot_discards_partial(tmp_path, monkeypatch):
    source = _source_db(tmp_path)
    output = str(tmp_path / "clean.sqlite")
    from src.history import research_snapshot as module

    real_hash = module.file_sha256
    source_calls = 0

    def changing_hash(path: str) -> str:
        nonlocal source_calls
        if os.path.realpath(path) == os.path.realpath(source):
            source_calls += 1
            if source_calls == 2:
                return "changed"
        return real_hash(path)

    monkeypatch.setattr(module, "file_sha256", changing_hash)
    with pytest.raises(ResearchSnapshotError, match="source DB 변경"):
        _create(source, output)
    assert not os.path.exists(output)
    assert not os.path.exists(output + ".partial")


def test_receipt_write_is_atomic_and_refuses_existing_evidence(tmp_path):
    receipt = tmp_path / "receipt.json"
    payload = {"status": "COMPLETE", "value": 84}
    _write_receipt_atomic(str(receipt), payload)
    assert json.loads(receipt.read_text(encoding="utf-8")) == payload
    assert not (tmp_path / "receipt.json.partial").exists()

    with pytest.raises(FileExistsError, match="overwrite"):
        _write_receipt_atomic(str(receipt), {"status": "REPLACED"})
    assert json.loads(receipt.read_text(encoding="utf-8")) == payload


def test_receipt_write_refuses_stale_partial(tmp_path):
    receipt = tmp_path / "receipt.json"
    partial = tmp_path / "receipt.json.partial"
    partial.write_text("stale", encoding="utf-8")
    with pytest.raises(FileExistsError, match="stale partial"):
        _write_receipt_atomic(str(receipt), {"status": "COMPLETE"})
    assert partial.read_text(encoding="utf-8") == "stale"
    assert not receipt.exists()
