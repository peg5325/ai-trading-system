"""Loader for config/reversal_study_v1.json (PREREG §8.1 item 1): cells, OOS cell (a), costs, seeds, fixed hashes."""
from __future__ import annotations

from pathlib import Path

from src.entry_study_v1.config import ROOT, Cell, load_config
from src.entry_study_v1.config import load_cells as _load_cells
from src.entry_study_v1.trades import CostSet

CONFIG_PATH = ROOT / "config" / "reversal_study_v1.json"
PREREG_PATH = "docs/research/PREREG_DAYTRADE_REVERSAL_STUDY_V1.md"
RULES = ("T1", "T2", "T3", "T4")
K_TOTAL = 16
BASE, STRESS, SENS = "base_0.20", "stress_0.30", "sens_0.10"


def cfg(path: Path = CONFIG_PATH) -> dict:
    return load_config(path)


def load_cells(path: Path = CONFIG_PATH) -> list[Cell]:
    return _load_cells(path)


def load_designated(path: Path = CONFIG_PATH) -> Cell:
    """OOS cell (a): the entry study's k = 2 cell, copied from the entry config, as an entry-study Cell."""
    d = cfg(path)["oos_designated"]
    return Cell(k=d["k"], strategy_id=d["strategy_id"], pair=d["pair"], rule=d["rule"], side=d["side"],
                etf=d["etf"], ref=d["ref"], params=d["params"], holds=d["holds"], primary_hold=d["primary_hold"])


def load_costs(path: Path = CONFIG_PATH) -> dict[str, CostSet]:
    return {name: CostSet(name, v["c"], v["s"], v["f"]) for name, v in cfg(path)["costs"].items()}


def pair_map(path: Path = CONFIG_PATH) -> dict[str, tuple[str, str, str]]:
    """{pair: (ref, long ETF, short ETF)}."""
    return {p: (v["ref"], v["long"], v["short"]) for p, v in cfg(path)["pairs"].items()}


def frozen_inputs(path: Path = CONFIG_PATH) -> dict:
    return cfg(path)["frozen_inputs"]


def seed(variant: int, k: int, path: Path = CONFIG_PATH) -> int:
    """PREREG §7.2: 20261013 + 1000 * variant + k, handed to the bootstrap directly."""
    s = cfg(path)["seed"]
    return s["base"] + s["variant_step"] * variant + k


def variant_id(name: str, path: Path = CONFIG_PATH) -> int:
    return cfg(path)["seed"]["variants"][name]


COSTS = load_costs()
