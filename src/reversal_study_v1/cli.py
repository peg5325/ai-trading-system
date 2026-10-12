"""CLI helpers: the §5.1 fixed-input checks shared by `precount`, `dev` and `oos`, reproduction record, JSON output."""
from __future__ import annotations

import platform
from pathlib import Path

import numpy as np

from src.daytrade_paper.external import load_events
from src.entry_study_v1.bars import DataMismatch
from src.entry_study_v1.cli import (
    git_state,
    passing_pairs,
    require_dev_inputs,
    write_json_once,
)
from src.entry_study_v1.config import EVENTS_HISTORY, ROOT, sha256_file
from src.reversal_study_v1 import config as RC
from src.reversal_study_v1.pipeline import check_rebuilt_mask, verified_dev_data

OUT_DIR = ROOT / "reports" / "reversal_study_v1"

__all__ = ["OUT_DIR", "add_fixed_args", "prepare_dev", "reproduction_record", "require_inputs", "write_json_once"]


def add_fixed_args(ap, *, approval: bool) -> None:
    """The three fixed SHA-256 values are not flags: they come from config/reversal_study_v1.json (PREREG §5.1)."""
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--gate", type=Path, required=True, help="entry study gate.json")
    ap.add_argument("--mask", type=Path, required=True, help="entry study mask.json")
    ap.add_argument("--manifest", type=Path, required=True, help="entry study data_manifest.json")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--allow-dirty", action="store_true")
    if approval:
        ap.add_argument("--i-have-user-approval", action="store_true")


def reproduction_record() -> dict:
    return {"git": git_state(), "config_sha256": sha256_file(RC.CONFIG_PATH),
            "frozen_inputs": RC.frozen_inputs(), "events_calendar_sha256": sha256_file(EVENTS_HISTORY),
            "python": platform.python_version(), "numpy": np.__version__, "base_seed": RC.cfg()["seed"]["base"]}


def require_inputs(args) -> tuple[dict, dict, dict, dict]:
    """§5.1 (1)-(3): approval flag (dev, oos), file SHA-256 = the config's fixed values, mask linkage, events
    calendar, clean tree; then the gate/mask pairs must be exactly the config's pairs. Returns
    (gate, mask, manifest, {pair: (ref, long, short)})."""
    fz = RC.frozen_inputs()
    args.gate_sha256 = fz["gate_sha256"]
    args.mask_sha256 = fz["mask_sha256"]
    args.data_sha256 = fz["data_manifest_sha256"]
    gate, mask, manifest = require_dev_inputs(args)
    pairs = passing_pairs(gate, mask)
    if pairs != RC.pair_map():
        raise DataMismatch(f"gate/mask pairs {pairs} differ from the config pairs {RC.pair_map()}")
    return gate, mask, manifest, pairs


def prepare_dev(args, *, purpose: str) -> tuple[dict, dict, dict, dict, dict]:
    """§5.1 (1)-(5): fixed inputs, DB rows of the six symbols against the manifest, P1/P2 mask rebuilt from the
    verified rows. Refuses before any signal or return exists. Returns (gate, mask, manifest, pairs, seg_data)."""
    gate, mask, manifest, pairs = require_inputs(args)
    symbols = sorted({s for t in pairs.values() for s in t})
    seg_data = verified_dev_data(args.db, symbols, manifest, purpose=purpose)
    check_rebuilt_mask(mask, pairs, seg_data, load_events(EVENTS_HISTORY), sha256_file(EVENTS_HISTORY))
    return gate, mask, manifest, pairs, seg_data
