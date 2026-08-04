from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.research.gap_rvol.provider_probe import (
    PROBE_DAYS,
    build_report,
    report_bytes,
    request_before,
    summarize_page,
)


def test_summarize_page_keeps_only_availability_metadata():
    result = {
        "candles": [{
            "timestamp": "2024-08-06T05:00:00.000-04:00",
            "openPrice": "1",
            "closePrice": "2",
            "volume": "3",
        }],
        "nextBefore": "cursor",
    }
    summary = summarize_page(
        symbol="SPY", target_day=date(2024, 8, 6), result=result
    )
    assert summary["has_target_day"] is True
    assert summary["returned_et_days"] == ["2024-08-06"]
    assert summary["row_count"] == 1
    assert "openPrice" not in json.dumps(summary)
    assert "closePrice" not in json.dumps(summary)
    assert "volume" not in json.dumps(summary)


def test_request_before_is_stable_and_after_regular_close():
    assert request_before(date(2024, 8, 1)) == "2024-08-02T08:59:00.000+09:00"


def test_build_report_requires_complete_frozen_matrix():
    symbols = ("SPY",)
    observations = [
        summarize_page(symbol="SPY", target_day=day, result={"candles": []})
        for day in PROBE_DAYS
    ]
    report = build_report(
        symbols=symbols,
        observations=observations,
        generated_at_utc="2026-08-04T00:00:00Z",
        http_429=0,
    )
    assert report["scope"]["read_only"] is True
    assert report["scope"]["performance_computed"] is False
    assert report["by_day"]["2024-08-01"]["missing_count"] == 1
    assert report_bytes(report) == report_bytes(report)

    with pytest.raises(ValueError, match="frozen matrix"):
        build_report(
            symbols=symbols,
            observations=observations[:-1],
            generated_at_utc="2026-08-04T00:00:00Z",
            http_429=0,
        )


def test_tracked_probe_is_complete_sanitized_and_oos_closed():
    path = Path("docs/research/PROBE_P0_US30_GAP_RVOL_D9_TOSS.json")
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report_bytes(report) == path.read_bytes()
    assert len(report["observations"]) == 330
    assert report["scope"] == {
        "blind_oos_opened": False,
        "database_written": False,
        "performance_computed": False,
        "prices_persisted": False,
        "read_only": True,
    }
    assert report["by_day"]["2024-08-01"]["available_count"] == 0
    assert report["by_day"]["2024-08-02"]["available_count"] == 0
    assert report["by_day"]["2024-08-05"]["available_count"] == 30
    serialized = path.read_text(encoding="utf-8")
    for forbidden in ("openPrice", "highPrice", "lowPrice", "closePrice", "volume"):
        assert forbidden not in serialized
