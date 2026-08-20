"""Frozen contract helpers for the DEMA/Stochastic development test."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from src.history import exchange_calendar as cal

PREREG_JSON = Path("docs/research/PREREG_P0_US30_DEMA_STOCH_MOMENTUM_V1.json")
PREREG_MD = Path("docs/research/PREREG_P0_US30_DEMA_STOCH_MOMENTUM_V1.md")
PREREG_JSON_SHA256 = "ba9fd2c0335eaecca63d3fe69c11efd8c8eba9e9d4795eed5448e0ef61843692"
PREREG_MD_SHA256 = "453bc6f9fcfdd4e7df7c03fc62d44ff31c0690276fd4b67e3921d36a3d3c9379"
EXPECTED_DATA_DAYS = 690
EXPECTED_EVAL_DAYS = 670


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_contract() -> dict[str, Any]:
    if _sha256(PREREG_JSON) != PREREG_JSON_SHA256:
        raise ValueError("DEMA/Stochastic prereg JSON hash mismatch")
    if _sha256(PREREG_MD) != PREREG_MD_SHA256:
        raise ValueError("DEMA/Stochastic prereg Markdown hash mismatch")
    contract = json.loads(PREREG_JSON.read_text(encoding="utf-8"))
    if contract.get("schema") != "p0-us30-dema-stoch-momentum-prereg/v1":
        raise ValueError("unexpected DEMA/Stochastic prereg schema")
    return contract


def _window(contract: Mapping[str, Any], start: str, end: str) -> tuple[date, ...]:
    windows = contract["windows"]
    return tuple(
        cal.trading_days(
            date.fromisoformat(str(windows[start])),
            date.fromisoformat(str(windows[end])),
        )
    )


def development_data_days(contract: Mapping[str, Any]) -> tuple[date, ...]:
    days = _window(contract, "development_data_start", "development_data_end")
    if len(days) != EXPECTED_DATA_DAYS:
        raise ValueError(f"development data day count mismatch: {len(days)}")
    return days


def development_eval_days(contract: Mapping[str, Any]) -> tuple[date, ...]:
    days = _window(contract, "development_eval_start", "development_eval_end")
    if len(days) != EXPECTED_EVAL_DAYS:
        raise ValueError(f"development eval day count mismatch: {len(days)}")
    return days


def fold_boundaries(
    contract: Mapping[str, Any],
) -> tuple[tuple[str, date, date], ...]:
    days = development_eval_days(contract)
    block = int(contract["windows"]["fold_block_trading_days"])
    full_blocks = len(days) // block
    head = len(days) - full_blocks * block
    chunks = [days[: head + block]]
    chunks.extend(
        days[head + block + i * block : head + block + (i + 1) * block]
        for i in range(full_blocks - 1)
    )
    if len(chunks) != 10 or any(not chunk for chunk in chunks):
        raise ValueError("development fold construction mismatch")
    return tuple(
        (f"F{index:02d}", chunk[0], chunk[-1])
        for index, chunk in enumerate(chunks, start=1)
    )
