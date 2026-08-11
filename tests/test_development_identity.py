from __future__ import annotations

import json

from src.research import development_identity


def test_live_daily_identity_is_bounded_to_development_end(tmp_path, monkeypatch):
    expected = {
        "snapshot_file": "file-sha",
        "snapshot_content": "content-sha",
        "intraday_manifest": "manifest-sha",
        "raw_content": "raw-sha",
        "daily_full_content": "daily-full-sha",
        "daily_full_rows": 100,
        "daily_development_content": "daily-development-sha",
        "daily_development_rows": 80,
    }
    snapshot = tmp_path / "snapshot.sqlite"
    snapshot.write_bytes(b"opaque-frozen-file")
    raw_root = tmp_path / "raw"
    raw_root.mkdir()
    snapshot_receipt = tmp_path / "snapshot-receipt.json"
    snapshot_receipt.write_text(
        json.dumps(
            {
                "snapshot": {
                    "file_sha256": expected["snapshot_file"],
                    "content_sha256": expected["snapshot_content"],
                }
            }
        ),
        encoding="utf-8",
    )
    intraday_manifest = tmp_path / "intraday-manifest.json"
    intraday_manifest.write_text(
        json.dumps({"data_content_sha256": expected["intraday_manifest"]}),
        encoding="utf-8",
    )
    daily_manifest = tmp_path / "daily-manifest.json"
    daily_manifest.write_text(
        json.dumps(
            {
                "data_content_sha256": expected["daily_full_content"],
                "summary": {"total_rows": expected["daily_full_rows"]},
            }
        ),
        encoding="utf-8",
    )
    seen_as_of: list[str] = []

    class RawReceipt:
        def __init__(self, _root):
            pass

        def manifest(self):
            return {"raw_content_sha256": expected["raw_content"]}

    def development_manifest(*_args, **kwargs):
        seen_as_of.append(kwargs["as_of"])
        return {
            "data_content_sha256": expected["daily_development_content"],
            "summary": {"total_rows": expected["daily_development_rows"]},
        }

    monkeypatch.setattr(development_identity, "file_sha256", lambda _path: "file-sha")
    monkeypatch.setattr(development_identity, "RawSnapshotStore", RawReceipt)
    checks = development_identity.verify_development_identity(
        snapshot_path=str(snapshot),
        raw_root=str(raw_root),
        daily_db_path="unused.sqlite",
        universe_yaml="unused.yaml",
        intraday_manifest_path=str(intraday_manifest),
        snapshot_receipt_path=str(snapshot_receipt),
        daily_manifest_path=str(daily_manifest),
        expected_snapshot_file=expected["snapshot_file"],
        expected_snapshot_content=expected["snapshot_content"],
        expected_intraday_manifest=expected["intraday_manifest"],
        expected_raw_content=expected["raw_content"],
        expected_daily_full_content=expected["daily_full_content"],
        expected_daily_full_rows=expected["daily_full_rows"],
        development_end="2026-01-20",
        expected_daily_development_content=expected["daily_development_content"],
        expected_daily_development_rows=expected["daily_development_rows"],
        daily_manifest_builder=development_manifest,
    )
    assert all(check["status"] == "match" for check in checks)
    assert seen_as_of == ["2026-01-20"]
