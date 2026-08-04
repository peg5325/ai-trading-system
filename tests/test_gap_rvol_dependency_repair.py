from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from src.research.gap_rvol.contract import load_contract
from src.research.gap_rvol.dependency_repair import (
    build_dependency_plan,
    build_dependency_probe,
    build_dependency_targets,
    load_frozen_dependency_plan,
    plan_bytes,
    probe_bytes,
    probe_observation,
)
from src.research.gap_rvol.windows import development_data_days
from tests.gap_rvol_test_helpers import minute_bars


def _audit(event_day: date) -> dict:
    return {
        "scope": {"blind_oos_opened": False},
        "exclusion_audit": {
            "all_specs_identical": True,
            "events": [{
                "symbol": "AAPL",
                "day": event_day.isoformat(),
                "reasons": [
                    "insufficient_rvol_lookback",
                    "no_previous_regular_close",
                ],
            }],
        },
    }


def test_dependency_targets_are_exact_missing_prior_inputs():
    contract = load_contract()
    days = development_data_days(contract)
    event_day = days[20]
    missing = set(days[14:20])

    def loader(symbol: str, day: date):
        assert symbol == "AAPL"
        return () if day in missing else minute_bars(day)

    targets = build_dependency_targets(
        audit=_audit(event_day), contract=contract, loader=loader
    )
    assert {target.day for target in targets} == missing
    prior = next(target for target in targets if target.day == days[19])
    assert prior.roles == ("prior_regular_close", "rvol_15m_lookback")
    assert prior.has_usable_regular_close is False
    assert prior.has_complete_rvol_window is False
    assert all(target.day < event_day for target in targets)


def test_dependency_planner_rejects_unexplained_rvol_reason():
    contract = load_contract()
    event_day = development_data_days(contract)[20]
    with pytest.raises(ValueError, match="not explained"):
        build_dependency_targets(
            audit=_audit(event_day),
            contract=contract,
            loader=lambda symbol, day: minute_bars(day, symbol=symbol),
        )


def test_dependency_plan_and_probe_are_deterministic_and_complete():
    contract = load_contract()
    days = development_data_days(contract)
    event_day = days[20]
    missing = set(days[14:20])
    targets = build_dependency_targets(
        audit=_audit(event_day),
        contract=contract,
        loader=lambda symbol, day: (
            () if day in missing else minute_bars(day, symbol=symbol)
        ),
    )
    plan = build_dependency_plan(
        targets=targets,
        audit_sha256="a" * 64,
        prereg_sha256="b" * 64,
        snapshot_identity={"snapshot_file_sha256": "c" * 64},
        generated_at_utc="2026-08-04T03:00:00Z",
    )
    assert plan_bytes(plan) == plan_bytes(plan)
    observations = [
        probe_observation(
            symbol=item["symbol"],
            target_day=date.fromisoformat(item["day"]),
            result={"candles": []},
        )
        for item in plan["targets"]
    ]
    report = build_dependency_probe(
        plan=plan,
        observations=observations,
        generated_at_utc="2026-08-04T03:01:00Z",
        http_429=0,
    )
    assert report["verdict"] == "DEPENDENCY_SOURCE_UNAVAILABLE"
    assert report["available_count"] == 0
    assert probe_bytes(report) == probe_bytes(report)
    with pytest.raises(ValueError, match="frozen target set"):
        build_dependency_probe(
            plan=plan,
            observations=observations[:-1],
            generated_at_utc="2026-08-04T03:01:00Z",
            http_429=0,
        )


def test_tracked_dependency_plan_is_exact_and_oos_closed():
    path = Path("docs/research/MANIFEST_P0_US30_GAP_RVOL_D9_DEPENDENCIES.json")
    if not path.exists():
        pytest.skip("tracked dependency plan is generated after implementation review")
    plan = json.loads(path.read_text(encoding="utf-8"))
    assert plan_bytes(plan) == path.read_bytes()
    assert plan["target_count"] == 458
    assert plan["role_counts"] == {
        "prior_regular_close": 193,
        "rvol_15m_lookback": 448,
    }
    assert plan["target_year_counts"] == {"2023": 57, "2024": 401}
    assert plan["target_min_day"] == "2023-04-21"
    assert plan["target_max_day"] == "2024-08-26"
    assert len(plan["symbols"]) == 30
    assert plan["scope"]["blind_oos_opened"] is False
    assert plan["scope"]["performance_computed"] is False
    assert plan["scope"]["canonical_db_written"] is False
    assert load_frozen_dependency_plan(str(path)) == plan

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["targets"] = tampered["targets"][:-1]
    bad = path.parent / "dependency-plan-tampered.test.json"
    try:
        bad.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(ValueError, match="file hash"):
            load_frozen_dependency_plan(str(bad))
    finally:
        bad.unlink(missing_ok=True)


def test_tracked_dependency_probe_is_sanitized_when_present():
    path = Path("docs/research/PROBE_P0_US30_GAP_RVOL_D9_DEPENDENCIES_TOSS.json")
    if not path.exists():
        pytest.skip("live dependency availability probe has not run")
    report = json.loads(path.read_text(encoding="utf-8"))
    assert probe_bytes(report) == path.read_bytes()
    assert report["target_count"] == 458
    assert report["verdict"] == "DEPENDENCY_SOURCE_PARTIAL"
    assert report["available_count"] == 265
    assert report["unavailable_count"] == 193
    assert report["available_role_counts"] == {"rvol_15m_lookback": 265}
    assert report["unavailable_role_counts"] == {
        "prior_regular_close": 193,
        "rvol_15m_lookback": 183,
    }
    assert report["http_429"] == 1
    assert report["plan_sha256"] == (
        "7a27ee176f910e943c955ac2db034dfebf47d7f2ffa5583c3edf244d576014c0"
    )
    assert report["scope"] == {
        "blind_oos_opened": False,
        "database_written": False,
        "performance_computed": False,
        "prices_persisted": False,
        "read_only": True,
    }
    serialized = path.read_text(encoding="utf-8")
    for forbidden in ("openPrice", "highPrice", "lowPrice", "closePrice", "volume"):
        assert forbidden not in serialized
