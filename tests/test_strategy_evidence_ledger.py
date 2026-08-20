from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "docs/research/STRATEGY_EVIDENCE_LEDGER_2026-08-20.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_ledger_source_hashes_and_summary_are_exact() -> None:
    ledger = _load(LEDGER)
    assert ledger["summary"] == {
        "hypotheses": 12,
        "conclusive_negative": 10,
        "inconclusive_data": 2,
        "selected": 0,
        "oos_or_paper_eligible": 0,
    }
    assert len(ledger["entries"]) == 12
    for entry in ledger["entries"]:
        source = ROOT / entry["source"]
        assert source.is_file()
        assert _sha256(source) == entry["source_sha256"]


def test_json_strategy_verdicts_and_metrics_match_sources() -> None:
    ledger = _load(LEDGER)
    entries = {entry["id"]: entry for entry in ledger["entries"]}

    direct = {
        "box_rsi_volume_mean_reversion": (
            "docs/research/RESULT_P0_US30_BOX_RSI_VOLUME_MR_DEVELOPMENT.json",
            "stats",
            "official",
        ),
        "afternoon_continuation": (
            "docs/research/RESULT_P0_US30_AFTERNOON_CONTINUATION_DEVELOPMENT.json",
            "stats",
            "official",
        ),
        "gap_down_fade": (
            "docs/research/RESULT_P0_US30_GAP_DOWN_FADE_DEVELOPMENT.json",
            "stats",
            "official",
        ),
        "spy_intraday_momentum": (
            "docs/research/RESULT_SPY_INTRADAY_MOMENTUM_DEVELOPMENT.json",
            "stats",
            "official",
        ),
    }
    for entry_id, (path, stats_key, official_key) in direct.items():
        source = _load(ROOT / path)
        entry = entries[entry_id]
        official = source[stats_key][official_key]
        assert entry["verdict"] == source["gate"]["verdict"]
        assert entry["trade_count"] == source["trade_count"]
        assert entry["official_net_pnl"] == pytest.approx(
            official["cumulative_net_pnl"], abs=1e-6
        )
        assert entry["official_expectancy"] == pytest.approx(
            official["net_expectancy"], abs=1e-6
        )
        assert entry["official_profit_factor"] == pytest.approx(
            official["profit_factor"], abs=1e-6
        )

    dema = _load(
        ROOT / "docs/research/RESULT_P0_US30_DEMA_STOCH_MOMENTUM_DEVELOPMENT.json"
    )
    assert entries["dema_stochastic_momentum"]["official_net_pnl"] == pytest.approx(
        dema["stats"]["combined"]["official"]["cumulative_net_pnl"], abs=1e-6
    )

    for entry_id, path, spec in (
        (
            "close_pressure_reversal_3d",
            "docs/research/RESULT_P0_US30_CLOSE_PRESSURE_REVERSAL_DEVELOPMENT.json",
            "primary_3d",
        ),
        (
            "high52_rs_breakout_5d",
            "docs/research/RESULT_P0_US30_HIGH52_RS_BREAKOUT_DEVELOPMENT.json",
            "primary_5d",
        ),
    ):
        source = _load(ROOT / path)
        entry = entries[entry_id]
        official = source["specs"][spec]["cost_levels"]["official"]
        assert entry["verdict"] == source["verdict"]
        assert entry["official_net_pnl"] == pytest.approx(
            official["total_net_pnl"], abs=1e-6
        )
        assert entry["positive_folds"] == source["specs"][spec][
            "positive_folds_official"
        ]


def test_no_entry_is_promotable_and_h4_claim_is_present() -> None:
    ledger = _load(LEDGER)
    assert ledger["decision"] == {
        "selected_strategy": None,
        "open_blind_oos": False,
        "start_paper": False,
        "next_research": (
            "new preregistration on a distinct low-turnover 5-trading-day hypothesis"
        ),
    }
    assert {entry["promotion_status"] for entry in ledger["entries"]} <= {
        "CLOSED",
        "BLOCKED_DATA",
    }
    h4 = next(entry for entry in ledger["entries"] if entry["id"] == "h4_screener_ranking")
    assert h4["verdict"] == "NULL"
    assert "H4 = NULL" in (ROOT / h4["source"]).read_text(encoding="utf-8")


def test_wrapped_gross_and_sector_schemas_match() -> None:
    ledger = _load(LEDGER)
    entries = {entry["id"]: entry for entry in ledger["entries"]}

    for entry_id, filename in (
        ("orb_intraday", "RESULT_P0_US30_ORB_DEVELOPMENT.json"),
        ("gap_rvol_intraday", "RESULT_P0_US30_GAP_RVOL_DEVELOPMENT.json"),
    ):
        source = _load(ROOT / "docs/research" / filename)["payload"]
        entry = entries[entry_id]
        official = source["stats"]["official"]
        assert entry["verdict"] == source["verdict"]["value"]
        assert entry["official_net_pnl"] == pytest.approx(
            official["cumulative_net_pnl"], abs=1e-6
        )
        assert entry["positive_folds"] == official["positive_folds"]

    gross = _load(
        ROOT / "docs/research/RESULT_P0_US30_GROSS_EDGE_SCREEN.json"
    )
    best = max(gross["candidates"], key=lambda item: item["mean_daily_gross_pct_points"])
    assert entries["intraday_gross_edge_screen"]["verdict"] == gross["verdict"]
    assert entries["intraday_gross_edge_screen"]["official_expectancy"] == pytest.approx(
        best["mean_daily_gross_pct_points"], abs=1e-6
    )

    sector = _load(
        ROOT / "docs/research/RESULT_P0_SECTOR_ETF_WEEKLY_MOMENTUM_DEVELOPMENT.json"
    )
    official = sector["primary"]["metrics"]["official"]
    entry = entries["sector_etf_weekly_momentum"]
    assert entry["verdict"] == sector["verdict"]
    assert entry["official_net_pnl"] == pytest.approx(
        official["total_net_pnl"], abs=1e-6
    )
    assert entry["positive_folds"] == sector["positive_excess_folds_official"]


def test_erratum_entries_cannot_be_promoted() -> None:
    ledger = _load(LEDGER)
    for entry in ledger["entries"]:
        if not entry["status_class"].endswith("WITH_ERRATUM"):
            continue
        source = _load(ROOT / entry["source"])
        assert source["prereg_compliance"] == (
            "DEVIATION_ACCEPTED_BY_APPEND_ONLY_ERRATUM"
        )
        assert source["blind_oos_performance_accessed"] is False
        assert source["current_execution_blind_oos_ohlcv_accessed"] is False
        assert entry["promotion_status"] == "CLOSED"
