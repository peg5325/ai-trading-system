from __future__ import annotations

import dataclasses

import pytest

from src.research.gap_rvol import contract as c
from src.research.gap_rvol.windows import (
    blind_oos_days,
    development_data_days,
    development_eval_days,
    folds,
)


def test_frozen_hashes_match_tracked_preregistration():
    assert c.compute_md_sha256() == c.PREREG_MD_SHA256
    assert c.compute_json_sha256() == c.PREREG_JSON_SHA256


def test_default_loader_is_fail_closed_and_immutable(monkeypatch):
    contract = c.load_contract()
    with pytest.raises(TypeError):
        contract["signal"]["retention_fraction"] = 0.9
    monkeypatch.setattr(c, "PREREG_MD_SHA256", "0" * 64)
    with pytest.raises(c.ContractIntegrityError):
        c.load_contract()


def test_primary_and_eight_neighbors_are_contract_derived():
    contract = c.load_contract()
    primary = c.primary_spec(contract)
    neighbors = c.neighbor_specs(contract)
    assert dataclasses.asdict(primary) == {
        "name": "primary",
        "gap_threshold": 0.003,
        "rvol_threshold": 1.5,
        "retention_fraction": 0.5,
        "target_r_multiple": 2.0,
        "authority": "decides_go_nogo",
    }
    assert len(neighbors) == 8
    assert all(item.authority == "diagnostic_only" for item in neighbors)
    base = dataclasses.asdict(primary)
    base.pop("name")
    base.pop("authority")
    for item in neighbors:
        values = dataclasses.asdict(item)
        values.pop("name")
        values.pop("authority")
        assert sum(values[key] != base[key] for key in base) == 1


def test_decision_and_rvol_specs_are_frozen_values():
    contract = c.load_contract()
    assert c.decision_bar_spec(contract) == c.GapDecisionSpec(5, 5)
    assert c.rvol_spec(contract) == c.RvolSpec(15, 20, 15)


def test_development_and_oos_windows_are_exactly_frozen():
    contract = c.load_contract()
    assert len(development_data_days(contract)) == 690
    assert len(development_eval_days(contract)) == 670
    assert len(blind_oos_days(contract)) == 125
    assert [fold.trading_days for fold in folds(contract)] == [103] + [63] * 9
