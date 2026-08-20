"""Frozen preregistration loader for box/order-block research."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

STRATEGY_ID = "p0_us30_box_orderblock_retest_v1"
DEFAULT_CONTRACT_PATH = Path(
    "docs/research/PREREG_P0_US30_BOX_ORDERBLOCK_RETEST_V1.json"
)
PREREG_JSON_SHA256 = (
    "e5dd6d2f1fd1fc92260266f2e4b6ff6a399d8e3254360df9e5dddb38f1df415a"
)
PREREG_MD_SHA256 = (
    "4f7e3cd0900a302c76c2358b052557b03c1e6ccf7fc7662e4b904807cf523301"
)


class ContractError(ValueError):
    """Raised when the tracked preregistration no longer matches the runner."""


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def load_contract(path: str | Path = DEFAULT_CONTRACT_PATH) -> dict[str, Any]:
    path = Path(path)
    if path == DEFAULT_CONTRACT_PATH:
        if sha256_file(path) != PREREG_JSON_SHA256:
            raise ContractError("prereg JSON hash mismatch")
        md_path = path.with_suffix(".md")
        if sha256_file(md_path) != PREREG_MD_SHA256:
            raise ContractError("prereg Markdown hash mismatch")
    document = json.loads(path.read_text(encoding="utf-8"))
    _validate(document)
    document["_contract_path"] = path.as_posix()
    document["_contract_sha256"] = sha256_file(path)
    return document


def _validate(document: dict[str, Any]) -> None:
    if document.get("schema") != "p0_us30_box_orderblock_retest_prereg/v1":
        raise ContractError("unexpected prereg schema")
    if document.get("version") != "1.2":
        raise ContractError("unexpected prereg version")
    if document.get("status") != "FROZEN_BEFORE_THIS_STRATEGY_RESULTS":
        raise ContractError("prereg is not frozen before results")
    if document.get("strategy_id") != STRATEGY_ID:
        raise ContractError("strategy id mismatch")
    if document["arms"]["primary"] != "order_block_retest_fixed5":
        raise ContractError("primary arm mismatch")
    data = document["data"]
    if data["development_end_inclusive"] != "2025-07-18":
        raise ContractError("development end mismatch")
    if data["blind_oos_start"] != "2025-07-21" or data["track"] != "swing":
        raise ContractError("swing OOS contract mismatch")
    discipline = document["discipline"]
    if discipline["blind_oos_opened"] or discipline["oos_grant_allowed"]:
        raise ContractError("blind OOS must remain closed")
