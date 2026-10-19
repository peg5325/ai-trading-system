"""Run gate, run ledger and the engine-reproduction driver (§7). tmp files only; the compute step is injected.

Authorizations here are FAKE test files (canonical unit market_data_run_account). The real config holds only the
closed 2026-10-16 authorization, which the gate must refuse.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from test_daytrade_v11_precount import make_db

from src.daytrade_v13.policy import COST_CONTRACT_VERSION, COST_MODELS
from src.evalpath import ledger
from src.evalpath.gate import GateRefused, check, parse_spec
from src.evalpath.research_copy import build_copy

ROOT = Path(__file__).resolve().parents[1]
REAL_AUTHS = json.loads((ROOT / "config" / "eval_authorizations.json").read_text(encoding="utf-8"))
AUTH_ID = "AUTH-TEST-FAKE"
WINDOWS = [{"start": "2023-04-11", "end_exclusive": "2026-01-21"}]
AUTHS = {"schema": "eval_authorizations/v1", "global": {"halt": False, "halt_reason": None},
         "authorizations": {AUTH_ID: {
             "status": "active", "approved_by": "test", "approved_ref": "fake", "run_types": ["ENGINE_REPRO"],
             "budget": 8, "budget_unit": ledger.MARKET_UNIT,
             "scope": {"policies": ["v1.0+", "v1.3"], "cost_model_ids": ["CM030_v1", "CM020_diag_v1"],
                       "delays": [0], "windows": WINDOWS}}}}
EVAL = "v13-engine-repro/v1 (PREREG_DAYTRADE_V1_3_SIZING arms, v1.1 mask 84d22f46)"
EVIDENCE = {k: "n/a: engine reproduction of a rejected arm; see spec prose" for k in (
    "difference_from_prior_failures", "changed_factors_and_reason", "edge_over_cost_basis",
    "data_and_exposure_history", "prior_discard_reasons_still_apply", "budget_stop_and_bad_result",
    "instruction_source")}


def actual(**kw):
    a = {"copy_sha256": "c" * 64, "allowlist_sha256": "a" * 64, "mask_sha256": "m" * 64,
         "symbols": ["QQQ", "TQQQ", "SQQQ"], "interval": "1m", "windows": WINDOWS}
    a.update(kw)
    return a


def spec(**kw):
    s = {"spec_id": "S1", "run_type": "ENGINE_REPRO", "authorization_id": AUTH_ID,
         "cost_contract_version": COST_CONTRACT_VERSION, "cost_model_ids": ["CM030_v1", "CM020_diag_v1"],
         "evaluation_contract_version": EVAL, "data": actual(), "budget": {"account_sim_max": 100},
         "runs": [{"run_key": "pre-CM030-Ra", "phase": "pre", "policy": "v1.0+", "cost_model_id": "CM030_v1"}],
         "evidence": dict(EVIDENCE)}
    s.update(kw)
    return s


def gate(s, auths=AUTHS, act=None):
    return check(s, auths, act or actual(), cost_models=set(COST_MODELS), cost_contract_version=COST_CONTRACT_VERSION,
                 evaluation_contract_version=EVAL)


def test_budget_comes_from_the_authorization_not_the_spec():
    g = gate(spec())
    assert g["budget"] == 8 and g["budget_unit"] == ledger.MARKET_UNIT


def test_the_real_closed_authorization_and_a_run_arm_unit_are_refused():
    old = "AUTH-20261016-V13-ENGINE-REPRO"
    assert REAL_AUTHS["authorizations"][old]["status"] == "closed"
    assert not [k for k, v in REAL_AUTHS["authorizations"].items() if v["status"] == "active"]
    with pytest.raises(GateRefused, match="closed"):
        gate(spec(authorization_id=old), auths=REAL_AUTHS)
    a = copy.deepcopy(AUTHS)
    a["authorizations"][AUTH_ID]["budget_unit"] = "run_arm"
    with pytest.raises(GateRefused, match="budget unit"):
        gate(spec(), auths=a)


@pytest.mark.parametrize("change,match", [
    ({"run_type": "FINAL_TEST"}, "not accepted"),
    ({"run_type": "STRATEGY_EXPLORE"}, "does not cover"),
    ({"run_type": "OTHER"}, "not in"),
    ({"open_seal": True}, "protected"),
    ({"raise_budget": True}, "protected"),
    ({"relax_criteria": True}, "protected"),
    ({"real_orders": True}, "protected"),
    ({"cost_contract_version": "cost_contract/v1"}, "cost contract"),
    ({"evaluation_contract_version": "other"}, "evaluation contract"),
    ({"cost_model_ids": ["CM010"]}, "unknown cost models"),
    ({"authorization_id": "AUTH-NONE"}, "not found"),
])
def test_spec_refusals(change, match):
    with pytest.raises(GateRefused, match=match):
        gate(spec(**change))


def test_missing_or_empty_evidence_is_refused():
    s = spec()
    s["evidence"]["edge_over_cost_basis"] = "  "
    with pytest.raises(GateRefused, match="evidence"):
        gate(s)
    s = spec()
    del s["evidence"]["instruction_source"]
    with pytest.raises(GateRefused, match="evidence"):
        gate(s)


@pytest.mark.parametrize("key,value", [("copy_sha256", "d" * 64), ("mask_sha256", "x"), ("symbols", ["QQQ"]),
                                       ("interval", "1d"), ("windows", [{"start": "2023-04-11",
                                                                         "end_exclusive": "2026-07-22"}])])
def test_spec_data_must_equal_what_the_entry_point_measured(key, value):
    with pytest.raises(GateRefused, match=f"data.{key}"):
        gate(spec(), act=actual(**{key: value}))


@pytest.mark.parametrize("run,match", [
    ({"policy": "v1.3-cap", "cost_model_id": "CM030_v1"}, "policy"),
    ({"policy": "v1.0+", "cost_model_id": "CM030_v1x1.5"}, "cost model"),
    ({"policy": "v1.0+", "cost_model_id": "CM030_v1", "delay": 1}, "delay"),
])
def test_runs_outside_the_authorized_scope_are_refused(run, match):
    with pytest.raises(GateRefused, match=match):
        gate(spec(runs=[{"run_key": "x", "phase": "pre", **run}]))


def test_halt_and_revocation():
    a = copy.deepcopy(AUTHS)
    a["global"]["halt"] = True
    with pytest.raises(GateRefused, match="halted"):
        gate(spec(), auths=a)
    a = copy.deepcopy(AUTHS)
    a["authorizations"][AUTH_ID]["status"] = "revoked"
    with pytest.raises(GateRefused, match="revoked"):
        gate(spec(), auths=a)


def test_spec_markdown_has_exactly_one_machine_block(tmp_path):
    p = tmp_path / "s.md"
    block = "```json run-spec\n" + json.dumps(spec()) + "\n```\n"
    p.write_text("# prose\n\n" + block, encoding="utf-8")
    assert parse_spec(p)["spec_id"] == "S1"
    p.write_text(block + block, encoding="utf-8")
    with pytest.raises(GateRefused, match="exactly one"):
        parse_spec(p)
    p.write_text("no block", encoding="utf-8")
    with pytest.raises(GateRefused, match="exactly one"):
        parse_spec(p)
    p.write_text("```json run-spec\n{bad\n```\n", encoding="utf-8")
    with pytest.raises(GateRefused, match="not JSON"):
        parse_spec(p)


def test_ledger_counts_reservations_across_restarts(tmp_path):
    lp = tmp_path / "l.jsonl"
    for i in range(3):
        ledger.reserve(lp, "A", 4, f"r{i}", 1, {})
    ledger.finish(lp, "r0", True, {})
    ledger.finish(lp, "r1", False, {})  # a failure still used its unit; r2 never finished (crash)
    assert ledger.used_units(ledger.read(lp), "A") == 3
    ledger.reserve(lp, "A", 4, "r3", 1, {})
    with pytest.raises(ledger.BudgetExceeded):
        ledger.reserve(lp, "A", 4, "r4", 1, {})
    with pytest.raises(ledger.BudgetExceeded, match="already reserved"):
        ledger.reserve(lp, "A", 99, "r0", 1, {})
    with pytest.raises(ValueError, match="already finished"):
        ledger.finish(lp, "r0", True, {})
    with pytest.raises(ValueError, match="no reservation"):
        ledger.finish(lp, "zz", True, {})
    assert ledger.used_units(ledger.read(lp), "B") == 0


# ---------------------------------------------------------------- driver


def _driver():
    path = ROOT / "scripts" / "eval_repro_v13.py"
    spec_ = importlib.util.spec_from_file_location("eval_repro_v13", path)
    mod = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(mod)
    return mod


def _setup(tmp_path, n_runs=2, budget=8):
    src = make_db(tmp_path / "src.sqlite")
    al = {"schema": "research_allowlist/v1", "id": "t", "interval": "1m", "symbols": ["QQQ", "TQQQ", "SQQQ"],
          "ranges": WINDOWS, "approved_by": "test", "approved_ref": "test"}
    copy_db = tmp_path / "copy.sqlite"
    man = build_copy(src, al, copy_db)
    mask = tmp_path / "mask.json"
    mask.write_text("{}", encoding="utf-8")
    s = spec(data=actual(copy_sha256=man["copy_sha256"], allowlist_sha256=man["allowlist_sha256"],
                         mask_sha256=hashlib.sha256(mask.read_bytes()).hexdigest()),
             runs=[{"run_key": f"k{i}", "phase": "pre", "policy": "v1.0+", "cost_model_id": "CM030_v1"}
                   for i in range(n_runs)])
    md = tmp_path / "spec.md"
    md.write_text("```json run-spec\n" + json.dumps(s) + "\n```\n", encoding="utf-8")
    auths = copy.deepcopy(AUTHS)
    auths["authorizations"][AUTH_ID]["budget"] = budget
    auth_file = tmp_path / "fake_authorizations.json"
    auth_file.write_text(json.dumps(auths), encoding="utf-8")
    files = {k: tmp_path / f"{k}.csv" for k in ("vix", "vix9d", "dix")}
    for f in files.values():
        f.write_text("x", encoding="utf-8")
    args = ["--spec", md, "--phase", "pre", "--code-root", tmp_path, "--code-label", "test", "--copy", copy_db,
            "--mask", mask, "--events", mask, "--vix-csv", files["vix"], "--vix9d-csv", files["vix9d"],
            "--dix-csv", files["dix"], "--ledger", tmp_path / "ledger.jsonl", "--out-dir", tmp_path / "out",
            "--authorizations", auth_file]
    return {"src": src, "copy": copy_db, "args": [str(a) for a in args], "tmp": tmp_path}


@pytest.fixture
def setup(tmp_path):
    return _setup(tmp_path)


def _arg(cmd, name):
    return cmd[cmd.index(name) + 1]


def guarded_fake_compute(calls):
    """Stands in for the compute process: uses the same ledger guard for each planned unit (synthetic stub)."""
    def run(cmd):
        calls.append(cmd)
        g = ledger.AccountCallGuard(Path(_arg(cmd, "--ledger")), _arg(cmd, "--authorization-id"),
                                    int(_arg(cmd, "--budget")), json.loads(_arg(cmd, "--unit-keys")))
        for key in json.loads(_arg(cmd, "--unit-keys")):
            g.call(key, lambda: None)
        out = Path(_arg(cmd, "--out"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("{}", encoding="utf-8")
        return 0
    return run


def test_driver_plans_account_units_and_passes_only_the_copy(setup):
    drv = _driver()
    calls: list = []
    assert drv.run(drv.parse(setup["args"]), compute=guarded_fake_compute(calls)) == 0
    assert len(calls) == 2
    assert [json.loads(_arg(c, "--unit-keys")) for c in calls] == [["S1/k0/A", "S1/k0/B"], ["S1/k1/A", "S1/k1/B"]]
    flat = " ".join(" ".join(c) for c in calls)
    assert str(setup["copy"]) in flat and str(setup["src"]) not in flat
    recs = ledger.read(setup["tmp"] / "ledger.jsonl")
    assert sum(r["event"] == "reserve" for r in recs) == 4
    assert {r["resource"] for r in recs} == {ledger.MARKET_UNIT}
    # the same units cannot be planned again
    again: list = []
    assert drv.run(drv.parse(setup["args"]), compute=guarded_fake_compute(again)) == 2 and again == []


def test_driver_refuses_a_plan_over_budget_before_any_compute(tmp_path):
    s = _setup(tmp_path, n_runs=3, budget=4)  # 3 runs x A/B = 6 account calls > 4
    drv = _driver()
    calls: list = []
    assert drv.run(drv.parse(s["args"]), compute=guarded_fake_compute(calls)) == 2
    assert calls == [] and ledger.read(tmp_path / "ledger.jsonl") == []


def test_driver_refuses_a_plain_db_and_stops_a_phase_on_failure(setup):
    drv = _driver()
    args = list(setup["args"])
    args[args.index("--copy") + 1] = str(setup["src"])
    assert drv.run(drv.parse(args), compute=guarded_fake_compute([])) == 2
    calls: list = []

    def failing(cmd):
        calls.append(cmd)
        return 3
    assert drv.run(drv.parse(setup["args"]), compute=failing) == 3
    assert len(calls) == 1  # the second run of the phase is not started


def test_compute_process_gets_only_allowlisted_environment(monkeypatch):
    drv = _driver()
    seen = {}

    class Done:
        returncode = 0

    def fake_run(cmd, env, check):
        seen.update(env)
        return Done()
    monkeypatch.setenv("FAKE_API_KEY_FOR_TEST", "sentinel-not-a-secret")
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    monkeypatch.setattr(drv.subprocess, "run", fake_run)
    assert drv.spawn(["python", "-c", "pass"]) == 0
    assert "FAKE_API_KEY_FOR_TEST" not in seen and "PYTHONPATH" not in seen
    assert set(seen) <= set(drv.ENV_ALLOWLIST)


def test_driver_refuses_while_another_run_holds_the_lock(setup):
    drv = _driver()
    with ledger.single_run(setup["tmp"] / "ledger.jsonl"):
        assert drv.run(drv.parse(setup["args"]), compute=guarded_fake_compute([])) == 2
