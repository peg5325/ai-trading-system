from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.research.dema_stoch.development import canonical_json_bytes
from src.research.dema_stoch.report import render_report

RESULT_JSON = Path(
    "docs/research/RESULT_P0_US30_DEMA_STOCH_MOMENTUM_DEVELOPMENT.json"
)
RESULT_MD = Path(
    "docs/research/RESULT_P0_US30_DEMA_STOCH_MOMENTUM_DEVELOPMENT.md"
)


def _result() -> dict:
    return json.loads(RESULT_JSON.read_text(encoding="utf-8"))


def test_tracked_result_has_deterministic_payload_and_report():
    result = _result()
    payload = dict(result)
    expected_hash = payload.pop("payload_sha256")
    assert hashlib.sha256(canonical_json_bytes(payload)).hexdigest() == expected_hash
    assert expected_hash == (
        "003c9729ba2f46b2d2a39cfb84e2b44b190a7443ec2128b00aac6107801ee89f"
    )
    assert RESULT_MD.read_text(encoding="utf-8") == render_report(result)


def test_tracked_result_is_bound_to_reviewed_development_scope():
    result = _result()
    assert result["producer_commit"] == (
        "36db7e7182834a99da02a44651d5b3860e8f6fc7"
    )
    assert result["review_evidence_url"] == (
        "https://github.com/peg5325/ai-trading-system/pull/129"
        "#issuecomment-5248482682"
    )
    assert result["scope"] == "development-only"
    assert result["window"] == {
        "start": "2023-05-18",
        "end": "2026-01-20",
        "trading_days": 670,
    }
    assert result["blind_oos_opened"] is False
    assert result["external_provider_used"] is False
    assert all(check["status"] == "match" for check in result["identity"])


def test_tracked_result_preserves_no_selection_evidence():
    result = _result()
    assert result["candidate_count_before_portfolio"] == 4121
    assert result["trade_count"] == 3715
    assert result["direction_counts"] == {"LONG": 2031, "SHORT": 1684}
    assert result["gate"]["verdict"] == "NO_SELECTION"
    assert result["gate"]["failed"] == ["D3", "D4", "D5", "D6", "D7"]
    assert result["gate"]["items"]["D9"] is True
    official = result["stats"]["combined"]["official"]
    assert official["net_expectancy"] == pytest.approx(-63.71545397836911)
    assert official["profit_factor"] == pytest.approx(0.3581859395891475)
    assert official["positive_folds"] == 0
    assert result["defects"]["incomplete_symbol_day_ratio"] == pytest.approx(
        0.03651741293532338
    )
