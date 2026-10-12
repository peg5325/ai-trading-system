"""Blind OOS (PREREG §8, §8.1): targets, OOS gate / mask / snapshot preparation, and the one-time run.

Every OOS read goes through `research_load`: purpose="coverage" while preparing (no return computed), and
purpose="performance" only with a verified `OOSExecutionGrant` while running. The small gate / load helpers are
copies of the entry study's script helpers so that they sit inside this study's IMPLEMENTATION_PATHS.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from src.entry_study_v1 import gate as G
from src.entry_study_v1 import mask as M
from src.entry_study_v1 import rules as ERL
from src.entry_study_v1.bars import DataMismatch, StudyRefused
from src.entry_study_v1.cli import check_sha, load_oos_calendar
from src.entry_study_v1.config import ROOT, Cell
from src.entry_study_v1.data import (
    DEV_SEGMENTS,
    OOS_END,
    OOS_EXCLUDED,
    OOS_SEGMENT,
    OOS_START,
    load_symbol,
    merge,
)
from src.entry_study_v1.pipeline import verify_against_manifest
from src.history.oos_unlock import (
    OOSExecutionSpec,
    bind_verified_oos_execution,
    load_verified_oos_unlock,
)
from src.reversal_study_v1 import config as RC
from src.reversal_study_v1 import devrun
from src.reversal_study_v1 import rules as RL
from src.reversal_study_v1.cli import (
    reproduction_record,
    require_inputs,
    write_json_once,
)
from src.reversal_study_v1.pipeline import collect_trades

SEG_B = DEV_SEGMENTS[1]
# Everything that affects signals, costs, bars, calendars or the load path (PREREG §11).
IMPLEMENTATION_PATHS = tuple(sorted({
    *(str(p.relative_to(ROOT)) for p in (ROOT / "src" / "entry_study_v1").glob("*.py")),
    *(str(p.relative_to(ROOT)) for p in (ROOT / "src" / "reversal_study_v1").glob("*.py")),
    "scripts/reversal_study_v1_oos.py", "src/daytrade_paper/rules.py", "src/daytrade_paper/core.py",
    "src/daytrade_paper/store.py", "src/daytrade_paper/external.py", "src/history/exchange_calendar.py",
    "src/history/loader.py", "src/history/oos_guard.py", "src/models.py"}))


@dataclass(frozen=True)
class Target:
    label: str  # "a" | "b"
    cell: Cell
    evaluate: Callable  # (rule, RefDay, params) -> Result

    @property
    def pair(self) -> str:
        return self.cell.pair


def targets(dev_result: dict) -> list[Target]:
    """(a) is always there; (b) is the dev result's single selected cell, if any (§8)."""
    if dev_result.get("k_total") != RC.K_TOTAL:
        raise StudyRefused("dev result is not a reversal-study dev_result (k_total != 16)")
    out = [Target("a", RC.load_designated(), ERL.evaluate)]
    sel = dev_result["selected_for_oos"]
    if len(sel) > 1:
        raise StudyRefused(f"dev result selects {sel}; at most one cell goes to the OOS")
    if sel:
        out.append(Target("b", RC.load_cells()[sel[0]], RL.evaluate))
    return out


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


def lookback(db, symbols, purpose: str, manifest: dict | None) -> dict:
    """Segment B rows (the lookback of the first OOS days), read with the development bound."""
    out = {s: load_symbol(db, s, SEG_B.data_start, SEG_B.data_end, purpose=purpose, dev=True) for s in symbols}
    if manifest is not None:
        verify_against_manifest({SEG_B.name: out}, manifest)
    return out


def oos_rows(db, symbols, purpose: str, strategy_id=None, grant=None) -> dict:
    return {s: load_symbol(db, s, OOS_START, OOS_END, purpose=purpose, dev=False, strategy_id=strategy_id,
                           grant=grant) for s in symbols}


def joined(look: dict, oos: dict) -> dict:
    return {OOS_SEGMENT.name: {s: merge(look[s], oos[s]) for s in oos}}


def pair_syms(pairs: dict, pair: str) -> list[str]:
    return sorted(set(pairs[pair]))


def _load_dev_result(args) -> dict:
    check_sha(args.dev_result, args.dev_result_sha256, "dev_result")
    return json.loads(args.dev_result.read_text(encoding="utf-8"))


def prepare(args) -> int:
    _gate, _mask, manifest, all_pairs = require_inputs(args)
    cal, cal_sha = load_oos_calendar(args.oos_events)  # refuses before any data is read
    tgts = targets(_load_dev_result(args))
    pairs = {p: all_pairs[p] for p in sorted({t.pair for t in tgts})}
    symbols = sorted({s for t in pairs.values() for s in t})
    oos = oos_rows(args.db, symbols, "coverage")
    seg_data = joined(lookback(args.db, symbols, "coverage", manifest), oos)
    mask = M.build_mask(pairs, seg_data, (OOS_SEGMENT,), cal, cal_sha, OOS_EXCLUDED)
    gates = oos_gates(seg_data, pairs)
    snapshot = {"window": [OOS_START.isoformat(), OOS_END.isoformat()],
                "rows": {s: {"sha256": oos[s].row_sha256, "count": oos[s].row_count} for s in symbols}}
    prepared = {"targets": [{"label": t.label, "k": t.cell.k, "strategy_id": t.cell.strategy_id, "pair": t.pair}
                            for t in tgts],
                "gates": gates,
                "target_status": {t.label: ("READY" if gates[t.pair]["status"] == "PASS" else "OOS_INVALID")
                                  for t in tgts},
                "oos_events_sha256": cal_sha, "reproduction": reproduction_record()}
    out = args.out_dir
    if any((out / n).exists() for n in ("oos_mask.json", "oos_snapshot.json", "oos_prepare.json")):
        raise StudyRefused("an OOS output already exists; outputs are write-once")
    print("oos_mask.json sha256", write_json_once(out / "oos_mask.json", mask))
    print("oos_snapshot.json sha256", write_json_once(out / "oos_snapshot.json", snapshot))
    print("oos_prepare.json sha256", write_json_once(out / "oos_prepare.json", prepared))
    return 0


def run(args) -> int:
    _gate, _mask, manifest, all_pairs = require_inputs(args)
    tgts = targets(_load_dev_result(args))
    check_sha(args.prepare, args.prepare_sha256, "oos_prepare")
    check_sha(args.oos_mask, args.oos_mask_sha256, "oos_mask")
    check_sha(args.oos_snapshot, args.oos_snapshot_sha256, "oos_snapshot")
    oos_mask = json.loads(args.oos_mask.read_text(encoding="utf-8"))
    snapshot = json.loads(args.oos_snapshot.read_text(encoding="utf-8"))
    prepared = json.loads(args.prepare.read_text(encoding="utf-8"))
    if [t["label"] for t in prepared["targets"]] != [t.label for t in tgts] or \
            [t["k"] for t in prepared["targets"]] != [t.cell.k for t in tgts]:
        raise DataMismatch("the OOS targets differ from the prepared ones")
    grants = dict(kv.split("=", 1) for kv in args.unlock_artifact)
    cal, cal_sha = load_oos_calendar(args.oos_events)
    if cal_sha != oos_mask["events_calendar_sha256"] or cal_sha != prepared["oos_events_sha256"]:
        raise DataMismatch("OOS event calendar differs from the one the OOS mask was built with")
    results = {}
    for t in tgts:
        cell = t.cell
        if prepared["target_status"][t.label] != "READY":
            results[t.label] = {"verdict": "OOS_INVALID", "why": "OOS gate G1/G3"}
            continue
        if t.label not in grants:
            raise StudyRefused(f"no --unlock-artifact {t.label}=PATH for target {t.label}: the OOS stays sealed")
        unlock = load_verified_oos_unlock(grants[t.label], repo_root=args.repo_root)
        spec = OOSExecutionSpec(cell.strategy_id, RC.PREREG_PATH, "config/reversal_study_v1.json",
                                IMPLEMENTATION_PATHS, args.snapshot_manifest_path, args.snapshot_hash_field)
        check_snapshot_binding(args.oos_snapshot_sha256, unlock)
        grant = bind_verified_oos_execution(unlock, spec, repo_root=args.repo_root)
        pairs = {t.pair: all_pairs[t.pair]}
        symbols = pair_syms(pairs, t.pair)
        oos = oos_rows(args.db, symbols, "performance", strategy_id=cell.strategy_id, grant=grant)
        for s in symbols:
            want = snapshot["rows"][s]
            if oos[s].row_sha256 != want["sha256"] or oos[s].row_count != want["count"]:
                raise DataMismatch(f"{s}: OOS rows differ from the fixed snapshot")
        seg_data = joined(lookback(args.db, symbols, "performance", manifest), oos)
        mask = M.build_mask(pairs, seg_data, (OOS_SEGMENT,), cal, cal_sha, OOS_EXCLUDED)
        if M.canonical_bytes(mask["pairs"][t.pair]) != M.canonical_bytes(oos_mask["pairs"][t.pair]):
            raise DataMismatch("OOS mask rebuilt from the DB differs from the fixed oos_mask.json")
        if oos_gates(seg_data, pairs)[t.pair] != prepared["gates"][t.pair]:
            raise DataMismatch("OOS G1/G3 recomputed from the loaded rows differ from oos_prepare.json")
        ref, lo, sh = pairs[t.pair]
        g4 = {lo: G.g4((OOS_SEGMENT,), seg_data, ref, lo, False, OOS_EXCLUDED),
              sh: G.g4((OOS_SEGMENT,), seg_data, ref, sh, True, OOS_EXCLUDED)}
        if not all(v["pass"] for v in g4.values()):
            results[t.label] = {"verdict": "OOS_INVALID", "why": "OOS gate G4", "g4": g4}
            continue
        trades = collect_trades([cell], t.evaluate, mask["pairs"][t.pair], seg_data, (OOS_SEGMENT,))
        results[t.label] = {**devrun.oos_cell(t.label, cell, trades[cell.k]), "g4": g4}
    out = {"targets": results, "reproduction": {**reproduction_record(), "oos_mask_sha256": args.oos_mask_sha256,
                                                "oos_snapshot_sha256": args.oos_snapshot_sha256}}
    print("oos_result.json sha256", write_json_once(args.out_dir / "oos_result.json", out))
    return 0

