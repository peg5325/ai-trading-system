"""Fail-closed loader for the frozen Gap+RVOL preregistration."""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

PREREG_MD_PATH = "docs/research/PREREG_P0_US30_GAP_RVOL_V1.md"
PREREG_JSON_PATH = "docs/research/PREREG_P0_US30_GAP_RVOL_V1.json"
PREREG_MD_SHA256 = "88543b8bf30440751ab5af00d979dd39f0dfd7ac13100ea445b2c537849ee50e"
PREREG_JSON_SHA256 = "a181799c7301e09d49d90482975e8a0c86deb60ca216e6b9f900abc8bf73e29a"


class ContractIntegrityError(RuntimeError):
    """The tracked contract does not match the implementation's frozen copy."""


def repo_root() -> str:
    here = os.path.abspath(__file__)
    for _ in range(4):
        here = os.path.dirname(here)
    return here


def _normalized_md(text: str) -> str:
    lines = [
        re.sub(r"[ \t]+", " ", line).rstrip()
        for line in text.replace("\r\n", "\n").split("\n")
    ]
    return "\n".join(lines).strip() + "\n"


def compute_md_sha256(path: str | None = None) -> str:
    target = path or os.path.join(repo_root(), PREREG_MD_PATH)
    with open(target, encoding="utf-8") as handle:
        return hashlib.sha256(
            _normalized_md(handle.read()).encode("utf-8")
        ).hexdigest()


def compute_json_sha256(path: str | None = None) -> str:
    target = path or os.path.join(repo_root(), PREREG_JSON_PATH)
    with open(target, encoding="utf-8") as handle:
        data = json.load(handle)
    canonical = json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def load_contract(*, verify: bool = True) -> Mapping[str, Any]:
    md_path = os.path.join(repo_root(), PREREG_MD_PATH)
    json_path = os.path.join(repo_root(), PREREG_JSON_PATH)
    if verify and compute_md_sha256(md_path) != PREREG_MD_SHA256:
        raise ContractIntegrityError("Gap+RVOL preregistration Markdown hash mismatch")
    if verify and compute_json_sha256(json_path) != PREREG_JSON_SHA256:
        raise ContractIntegrityError("Gap+RVOL preregistration JSON hash mismatch")
    with open(json_path, encoding="utf-8") as handle:
        return _freeze(json.load(handle))


def require(mapping: Any, *keys: str) -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, Mapping) or key not in current:
            raise ContractIntegrityError(f"missing contract value: {'.'.join(keys)}")
        current = current[key]
    return current


@dataclass(frozen=True)
class SpecParams:
    name: str
    gap_threshold: float
    rvol_threshold: float
    retention_fraction: float
    target_r_multiple: float
    authority: str


def primary_spec(contract: Mapping[str, Any]) -> SpecParams:
    return SpecParams(
        name="primary",
        gap_threshold=float(require(contract, "features", "gap", "threshold_gte")),
        rvol_threshold=float(require(contract, "features", "rvol", "threshold_gte")),
        retention_fraction=float(require(contract, "signal", "retention_fraction")),
        target_r_multiple=float(require(contract, "risk", "target_r_multiple")),
        authority="decides_go_nogo",
    )


_AXIS_FIELDS = {
    "gap_threshold_gte": "gap_threshold",
    "rvol_threshold_gte": "rvol_threshold",
    "retention_fraction": "retention_fraction",
    "target_r_multiple": "target_r_multiple",
}


def neighbor_specs(contract: Mapping[str, Any]) -> tuple[SpecParams, ...]:
    base = primary_spec(contract)
    result: list[SpecParams] = []
    for variant in require(contract, "parameter_cliff_neighborhood", "variants"):
        axis = str(variant["axis"])
        if axis not in _AXIS_FIELDS:
            raise ContractIntegrityError(f"unknown neighbor axis: {axis}")
        field = _AXIS_FIELDS[axis]
        for value in variant["neighbors"]:
            values = {
                "gap_threshold": base.gap_threshold,
                "rvol_threshold": base.rvol_threshold,
                "retention_fraction": base.retention_fraction,
                "target_r_multiple": base.target_r_multiple,
            }
            values[field] = float(value)
            result.append(SpecParams(
                name=f"{axis.removesuffix('_gte')}_{value}",
                authority="diagnostic_only",
                **values,
            ))
    return tuple(result)


def all_specs(contract: Mapping[str, Any]) -> tuple[SpecParams, ...]:
    return (primary_spec(contract), *neighbor_specs(contract))


@dataclass(frozen=True)
class GapDecisionSpec:
    minutes: int
    min_bars: int


def decision_bar_spec(contract: Mapping[str, Any]) -> GapDecisionSpec:
    return GapDecisionSpec(
        minutes=int(require(contract, "signal", "decision_bar_minutes")),
        min_bars=int(require(contract, "signal", "decision_bar_min_bars")),
    )


@dataclass(frozen=True)
class RvolSpec:
    minutes: int
    lookback_days: int
    min_valid_days: int


def rvol_spec(contract: Mapping[str, Any]) -> RvolSpec:
    return RvolSpec(
        minutes=int(require(contract, "features", "rvol", "window_minutes")),
        lookback_days=int(require(contract, "features", "rvol", "lookback_trading_days")),
        min_valid_days=int(require(contract, "features", "rvol", "min_valid_lookback_days")),
    )


@dataclass(frozen=True)
class SignalWindowSpec:
    observation_start_minute: int
    latest_signal_minute: int
    latest_fill_minute: int


def _time_to_minute(value: object) -> int:
    hour, minute = str(value).split(":")
    return int(hour) * 60 + int(minute)


def signal_window_spec(contract: Mapping[str, Any]) -> SignalWindowSpec:
    return SignalWindowSpec(
        observation_start_minute=_time_to_minute(
            require(contract, "signal", "observation_start_et")),
        latest_signal_minute=_time_to_minute(
            require(contract, "signal", "latest_signal_bar_start_et")),
        latest_fill_minute=_time_to_minute(
            require(contract, "signal", "latest_fill_bar_start_et")),
    )
