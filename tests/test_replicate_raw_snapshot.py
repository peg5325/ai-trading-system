"""raw snapshot 외부 복제/restore smoke 안전 계약."""
from __future__ import annotations

import gzip
import json
import os

import pytest

from scripts.replicate_raw_snapshot import (
    RawReplicaError,
    main,
    replicate_raw_snapshot,
)
from src.history.raw_snapshot import RawSnapshotStore


def _raw(root, symbol="AAA"):
    store = RawSnapshotStore(str(root))
    store.emit(
        symbol=symbol, interval="1m", before=None,
        requested_at="2026-07-26T00:00:00Z",
        received_at="2026-07-26T00:00:01Z", source="test",
        payload={"candles": [{"symbol": symbol, "close": 1.0}]},
    )
    return store


def test_raw_replica_is_deterministic_and_restore_verified(tmp_path):
    root = tmp_path / "raw"
    target = tmp_path / "external"
    target.mkdir()
    store = _raw(root)

    first = replicate_raw_snapshot(
        source_root=str(root), target_dir=str(target),
        archive_name="first.tar", target_id="test", target_kind="external",
        allow_same_device=True,
    )
    second = replicate_raw_snapshot(
        source_root=str(root), target_dir=str(target),
        archive_name="second.tar", target_id="test", target_kind="external",
        allow_same_device=True,
    )
    assert first["status"] == "success"
    assert first["restore_content_match"] is True
    assert first["source_raw_content_sha256"] == store.manifest()["raw_content_sha256"]
    assert first["source_payload_files_verified"] == 1
    assert first["restored_payload_files_verified"] == 1
    assert first["archive_sha256"] == second["archive_sha256"]


def test_raw_replica_refuses_overwrite_and_nested_target(tmp_path):
    root = tmp_path / "raw"
    _raw(root)
    nested = root / "nested"
    nested.mkdir()
    with pytest.raises(RawReplicaError, match="밖"):
        replicate_raw_snapshot(
            source_root=str(root), target_dir=str(nested),
            archive_name="raw.tar", target_id="x", target_kind="x",
            allow_same_device=True,
        )

    target = tmp_path / "external"
    target.mkdir()
    (target / "raw.tar").write_bytes(b"keep")
    with pytest.raises(RawReplicaError, match="overwrite"):
        replicate_raw_snapshot(
            source_root=str(root), target_dir=str(target),
            archive_name="raw.tar", target_id="x", target_kind="x",
            allow_same_device=True,
        )
    assert (target / "raw.tar").read_bytes() == b"keep"


def test_raw_replica_cli_writes_sanitized_receipts(tmp_path):
    root = tmp_path / "raw"
    target = tmp_path / "external"
    target.mkdir()
    _raw(root)
    detail = tmp_path / "detail.json"
    tracked = tmp_path / "tracked.json"
    assert main([
        "--source-root", str(root),
        "--target-dir", str(target),
        "--archive-name", "raw.tar",
        "--target-id", "cloud",
        "--target-kind", "cloud_sync",
        "--allow-same-device",
        "--receipt", str(detail),
        "--sanitized-receipt", str(tracked),
    ]) == 0
    payload = json.loads(tracked.read_text(encoding="utf-8"))
    assert payload == json.loads(detail.read_text(encoding="utf-8"))
    assert str(tmp_path) not in tracked.read_text(encoding="utf-8")
    assert not os.path.exists(str(tracked) + ".partial")


def test_raw_replica_detects_payload_corruption_even_when_filename_is_unchanged(tmp_path):
    root = tmp_path / "raw"
    target = tmp_path / "external"
    target.mkdir()
    store = _raw(root)
    rel = store.list_content_files()[0]
    path = root / rel
    blob = bytearray(path.read_bytes())
    blob[-1] ^= 0x01
    path.write_bytes(blob)

    with pytest.raises((RawReplicaError, gzip.BadGzipFile, EOFError, OSError)):
        replicate_raw_snapshot(
            source_root=str(root), target_dir=str(target),
            archive_name="raw.tar", target_id="x", target_kind="x",
            allow_same_device=True,
        )
    assert not (target / "raw.tar").exists()
