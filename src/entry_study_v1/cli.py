"""Shared CLI helpers: fixed-input checks, reproduction record, write-once JSON output."""
from __future__ import annotations

import json
import platform
import subprocess
from pathlib import Path

import numpy as np

from src.entry_study_v1.bars import DataMismatch, StudyRefused
from src.entry_study_v1.config import (
    CONFIG_PATH,
    EVENTS_HISTORY,
    ROOT,
    load_cells,
    sha256_file,
)
from src.entry_study_v1.mask import canonical_bytes, write_once

OUT_DIR = ROOT / "reports" / "entry_study_v1"


def git_state() -> dict:
    def run(*a: str) -> str | None:
        try:
            return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    status = run("status", "--porcelain", "--untracked-files=no")
    return {"commit": run("rev-parse", "HEAD"), "dirty_tracked": None if status is None else bool(status)}


def reproduction_record() -> dict:
    return {"git": git_state(), "config_sha256": sha256_file(CONFIG_PATH),
            "events_calendar_sha256": sha256_file(EVENTS_HISTORY), "python": platform.python_version(),
            "numpy": np.__version__, "base_seed": 20261012}


def check_sha(path: Path, expected: str, what: str) -> None:
    got = sha256_file(path)
    if got != expected:
        raise DataMismatch(f"{what} {path} hashes to {got}, expected {expected}")


def add_fixed_args(ap, *, approval: bool) -> None:
    """The fixed-input flags of `precount`, `dev` and `oos` (PREREG §11)."""
    ap.add_argument("--db", type=Path, required=True)
    ap.add_argument("--gate", type=Path, required=True)
    ap.add_argument("--gate-sha256", required=True)
    ap.add_argument("--mask", type=Path, required=True)
    ap.add_argument("--mask-sha256", required=True)
    ap.add_argument("--manifest", type=Path, required=True, help="data_manifest.json")
    ap.add_argument("--data-sha256", required=True, help="SHA-256 of the data_manifest file")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--allow-dirty", action="store_true")
    if approval:
        ap.add_argument("--i-have-user-approval", action="store_true")


def require_dev_inputs(args) -> tuple[dict, dict, dict]:
    """The checks shared by `dev`, `oos` and `precount`: approval flag (not for precount), file hashes, events
    calendar, clean tree. Returns (gate, mask, manifest) parsed."""
    if hasattr(args, "i_have_user_approval") and not args.i_have_user_approval:
        raise StudyRefused("refusing: --i-have-user-approval is required")
    check_sha(args.gate, args.gate_sha256, "gate")
    check_sha(args.mask, args.mask_sha256, "mask")
    check_sha(args.manifest, args.data_sha256, "data_manifest")
    gate = json.loads(args.gate.read_text(encoding="utf-8"))
    mask = json.loads(args.mask.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if mask["events_calendar_sha256"] != sha256_file(EVENTS_HISTORY):
        raise DataMismatch("event calendar differs from the one the mask was built with")
    if git_state()["dirty_tracked"] and not args.allow_dirty:
        raise StudyRefused("tracked files are modified; commit first or pass --allow-dirty")
    return gate, mask, manifest


def passing_pairs(gate: dict, mask: dict) -> dict:
    """{pair: (ref, long, short)} for pairs with gate status PASS; the mask must hold exactly those."""
    live = {p: g for p, g in gate["pairs"].items() if g["status"] == "PASS"}
    if set(live) != set(mask["pairs"]):
        raise DataMismatch(f"mask pairs {sorted(mask['pairs'])} != gate PASS pairs {sorted(live)}")
    return {p: (m["ref"], m["long"], m["short"]) for p, m in mask["pairs"].items()}


def cells():
    return load_cells()


def pair_symbols(only=None) -> dict:
    """{pair: (ref, long ETF, short ETF)} from the config cells (optionally only the given pairs)."""
    out: dict = {}
    for c in load_cells():
        if only is not None and c.pair not in only:
            continue
        ref, lo, sh = out.get(c.pair, (c.ref, None, None))
        out[c.pair] = (ref, c.etf if c.side == "long" else lo, c.etf if c.side == "short" else sh)
    return out


def write_json_once(path: Path, obj) -> str:
    return write_once(path, canonical_bytes(obj))
