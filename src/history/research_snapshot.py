"""ORB 연구용 immutable clean-session SQLite snapshot 생성.

Canonical history DB는 보존하고, 고정된 P0 심볼·1m·완료 거래일·허용 세션만 별도 DB로
복사한다. 결과 파일은 atomic rename으로 승격되며 content/file SHA와 collection provenance를
검증한다. 전략 성과·forward return·blind OOS는 계산하지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.history.ingest_provenance import BACKFILL_SOURCE, verify_collection_provenance
from src.history.store import SCHEMA

SNAPSHOT_SCHEMA = "orb-clean-session-snapshot/receipt/v1"
INTERVAL = "1m"
_CANDLE_CONTENT_FIELDS = (
    "symbol", "market", "interval", "ts_utc", "ts_exchange", "session",
    "open", "high", "low", "close", "volume", "currency", "adjusted", "source",
)
_CANDLE_STORAGE_FIELDS = (*_CANDLE_CONTENT_FIELDS, "created_at", "updated_at")
_INGEST_LOG_FIELDS = (
    "run_id", "ts", "source", "symbol", "interval", "rows_seen",
    "rows_inserted", "rows_updated", "rows_duplicate", "rows_conflict",
    "earliest", "latest", "note",
)
_IMPORT_CONFLICT_FIELDS = (
    "ts", "run_id", "symbol", "interval", "ts_utc", "field",
    "existing_val", "incoming_val", "existing_source", "incoming_source",
    "resolution",
)
_BACKFILL_CURSOR_FIELDS = (
    "symbol", "interval", "oldest_ts_utc", "next_before", "exhausted",
    "updated_at",
)


class ResearchSnapshotError(RuntimeError):
    """Snapshot 계약 또는 검증 실패."""


@dataclass(frozen=True)
class SnapshotResult:
    receipt: dict[str, Any]
    output_path: str


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def content_sha256(con: sqlite3.Connection) -> str:
    """연구 입력 필드 전체를 PK 순서로 직렬화한 결정론 SHA-256."""
    h = hashlib.sha256()
    fields = ",".join(_CANDLE_CONTENT_FIELDS)
    for row in con.execute(
        f"SELECT {fields} FROM candle_history ORDER BY symbol, interval, ts_utc"
    ):
        blob = json.dumps(
            list(row), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        h.update(blob.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def snapshot_inventory(con: sqlite3.Connection, *, as_of: str,
                       allowed_sessions: list[str]) -> dict[str, Any]:
    sessions = {
        str(row[0]): int(row[1])
        for row in con.execute(
            "SELECT session,COUNT(*) FROM candle_history GROUP BY session ORDER BY session"
        )
    }
    row = con.execute(
        "SELECT COUNT(*),COUNT(DISTINCT symbol),MIN(substr(ts_exchange,1,10)),"
        "MAX(substr(ts_exchange,1,10)) FROM candle_history"
    ).fetchone()
    beyond = con.execute(
        "SELECT COUNT(*) FROM candle_history WHERE substr(ts_exchange,1,10)>?",
        (as_of,),
    ).fetchone()[0]
    disallowed = sorted(set(sessions) - set(allowed_sessions))
    return {
        "row_count": int(row[0]),
        "distinct_symbols": int(row[1]),
        "earliest_trading_day": row[2],
        "latest_trading_day": row[3],
        "sessions": sessions,
        "bars_beyond_as_of": int(beyond),
        "disallowed_sessions": disallowed,
    }


def _assert_safe_paths(source_db: str, output_db: str) -> tuple[str, str, str]:
    source = os.path.realpath(source_db)
    output = os.path.realpath(output_db)
    partial = output + ".partial"
    if source == output:
        raise ResearchSnapshotError("source와 output은 같은 파일일 수 없다")
    if not os.path.isfile(source):
        raise ResearchSnapshotError(f"source DB가 없음: {os.path.basename(source)}")
    if os.path.exists(output):
        raise ResearchSnapshotError(f"output overwrite 거부: {os.path.basename(output)}")
    if os.path.exists(partial):
        raise ResearchSnapshotError(
            f"stale partial 존재, 자동 삭제하지 않음: {os.path.basename(partial)}")
    wal = source + "-wal"
    if os.path.exists(wal) and os.path.getsize(wal) > 0:
        raise ResearchSnapshotError("source에 미체크포인트 WAL이 있어 immutable read 거부")
    return source, output, partial


def _placeholders(values: list[str]) -> str:
    return ",".join("?" for _ in values)


def _copy_filtered(
    con: sqlite3.Connection, *, source_uri: str, symbols: list[str], as_of: str,
    allowed_sessions: list[str], collection_run_id: str, metadata: dict[str, str],
) -> None:
    con.execute("ATTACH DATABASE ? AS source", (source_uri,))
    con.executescript(SCHEMA)
    con.execute(
        "CREATE TABLE research_snapshot_metadata "
        "(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID"
    )
    symbol_marks = _placeholders(symbols)
    session_marks = _placeholders(allowed_sessions)
    candle_fields = ",".join(_CANDLE_STORAGE_FIELDS)
    con.execute(
        f"INSERT INTO candle_history({candle_fields}) "
        f"SELECT {candle_fields} FROM source.candle_history "
        f"WHERE symbol IN ({symbol_marks}) AND interval=? "
        "AND substr(ts_exchange,1,10)<=? "
        f"AND session IN ({session_marks}) "
        "ORDER BY symbol,interval,ts_utc",
        [*symbols, INTERVAL, as_of, *allowed_sessions],
    )
    ingest_fields = ",".join(_INGEST_LOG_FIELDS)
    con.execute(
        f"INSERT INTO ingest_log({ingest_fields}) "
        f"SELECT {ingest_fields} FROM source.ingest_log "
        f"WHERE run_id=? AND source=? AND interval=? AND symbol IN ({symbol_marks}) "
        "ORDER BY symbol,ts,rowid",
        [collection_run_id, BACKFILL_SOURCE, INTERVAL, *symbols],
    )
    conflict_fields = ",".join(_IMPORT_CONFLICT_FIELDS)
    con.execute(
        f"INSERT INTO import_conflict({conflict_fields}) "
        f"SELECT {conflict_fields} FROM source.import_conflict "
        f"WHERE interval=? AND symbol IN ({symbol_marks}) "
        "ORDER BY symbol,ts_utc,ts,rowid",
        [INTERVAL, *symbols],
    )
    cursor_fields = ",".join(_BACKFILL_CURSOR_FIELDS)
    con.execute(
        f"INSERT INTO backfill_cursor({cursor_fields}) "
        f"SELECT {cursor_fields} FROM source.backfill_cursor "
        f"WHERE interval=? AND symbol IN ({symbol_marks}) ORDER BY symbol",
        [INTERVAL, *symbols],
    )
    con.executemany(
        "INSERT INTO research_snapshot_metadata(key,value) VALUES (?,?)",
        sorted(metadata.items()),
    )
    con.commit()


def create_clean_snapshot(
    *, source_db: str, output_db: str, symbols: list[str], as_of: str,
    allowed_sessions: list[str], collection_run_id: str,
    generated_at_utc: str, source_commit: str | None,
    raw_content_sha256: str | None,
) -> SnapshotResult:
    """Canonical DB에서 ORB 연구 입력 derivative를 생성하고 fail-closed 검증한다."""
    if not symbols or len(symbols) != len(set(symbols)):
        raise ResearchSnapshotError("symbols는 비어 있지 않은 중복 없는 목록이어야 한다")
    allowed = sorted(set(allowed_sessions))
    if not allowed or not set(allowed) <= {"PRE", "REGULAR", "POST"}:
        raise ResearchSnapshotError("allowed_sessions는 PRE/REGULAR/POST의 비공백 부분집합이어야 한다")
    if not collection_run_id.strip():
        raise ResearchSnapshotError("collection_run_id는 비공백이어야 한다")

    source, output, partial = _assert_safe_paths(source_db, output_db)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    source_sha_before = file_sha256(source)
    source_uri = Path(source).as_uri() + "?mode=ro&immutable=1"
    metadata = {
        "schema": SNAPSHOT_SCHEMA,
        "as_of": as_of,
        "interval": INTERVAL,
        "allowed_sessions": ",".join(allowed),
        "collection_run_id": collection_run_id,
        "generated_at_utc": generated_at_utc,
        "source_basename": os.path.basename(source),
        "source_commit": source_commit or "",
        "source_file_sha256": source_sha_before,
        "raw_content_sha256": raw_content_sha256 or "",
    }
    stage = "initialize"

    try:
        stage = "copy_filtered"
        con = sqlite3.connect(partial, uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=DELETE")
            con.execute("PRAGMA synchronous=FULL")
            _copy_filtered(
                con, source_uri=source_uri, symbols=symbols, as_of=as_of,
                allowed_sessions=allowed, collection_run_id=collection_run_id,
                metadata=metadata,
            )
            stage = "inventory"
            inventory = snapshot_inventory(
                con, as_of=as_of, allowed_sessions=allowed)
            if inventory["distinct_symbols"] != len(symbols):
                raise ResearchSnapshotError(
                    f"symbol 수 불일치: {inventory['distinct_symbols']} != {len(symbols)}")
            if inventory["row_count"] <= 0:
                raise ResearchSnapshotError("snapshot row_count=0")
            if inventory["bars_beyond_as_of"] != 0:
                raise ResearchSnapshotError("as_of 초과 봉 존재")
            if inventory["disallowed_sessions"]:
                raise ResearchSnapshotError(
                    f"비허용 세션 존재: {inventory['disallowed_sessions']}")

            stage = "provenance"
            provenance = verify_collection_provenance(
                con, run_id=collection_run_id, interval=INTERVAL,
                source=BACKFILL_SOURCE, symbols=symbols,
                declared_completed_through=as_of, as_of=as_of,
            )
            if provenance["provenance_status"] != "VERIFIED":
                raise ResearchSnapshotError(
                    "collection provenance 미검증: "
                    f"{provenance['provenance_status']}")

            stage = "sqlite_integrity"
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            quick = con.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok" or quick != "ok":
                raise ResearchSnapshotError(
                    f"SQLite 검증 실패: integrity={integrity}, quick={quick}")

            stage = "content_sha256"
            content_hash = content_sha256(con)
        finally:
            con.close()

        stage = "source_sha256_after"
        source_sha_after = file_sha256(source)
        if source_sha_before != source_sha_after:
            raise ResearchSnapshotError("snapshot 생성 중 source DB 변경 감지")

        stage = "atomic_promote"
        with open(partial, "rb+") as fh:
            os.fsync(fh.fileno())
        os.replace(partial, output)
        dir_fd = os.open(os.path.dirname(output), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        file_hash = file_sha256(output)
        receipt = {
            "schema": SNAPSHOT_SCHEMA,
            "status": "COMPLETE",
            "generated_at_utc": generated_at_utc,
            "source": {
                "basename": os.path.basename(source),
                "commit": source_commit,
                "file_sha256_before": source_sha_before,
                "file_sha256_after": source_sha_after,
                "unchanged": source_sha_before == source_sha_after,
            },
            "contract": {
                "interval": INTERVAL,
                "as_of": as_of,
                "allowed_sessions": allowed,
                "symbols": sorted(symbols),
                "collection_run_id": collection_run_id,
            },
            "snapshot": {
                "basename": os.path.basename(output),
                "size_bytes": os.path.getsize(output),
                "file_sha256": file_hash,
                "content_sha256": content_hash,
                "integrity_check": integrity,
                "quick_check": quick,
                **inventory,
            },
            "provenance_verification": provenance,
            "raw_content_sha256": raw_content_sha256,
        }
        return SnapshotResult(receipt=receipt, output_path=output)
    except Exception as exc:
        if os.path.exists(partial):
            os.remove(partial)
        if isinstance(exc, ResearchSnapshotError):
            raise ResearchSnapshotError(f"{stage} 단계 실패: {exc}") from exc
        raise ResearchSnapshotError(f"{stage} 단계 실패: {exc!r}") from exc
