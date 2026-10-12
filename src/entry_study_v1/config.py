"""Loader for config/entry_study_v1.json (PREREG §8.1 item 1): cells, rule parameters, holds, ranking rule."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "entry_study_v1.json"
PREREG_PATH = "docs/research/PREREG_DAYTRADE_ENTRY_STUDY_V1.md"
EVENTS_HISTORY = ROOT / "config" / "daytrade_events_history_v1.yaml"

PAIRS = ("P1", "P2", "P3")
RULES = ("E1", "E2", "E3", "E4", "E5")


def hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


@dataclass(frozen=True)
class Cell:
    k: int
    strategy_id: str
    pair: str
    rule: str
    side: str  # "long" | "short" (signal direction; long buys the long ETF, short buys the inverse ETF)
    etf: str
    ref: str
    params: dict
    holds: dict  # {"30m": 30, "60m": 60, "close": "15:55"}
    primary_hold: str


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_config(path: Path = CONFIG_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_cells(path: Path = CONFIG_PATH) -> list[Cell]:
    cells = [Cell(**c) for c in load_config(path)["cells"]]
    if [c.k for c in cells] != list(range(len(cells))):
        raise ValueError("config cells must be indexed 0..n-1 in order")
    return cells


def forced_exit_minute(cell: Cell) -> int:
    return hhmm(cell.holds["close"])
