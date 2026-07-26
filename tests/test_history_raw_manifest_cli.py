"""history_raw_manifest CLI의 atomic·결정론 계약."""
from __future__ import annotations

import json

import pytest

from scripts.history_raw_manifest import main
from src.history.raw_snapshot import RawSnapshotStore


def test_raw_manifest_cli_matches_store_and_is_deterministic(tmp_path):
    root = tmp_path / "raw"
    store = RawSnapshotStore(str(root))
    store.emit(
        symbol="AAA", interval="1m", before=None,
        requested_at="2026-07-26T00:00:00Z",
        received_at="2026-07-26T00:00:01Z", source="test",
        payload={"candles": [{"symbol": "AAA", "close": 1.0}]},
    )
    output = tmp_path / "manifest.json"

    assert main(["--root", str(root), "--out", str(output)]) == 0
    first = output.read_bytes()
    assert json.loads(first) == store.manifest()

    output.unlink()
    assert main(["--root", str(root), "--out", str(output)]) == 0
    assert output.read_bytes() == first


def test_raw_manifest_cli_refuses_stale_partial(tmp_path):
    root = tmp_path / "raw"
    root.mkdir()
    output = tmp_path / "manifest.json"
    (tmp_path / "manifest.json.partial").write_text("stale", encoding="utf-8")

    with pytest.raises(SystemExit):
        main(["--root", str(root), "--out", str(output)])
    assert not output.exists()
