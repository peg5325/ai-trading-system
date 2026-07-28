from src.research.orb.manifest import (
    PREREG_MERGE_COMMIT,
    build_result,
    payload_sha256,
)


def test_payload_hash_is_deterministic_and_generated_time_is_outside():
    payload = {
        "prereg": {"prereg_merge_commit": PREREG_MERGE_COMMIT},
        "producer_commit": "abc",
        "snapshot": {},
        "config_version": "v1",
        "schema_version": "orb-development-result/v1",
        "window": {},
        "single_pass": {"oos_opened": False},
        "specs": [{"name": "primary"}],
        "stats": {},
        "excluded": [],
        "skipped": [],
        "defect_ratios": {},
        "dev_gate": {},
        "verdict": {},
        "loader": {
            "function": "src.history.oos_guard.research_load",
            "purpose": "performance",
            "track": "intraday",
        },
    }
    a = build_result(payload, generated_at_utc="2026-07-28T00:00:00Z")
    b = build_result(payload, generated_at_utc="2026-07-28T01:00:00Z")
    assert a["payload"] == b["payload"]
    assert a["payload_sha256"] == b["payload_sha256"]
    assert a["generated_at_utc"] != b["generated_at_utc"]
    assert a["payload_sha256"] == payload_sha256(a["payload"])


def test_prereg_merge_commit_is_frozen_stage_three_merge():
    assert PREREG_MERGE_COMMIT == "e42dafcf4a5f803c1af9a3535a980492c93e9c96"


def test_missing_required_payload_or_oos_opened_fails_closed():
    import pytest

    with pytest.raises(ValueError, match="필수 필드 누락"):
        build_result({}, generated_at_utc="x")
