from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from types import MappingProxyType
from typing import Any

import pytest

import src.research.gap_rvol.development as development_module
from src.research.gap_rvol.contract import all_specs, load_contract
from src.research.gap_rvol.development import (
    DevelopmentPaths,
    NotRunError,
    build_development_feature_pass,
    run_development,
)
from src.research.gap_rvol.manifest import (
    IMPLEMENTATION_MERGE_COMMIT,
    IMPLEMENTATION_REVIEW_EVIDENCE_URL,
    PreconditionEvidence,
    SnapshotDefectEvidence,
)
from src.research.gap_rvol.simulator import DaySimulationResult
from src.research.orb.identity import IdentityCheck, IdentityResult
from tests.gap_rvol_test_helpers import minute_bars


def _thaw(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _preconditions() -> PreconditionEvidence:
    return PreconditionEvidence(
        implementation_merge_commit=IMPLEMENTATION_MERGE_COMMIT,
        implementation_review_evidence_url=IMPLEMENTATION_REVIEW_EVIDENCE_URL,
        implementation_map_status="tracked",
        runner_reviewed_commit="a" * 40,
        runner_code_review_status="approved",
        runner_architect_status="clear",
        runner_review_evidence_url=(
            "https://github.com/peg5325/ai-trading-system/pull/102"
            "#issuecomment-5127000000"
        ),
        loader_boundary_status="passed",
        loader_boundary_test=(
            "tests/test_gap_rvol_loading_and_map.py::"
            "test_performance_package_and_runner_have_no_loader_bypass"
        ),
    )


def _matching_identity() -> IdentityResult:
    identity = load_contract()["data_identity"]
    pairs = (
        ("snapshot_file", "snapshot_file_sha256"),
        ("snapshot_content", "snapshot_content_sha256"),
        ("manifest_data", "manifest_data_sha256"),
        ("raw_content", "raw_content_sha256"),
    )
    return IdentityResult(
        checks=tuple(
            IdentityCheck(name, identity[key], identity[key], "match")
            for name, key in pairs
        ),
        ok=True,
        verdict=None,
    )


def test_run_development_wires_identity_feature_simulation_and_manifest(monkeypatch):
    contract = load_contract()
    day = date(2024, 3, 4)
    identity = _matching_identity()
    calls = []

    monkeypatch.setattr(
        development_module,
        "verify_inputs",
        lambda **kwargs: calls.append(("identity", kwargs)) or identity,
    )
    monkeypatch.setattr(
        development_module,
        "require_identity",
        lambda value: calls.append(("require", value)),
    )
    monkeypatch.setattr(
        development_module,
        "build_development_feature_pass",
        lambda **kwargs: calls.append(("features", kwargs)) or object(),
    )
    results = {
        spec.name: (
            DaySimulationResult(
                day=day,
                spec_name=spec.name,
                trades=(),
                skipped=(),
                excluded=(),
                filtered=(),
                major_defects=(),
                open_positions_at_close=0,
            ),
        )
        for spec in all_specs(contract)
    }
    monkeypatch.setattr(
        development_module,
        "simulate_all_specs",
        lambda **kwargs: calls.append(("simulate", kwargs)) or results,
    )
    monkeypatch.setattr(
        development_module,
        "SnapshotDefectEvidence",
        type(
            "FakeSnapshotDefectEvidence",
            (),
            {
                "from_sources": staticmethod(
                    lambda **_kwargs: SnapshotDefectEvidence(0, ())
                )
            },
        ),
    )
    monkeypatch.setattr(
        development_module,
        "build_result",
        lambda evidence, **kwargs: {
            "producer": evidence.producer_commit,
            "generated": kwargs["generated_at_utc"],
        },
    )

    run = run_development(
        paths=DevelopmentPaths(
            snapshot_path="/snapshot.sqlite",
            raw_root="/raw",
            manifest_path=development_module.tracked_manifest_path(),
        ),
        producer_commit="a" * 40,
        generated_at_utc="2026-07-30T08:00:00Z",
        preconditions=_preconditions(),
    )
    assert run.result["producer"] == "a" * 40
    assert [name for name, _ in calls] == [
        "identity",
        "require",
        "features",
        "simulate",
    ]


def test_custom_manifest_is_rejected_before_identity(monkeypatch, tmp_path):
    forged_path = tmp_path / "forged.json"
    forged_path.write_text(json.dumps({"symbols": []}), encoding="utf-8")
    monkeypatch.setattr(
        development_module,
        "verify_inputs",
        lambda **_kwargs: pytest.fail("custom manifest must fail before identity"),
    )
    with pytest.raises(NotRunError, match=r"NOT_RUN P4: .*tracked path"):
        run_development(
            paths=DevelopmentPaths(
                snapshot_path="/snapshot.sqlite",
                raw_root="/raw",
                manifest_path=str(forged_path),
            ),
            producer_commit="a" * 40,
            generated_at_utc="2026-07-30T08:00:00Z",
            preconditions=_preconditions(),
        )


def test_tracked_manifest_file_hash_is_pinned():
    path = development_module.tracked_manifest_path()
    assert development_module._file_sha256(path) == (
        development_module.TRACKED_SNAPSHOT_MANIFEST_SHA256
    )


def test_generated_time_and_review_evidence_are_fail_closed(tmp_path):
    paths = DevelopmentPaths(
        snapshot_path="/snapshot.sqlite",
        raw_root="/raw",
        manifest_path=str(tmp_path / "missing.json"),
    )
    with pytest.raises(ValueError, match="UTC Z"):
        run_development(
            paths=paths,
            producer_commit="a" * 40,
            generated_at_utc="now",
            preconditions=_preconditions(),
        )
    with pytest.raises(NotRunError, match=r"NOT_RUN P1-P3/P5: .*review evidence"):
        run_development(
            paths=paths,
            producer_commit="a" * 40,
            generated_at_utc="2026-07-30T08:00:00Z",
            preconditions=replace(
                _preconditions(),
                runner_review_evidence_url="https://example.com/review",
            ),
        )
    with pytest.raises(NotRunError, match=r"NOT_RUN P1-P3/P5: .*producer_commit"):
        run_development(
            paths=paths,
            producer_commit="b" * 40,
            generated_at_utc="2026-07-30T08:00:00Z",
            preconditions=_preconditions(),
        )


def test_p4_failure_is_not_run_and_never_reaches_feature_pass(monkeypatch):
    monkeypatch.setattr(
        development_module,
        "verify_inputs",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("raw hash mismatch")),
    )
    monkeypatch.setattr(
        development_module,
        "build_development_feature_pass",
        lambda **_kwargs: pytest.fail("NOT_RUN must not calculate performance"),
    )
    with pytest.raises(NotRunError, match=r"NOT_RUN P4: raw hash mismatch") as error:
        run_development(
            paths=DevelopmentPaths(
                snapshot_path="/snapshot.sqlite",
                raw_root="/raw",
                manifest_path=development_module.tracked_manifest_path(),
            ),
            producer_commit="a" * 40,
            generated_at_utc="2026-07-30T08:00:00Z",
            preconditions=_preconditions(),
        )
    assert error.value.verdict == "NOT_RUN"


def test_feature_pass_uses_prior_day_warmup_and_guarded_development_days(monkeypatch):
    contract = json.loads(json.dumps(_thaw(load_contract())))
    contract["windows"] |= {
        "development_data_start": "2024-03-05",
        "development_data_end": "2024-03-06",
        "development_data_trading_days": 2,
        "warmup_trading_days": 1,
        "development_eval_start": "2024-03-06",
        "development_eval_end": "2024-03-06",
        "development_eval_trading_days": 1,
        "blind_oos_start": "2024-03-07",
        "blind_oos_end": "2024-03-08",
        "blind_oos_trading_days": 2,
    }
    data_days = (date(2024, 3, 5), date(2024, 3, 6))
    eval_days = (date(2024, 3, 6),)
    monkeypatch.setattr(
        development_module, "development_data_days", lambda _contract: data_days
    )
    monkeypatch.setattr(
        development_module, "development_eval_days", lambda _contract: eval_days
    )
    monkeypatch.setattr(
        development_module,
        "blind_oos_days",
        lambda _contract: (date(2024, 3, 7), date(2024, 3, 8)),
    )
    calls: list[tuple[str, date]] = []

    def loader(_db_path: str, symbol: str, day: date):
        calls.append((symbol, day))
        return minute_bars(day, symbol=symbol, price=100.0, volume=100.0)

    feature_pass = build_development_feature_pass(
        contract=contract,
        db_path="/synthetic.sqlite",
        loader=loader,
    )
    symbols = development_module.universe_symbols(contract)
    assert feature_pass.data_days == data_days
    assert feature_pass.eval_days == eval_days
    assert len(feature_pass.symbols) == 30
    assert len(all_specs(contract)) == 9
    assert set(calls) == {
        (symbol, day)
        for symbol in symbols
        for day in (date(2024, 3, 4), *data_days)
    }
