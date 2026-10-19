"""Engine reproduction driver for the v1.3 route (run gate + run ledger + research copy), MINIMAL_PATCH_PLAN §4.

  python scripts/eval_repro_v13.py --spec docs/research/RUNSPEC_....md --phase pre --code-root <export of bab3c0d> \
      --copy <copy>.sqlite --mask <v11_mask.json> --events config/daytrade_events_history_v1.yaml \
      --vix-csv .. --vix9d-csv .. --dix-csv .. --ledger <ledger.jsonl> --out-dir <dir>

Order: parse the one `json run-spec` block, verify the research copy against its manifest, measure the mask hash,
check spec + authorization + measured inputs (`src.evalpath.gate.check`). Then, under a single-run lock, expand the
phase's runs into account-call units (run x segment) and refuse the whole plan before any computation if it exceeds
the authorization's remaining budget. Budget unit (closeout 2026-10-19) = one run_account call on real market data;
a strategy over segments A and B is 2 units. The compute process (`eval_repro_v13_compute.py`, PYTHONPATH cleared,
only the copy path) reserves each unit right before its call and refuses unplanned calls. No source DB, no orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.daytrade_v11.data import SEGMENTS
from src.daytrade_v13.policy import COST_CONTRACT_VERSION, COST_MODELS
from src.evalpath import ledger
from src.evalpath.gate import (
    GateRefused,
    check,
    load_authorizations,
    parse_spec,
)
from src.evalpath.research_copy import (
    CopyRefused,
    assert_covered,
    verify_copy,
)

EVALUATION_CONTRACT_VERSION = "v13-engine-repro/v1 (PREREG_DAYTRADE_V1_3_SIZING arms, v1.1 mask 84d22f46)"
AUTHORIZATIONS = ROOT / "config" / "eval_authorizations.json"
COMPUTE = Path(__file__).resolve().parent / "eval_repro_v13_compute.py"


def parse(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--spec", type=Path, required=True)
    p.add_argument("--phase", required=True, help="runs of the spec whose `phase` equals this")
    p.add_argument("--code-root", type=Path, required=True)
    p.add_argument("--code-label", required=True, help="commit the code root was exported from")
    for a in ("--copy", "--mask", "--events", "--vix-csv", "--vix9d-csv", "--dix-csv", "--ledger", "--out-dir"):
        p.add_argument(a, type=Path, required=True)
    p.add_argument("--authorizations", type=Path, default=AUTHORIZATIONS)
    return p.parse_args(argv)


def spawn(cmd: list[str]) -> int:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    return subprocess.run(cmd, env=env, check=False).returncode


def run(args, compute=spawn) -> int:
    try:
        spec = parse_spec(args.spec)
        man = verify_copy(args.copy)
        al = man["allowlist"]
        actual = {"copy_sha256": man["copy_sha256"], "allowlist_sha256": man["allowlist_sha256"],
                  "mask_sha256": hashlib.sha256(args.mask.read_bytes()).hexdigest(),
                  "symbols": al["symbols"], "interval": al["interval"], "windows": al["ranges"]}
        granted = check(spec, load_authorizations(args.authorizations), actual, cost_models=set(COST_MODELS),
                        cost_contract_version=COST_CONTRACT_VERSION,
                        evaluation_contract_version=EVALUATION_CONTRACT_VERSION)
        for seg in SEGMENTS:
            for sym in al["symbols"]:
                assert_covered(man, sym, al["interval"], *seg.bounds())
    except (GateRefused, CopyRefused) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    runs = [r for r in spec["runs"] if r["phase"] == args.phase]
    if not runs:
        print(f"refused: no runs with phase {args.phase!r} in the spec", file=sys.stderr)
        return 2
    keyed = [{**r, "run_key": f"{spec['spec_id']}/{r['run_key']}"} for r in runs]
    units = ledger.expand_plan(keyed, [seg.name for seg in SEGMENTS])
    rc_all = 0
    try:
        with ledger.single_run(args.ledger):
            ledger.preflight(args.ledger, granted["authorization_id"], granted["budget"], units)
            for r, short in zip(keyed, runs):
                out = args.out_dir / f"{short['run_key']}.json"
                full = args.out_dir / f"{short['run_key']}.full.json"
                costs = vars(COST_MODELS[r["cost_model_id"]])
                cmd = [sys.executable, str(COMPUTE), "--code-root", str(args.code_root), "--copy", str(args.copy),
                       "--mask", str(args.mask), "--events", str(args.events), "--vix-csv", str(args.vix_csv),
                       "--vix9d-csv", str(args.vix9d_csv), "--dix-csv", str(args.dix_csv), "--out", str(out),
                       "--full-out", str(full), "--ledger", str(args.ledger), "--run-key", r["run_key"],
                       "--authorization-id", granted["authorization_id"], "--budget", str(granted["budget"]),
                       "--unit-keys", json.dumps([u["unit_key"] for u in units if u["run_key"] == r["run_key"]]),
                       "--policy", r["policy"], "--costs", json.dumps(costs), "--delay", str(r.get("delay", 0))]
                rc = compute(cmd)
                print(f"{r['run_key']}: returncode {rc}, result {out if out.exists() else 'none'}")
                if rc != 0:
                    rc_all = rc
                    break  # a failed or mismatched run stops the phase; nothing is retried automatically
    except (ledger.BudgetExceeded, ledger.RunLocked) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    return rc_all


if __name__ == "__main__":
    sys.exit(run(parse()))
