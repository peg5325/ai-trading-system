"""Run gate (§7): checks a research run spec against an authorization and the actual inputs, at the entry point.

The spec is one Markdown document; its machine-read values live in exactly one fenced block tagged
`json run-spec` (the prose explains, the block decides; nothing is kept twice). The gate checks contract
compliance and the presence of required evidence. It cannot judge whether a strategy is truly profitable.

Protection rules are not lifted by a line in a spec: a FINAL_TEST (seal opening), real orders, relaxed criteria,
a larger budget or a removed boundary need a separately approved authorization with an explicit scope and
version. This version accepts no FINAL_TEST at all.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from src.evalpath.ledger import MARKET_UNIT

RUN_TYPES = ("ENGINE_REPRO", "STRATEGY_EXPLORE", "FINAL_TEST")
NEVER_IN_THIS_VERSION = frozenset({"FINAL_TEST"})
REQUIRED = ("spec_id", "run_type", "authorization_id", "cost_contract_version", "cost_model_ids",
            "evaluation_contract_version", "data", "runs", "evidence")
DATA_KEYS = ("copy_sha256", "allowlist_sha256", "mask_sha256", "symbols", "interval", "windows")
# Evidence a run must state before it starts (P6 §0.5). An engine reproduction states why each item does not apply.
EVIDENCE_KEYS = ("difference_from_prior_failures", "changed_factors_and_reason", "edge_over_cost_basis",
                 "data_and_exposure_history", "prior_discard_reasons_still_apply", "budget_stop_and_bad_result",
                 "instruction_source")
FORBIDDEN_FLAGS = ("open_seal", "real_orders", "relax_criteria", "raise_budget", "lift_boundary")
_BLOCK = re.compile(r"```json run-spec\n(.*?)\n```", re.DOTALL)


class GateRefused(Exception):
    pass


def parse_spec(md_path: Path) -> dict:
    blocks = _BLOCK.findall(Path(md_path).read_text(encoding="utf-8"))
    if len(blocks) != 1:
        raise GateRefused(f"{md_path}: expected exactly one ```json run-spec block, found {len(blocks)}")
    try:
        return json.loads(blocks[0])
    except json.JSONDecodeError as exc:
        raise GateRefused(f"{md_path}: run-spec block is not JSON: {exc}") from exc


def load_authorizations(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def check(spec: dict, auths: dict, actual: dict, *, cost_models: set[str], cost_contract_version: str,
          evaluation_contract_version: str) -> dict:
    """Refuse unless the spec, its authorization and the actual inputs agree. Returns the granted scope.

    `actual` holds what the entry point measured itself: copy_sha256, allowlist_sha256, mask_sha256, symbols,
    interval, windows (from the verified copy manifest and the mask file), never values copied from the spec.
    """
    missing = [k for k in REQUIRED if k not in spec]
    if missing:
        raise GateRefused(f"spec misses {missing}")
    if spec["run_type"] not in RUN_TYPES:
        raise GateRefused(f"run_type {spec['run_type']!r} not in {RUN_TYPES}")
    if spec["run_type"] in NEVER_IN_THIS_VERSION:
        raise GateRefused(f"{spec['run_type']} is not accepted by this gate version (needs a managed approval)")
    flags = [f for f in FORBIDDEN_FLAGS if spec.get(f)]
    if flags:
        raise GateRefused(f"spec asks for protected changes {flags}; a spec line cannot authorize them")
    ev = spec["evidence"]
    if not isinstance(ev, dict):
        raise GateRefused("evidence must be an object with the required keys")
    empty =[k for k in EVIDENCE_KEYS if not str(ev.get(k, "")).strip()]
    if empty:
        raise GateRefused(f"evidence missing or empty: {empty}")

    g = auths.get("global", {})
    if g.get("halt"):
        raise GateRefused(f"all runs halted: {g.get('halt_reason')}")
    auth = auths.get("authorizations", {}).get(spec["authorization_id"])
    if auth is None:
        raise GateRefused(f"authorization {spec['authorization_id']!r} not found")
    if auth.get("status") != "active":
        raise GateRefused(f"authorization {spec['authorization_id']} is {auth.get('status')!r}")
    if spec["run_type"] not in auth["run_types"]:
        raise GateRefused(f"authorization {spec['authorization_id']} does not cover {spec['run_type']}")
    if auth.get("budget_unit") != MARKET_UNIT:
        raise GateRefused(f"authorization {spec['authorization_id']} budget unit {auth.get('budget_unit')!r} is not "
                          f"{MARKET_UNIT!r} (one real-market run_account call per unit)")

    if spec["cost_contract_version"] != cost_contract_version:
        raise GateRefused(f"cost contract {spec['cost_contract_version']!r} != code {cost_contract_version!r}")
    if spec["evaluation_contract_version"] != evaluation_contract_version:
        raise GateRefused(f"evaluation contract {spec['evaluation_contract_version']!r} != "
                          f"{evaluation_contract_version!r}")
    unknown = set(spec["cost_model_ids"]) - cost_models
    if unknown:
        raise GateRefused(f"unknown cost models {sorted(unknown)}")

    data = spec["data"]
    for k in DATA_KEYS:
        if data.get(k) != actual.get(k):
            raise GateRefused(f"data.{k}: spec {data.get(k)!r} != actual {actual.get(k)!r}")

    scope = auth["scope"]
    for run in spec["runs"]:
        if run["policy"] not in scope["policies"]:
            raise GateRefused(f"policy {run['policy']} outside authorization scope {scope['policies']}")
        if run["cost_model_id"] not in scope["cost_model_ids"] or run["cost_model_id"] not in spec["cost_model_ids"]:
            raise GateRefused(f"cost model {run['cost_model_id']} outside the spec or authorization scope")
        if run.get("delay", 0) not in scope.get("delays", [0]):
            raise GateRefused(f"delay {run.get('delay')} outside authorization scope")
    if data["windows"] != scope["windows"]:
        raise GateRefused(f"windows {data['windows']} != authorized {scope['windows']}")
    for key in ("symbols", "interval"):  # checked when the authorization restricts them
        if key in scope and data[key] != scope[key]:
            raise GateRefused(f"{key} {data[key]!r} != authorized {scope[key]!r}")
    return {"authorization_id": spec["authorization_id"], "budget": int(auth["budget"]),
            "budget_unit": auth["budget_unit"], "scope": scope}
