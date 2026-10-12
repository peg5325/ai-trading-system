"""Entry study v1 blind OOS (PREREG_DAYTRADE_ENTRY_STUDY_V1 §8, §8.1).

  prepare  Takes the development result's selected cells (none -> OOS stays sealed), applies the OOS data gate
           (G1, G3) and builds the OOS mask and the snapshot row-hash list. Every OOS read is
           research_load(purpose="coverage"); no return is computed. Writes oos_mask.json, oos_snapshot.json,
           oos_prepare.json (write-once) and prints their SHA-256 for the addendum PR.
  run      Opens the OOS once: per selected cell a verified unlock artifact is loaded and bound to this code
           (OOSExecutionGrant); without it research_load raises OOSLockError. Refuses without
           --i-have-user-approval, if the rows or the OOS mask differ from the prepared ones, or if G4 fails.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.entry_study_v1 import devrun
from src.entry_study_v1 import gate as G
from src.entry_study_v1 import mask as M
from src.entry_study_v1.bars import DataMismatch, StudyRefused
from src.entry_study_v1.cli import (
    add_fixed_args,
    cells,
    check_sha,
    load_oos_calendar,
    pair_symbols,
    reproduction_record,
    require_dev_inputs,
    write_json_once,
)
from src.entry_study_v1.config import PREREG_PATH
from src.entry_study_v1.data import (
    DEV_SEGMENTS,
    OOS_END,
    OOS_EXCLUDED,
    OOS_SEGMENT,
    OOS_START,
    load_symbol,
    merge,
)
from src.entry_study_v1.pipeline import pair_trades, verify_against_manifest
from src.history.oos_unlock import (
    OOSExecutionSpec,
    bind_verified_oos_execution,
    load_verified_oos_unlock,
)

SEG_B = DEV_SEGMENTS[1]
# Everything that affects signals, costs, bars, calendars or the load path.
IMPLEMENTATION_PATHS = tuple(sorted({
    *(str(p.relative_to(ROOT)) for p in (ROOT / "src" / "entry_study_v1").glob("*.py")),
    "scripts/entry_study_v1_oos.py", "src/daytrade_paper/rules.py", "src/daytrade_paper/core.py",
    "src/daytrade_paper/store.py", "src/daytrade_paper/external.py", "src/history/exchange_calendar.py",
    "src/history/loader.py", "src/history/oos_guard.py", "src/models.py"}))


def check_snapshot_binding(snapshot_file_sha256: str, unlock) -> None:
    """The snapshot file in use must be the one the verified unlock artifact names."""
    if snapshot_file_sha256 != unlock.snapshot_hash:
        raise DataMismatch("oos_snapshot.json hash differs from the snapshot hash in the verified unlock artifact")


def oos_gates(seg_data: dict, pairs: dict) -> dict:
    gates = {}
    for p, (ref, lo, sh) in pairs.items():
        syms = (ref, lo, sh)
        gates[p] = {"g1": G.g1(OOS_SEGMENT, seg_data["OOS"], syms, OOS_EXCLUDED),
                    "g3": G.g3(OOS_SEGMENT, seg_data["OOS"], syms, OOS_EXCLUDED)}
        gates[p]["status"] = "PASS" if gates[p]["g1"]["pass"] and gates[p]["g3"]["pass"] else "OOS_INVALID"
    return gates


def _lookback(db, symbols, purpose: str, manifest: dict | None) -> dict:
    """Segment B rows (the lookback of the first OOS days), read with the development bound."""
    out = {s: load_symbol(db, s, SEG_B.data_start, SEG_B.data_end, purpose=purpose, dev=True) for s in symbols}
    if manifest is not None:
        verify_against_manifest({SEG_B.name: out}, manifest)
    return out


def _oos_rows(db, symbols, purpose: str, strategy_id=None, grant=None) -> dict:
    return {s: load_symbol(db, s, OOS_START, OOS_END, purpose=purpose, dev=False, strategy_id=strategy_id,
                           grant=grant) for s in symbols}


def _joined(lookback: dict, oos: dict) -> dict:
    return {OOS_SEGMENT.name: {s: merge(lookback[s], oos[s]) for s in oos}}


def _selected(args):
    result = json.loads(args.dev_result.read_text(encoding="utf-8"))
    check_sha(args.dev_result, args.dev_result_sha256, "dev_result")
    if not result["selected_for_oos"]:
        raise StudyRefused("NO_PASS: no DEV_PASS cell, the OOS stays sealed")
    by_k = {c.k: c for c in cells()}
    return [by_k[k] for k in result["selected_for_oos"]]


def prepare(args) -> int:
    _gate, _mask, manifest = require_dev_inputs(args)
    cal, cal_sha = load_oos_calendar(args.oos_events)  # refuses before any data is read
    sel = _selected(args)
    pairs = pair_symbols({c.pair for c in sel})
    symbols = sorted({s for t in pairs.values() for s in t})
    oos = _oos_rows(args.db, symbols, "coverage")
    seg_data = _joined(_lookback(args.db, symbols, "coverage", manifest), oos)
    mask = M.build_mask(pairs, seg_data, (OOS_SEGMENT,), cal, cal_sha, OOS_EXCLUDED)
    gates = oos_gates(seg_data, pairs)
    snapshot = {"window": [OOS_START.isoformat(), OOS_END.isoformat()],
                "rows": {s: {"sha256": oos[s].row_sha256, "count": oos[s].row_count} for s in symbols}}
    prepared = {"selected": [c.k for c in sel], "gates": gates,
                "cell_status": {str(c.k): ("OOS_INVALID" if gates[c.pair]["status"] != "PASS" else "READY")
                                for c in sel},
                "oos_events_sha256": cal_sha,
                "reproduction": {**reproduction_record(), "gate_sha256": args.gate_sha256,
                                 "mask_sha256": args.mask_sha256, "data_manifest_sha256": args.data_sha256}}
    out = args.out_dir
    if any((out / n).exists() for n in ("oos_mask.json", "oos_snapshot.json", "oos_prepare.json")):
        raise StudyRefused("an OOS output already exists; outputs are write-once")
    print("oos_mask.json sha256", write_json_once(out / "oos_mask.json", mask))
    print("oos_snapshot.json sha256", write_json_once(out / "oos_snapshot.json", snapshot))
    print("oos_prepare.json sha256", write_json_once(out / "oos_prepare.json", prepared))
    return 0


def run(args) -> int:
    _gate, _mask, manifest = require_dev_inputs(args)
    sel = _selected(args)
    check_sha(args.prepare, args.prepare_sha256, "oos_prepare")
    check_sha(args.oos_mask, args.oos_mask_sha256, "oos_mask")
    check_sha(args.oos_snapshot, args.oos_snapshot_sha256, "oos_snapshot")
    oos_mask = json.loads(args.oos_mask.read_text(encoding="utf-8"))
    snapshot = json.loads(args.oos_snapshot.read_text(encoding="utf-8"))
    prepared = json.loads(args.prepare.read_text(encoding="utf-8"))
    grants = dict(kv.split("=", 1) for kv in args.unlock_artifact)
    all_cells = cells()
    cal, cal_sha = load_oos_calendar(args.oos_events)
    if cal_sha != oos_mask["events_calendar_sha256"] or cal_sha != prepared["oos_events_sha256"]:
        raise DataMismatch("OOS event calendar differs from the one the OOS mask was built with")
    results = {}
    for cell in sel:
        if prepared["cell_status"][str(cell.k)] != "READY":
            results[str(cell.k)] = {"verdict": "OOS_INVALID", "why": "OOS gate G1/G3"}
            continue
        if str(cell.k) not in grants:
            raise StudyRefused(f"no --unlock-artifact {cell.k}=PATH for cell {cell.k}: the OOS stays sealed")
        unlock = load_verified_oos_unlock(grants[str(cell.k)], repo_root=args.repo_root)
        spec = OOSExecutionSpec(cell.strategy_id, PREREG_PATH, "config/entry_study_v1.json",
                                IMPLEMENTATION_PATHS, args.snapshot_manifest_path, args.snapshot_hash_field)
        check_snapshot_binding(args.oos_snapshot_sha256, unlock)
        grant = bind_verified_oos_execution(unlock, spec, repo_root=args.repo_root)
        pairs = pair_symbols({cell.pair})
        symbols = sorted(set(pairs[cell.pair]))
        oos = _oos_rows(args.db, symbols, "performance", strategy_id=cell.strategy_id, grant=grant)
        for s in symbols:
            want = snapshot["rows"][s]
            if oos[s].row_sha256 != want["sha256"] or oos[s].row_count != want["count"]:
                raise DataMismatch(f"{s}: OOS rows differ from the fixed snapshot")
        seg_data = _joined(_lookback(args.db, symbols, "performance", manifest), oos)
        mask = M.build_mask(pairs, seg_data, (OOS_SEGMENT,), cal, cal_sha, OOS_EXCLUDED)
        if M.canonical_bytes(mask["pairs"][cell.pair]) != M.canonical_bytes(oos_mask["pairs"][cell.pair]):
            raise DataMismatch("OOS mask rebuilt from the DB differs from the fixed oos_mask.json")
        again = oos_gates(seg_data, pairs)[cell.pair]
        if again != prepared["gates"][cell.pair]:
            raise DataMismatch("OOS G1/G3 recomputed from the loaded rows differ from oos_prepare.json")
        ref, lo, sh = pairs[cell.pair]
        g4 = {lo: G.g4((OOS_SEGMENT,), seg_data, ref, lo, False, OOS_EXCLUDED),
              sh: G.g4((OOS_SEGMENT,), seg_data, ref, sh, True, OOS_EXCLUDED)}
        if not all(v["pass"] for v in g4.values()):
            results[str(cell.k)] = {"verdict": "OOS_INVALID", "why": "OOS gate G4", "g4": g4}
            continue
        trades = pair_trades(cell.pair, all_cells, mask["pairs"][cell.pair], seg_data, (OOS_SEGMENT,))
        results[str(cell.k)] = {**devrun.oos_cell(cell, trades["main"][cell.k]), "g4": g4}
    out = {"cells": results, "reproduction": {**reproduction_record(), "oos_mask_sha256": args.oos_mask_sha256,
                                              "oos_snapshot_sha256": args.oos_snapshot_sha256}}
    print("oos_result.json sha256", write_json_once(args.out_dir / "oos_result.json", out))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("prepare", "run"):
        sp = sub.add_parser(name)
        add_fixed_args(sp, approval=(name == "run"))
        sp.add_argument("--dev-result", type=Path, required=True)
        sp.add_argument("--dev-result-sha256", required=True)
        sp.add_argument("--oos-events", type=Path, default=ROOT / "config" / "daytrade_events_oos_v1.yaml")
    runp = sub.choices["run"]
    runp.add_argument("--prepare", type=Path, required=True, help="oos_prepare.json")
    runp.add_argument("--prepare-sha256", required=True)
    runp.add_argument("--oos-mask", type=Path, required=True)
    runp.add_argument("--oos-mask-sha256", required=True)
    runp.add_argument("--oos-snapshot", type=Path, required=True)
    runp.add_argument("--oos-snapshot-sha256", required=True)
    runp.add_argument("--unlock-artifact", action="append", default=[], metavar="K=PATH")
    runp.add_argument("--snapshot-manifest-path", required=True)
    runp.add_argument("--snapshot-hash-field", required=True)
    runp.add_argument("--repo-root", type=Path, default=ROOT)
    args = ap.parse_args(argv)
    return prepare(args) if args.cmd == "prepare" else run(args)


if __name__ == "__main__":
    raise SystemExit(main())
