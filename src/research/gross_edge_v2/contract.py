"""Frozen gross-edge screen v2 contract loader (freeze commit e164d20).

Unlike v1 (normalised-text hashes), the v2 freeze recorded raw file SHA-256 values, so
this loader verifies the exact on-disk bytes of both documents. Any change fails closed.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from src.history.research_snapshot import file_sha256
from src.research.gross_edge.contract import _freeze, repo_root
from src.research.orb.contract import ContractIntegrityError

PREREG_MD_PATH = "docs/research/PREREG_P0_US30_GROSS_EDGE_SCREEN_V2.md"
PREREG_JSON_PATH = "docs/research/PREREG_P0_US30_GROSS_EDGE_SCREEN_V2.json"
PREREG_MD_SHA256 = "5cd63e4ce048a7baae803b075c78bca88b598d6666f65969849432bf76517d48"
PREREG_JSON_SHA256 = "e94024a207ed607062bab21e791012cdc7c0e36f111444babc920f6c44787c4c"
FREEZE_COMMIT = "e164d20"

ARM_IDS = ("A1", "A2", "B1", "B2", "B3", "C1", "C2", "D1")


def compute_md_sha256(path: str | None = None) -> str:
    return file_sha256(path or os.path.join(repo_root(), PREREG_MD_PATH))


def compute_json_sha256(path: str | None = None) -> str:
    return file_sha256(path or os.path.join(repo_root(), PREREG_JSON_PATH))


def load_contract(*, verify: bool = True) -> Mapping[str, Any]:
    if verify:
        md_hash = compute_md_sha256()
        json_hash = compute_json_sha256()
        if md_hash != PREREG_MD_SHA256:
            raise ContractIntegrityError(f"screen v2 prereg Markdown hash mismatch: {md_hash}")
        if json_hash != PREREG_JSON_SHA256:
            raise ContractIntegrityError(f"screen v2 prereg JSON hash mismatch: {json_hash}")
    with open(os.path.join(repo_root(), PREREG_JSON_PATH), encoding="utf-8") as stream:
        data = json.load(stream)
    if data.get("status") != "FROZEN_BEFORE_RESULTS":
        raise ContractIntegrityError("screen v2 prereg must be FROZEN_BEFORE_RESULTS")
    if tuple(arm["id"] for arm in data.get("arms", [])) != ARM_IDS:
        raise ContractIntegrityError("screen v2 arm registry must be exactly A1..D1 (8 arms)")
    windows = data.get("windows", {})
    if windows.get("blind_oos_access") != "forbidden" or windows.get(
        "holdout_h2_access"
    ) != "forbidden":
        raise ContractIntegrityError("screen v2 blind OOS / H2 policy must remain forbidden")
    return _freeze(data)
