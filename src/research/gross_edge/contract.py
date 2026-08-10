"""Frozen gross-edge screen contract loader (#124)."""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from src.research.orb.contract import ContractIntegrityError

PREREG_MD_PATH = "docs/research/PREREG_P0_US30_GROSS_EDGE_SCREEN_V1.md"
PREREG_JSON_PATH = "docs/research/PREREG_P0_US30_GROSS_EDGE_SCREEN_V1.json"
PREREG_MD_SHA256 = "62efca0e4770e2ef6c1f0eaa916f04918d5a3dbeb22fb6a5863e7ca037409f36"
PREREG_JSON_SHA256 = "784543544a192252b78481d1e2d12e9810c07dc9f61b6ea4352024994f207e1e"


def repo_root() -> str:
    here = os.path.abspath(__file__)
    for _ in range(4):
        here = os.path.dirname(here)
    return here


def _normalized_md(text: str) -> str:
    lines = [
        re.sub(r"[ \t]+", " ", line).rstrip()
        for line in text.replace("\r\n", "\n").split("\n")
    ]
    return "\n".join(lines).strip() + "\n"


def compute_md_sha256(path: str | None = None) -> str:
    target = path or os.path.join(repo_root(), PREREG_MD_PATH)
    with open(target, encoding="utf-8") as stream:
        payload = _normalized_md(stream.read()).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_json_sha256(path: str | None = None) -> str:
    target = path or os.path.join(repo_root(), PREREG_JSON_PATH)
    with open(target, encoding="utf-8") as stream:
        data = json.load(stream)
    payload = json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def load_contract(*, verify: bool = True) -> Mapping[str, Any]:
    if verify:
        md_hash = compute_md_sha256()
        json_hash = compute_json_sha256()
        if md_hash != PREREG_MD_SHA256:
            raise ContractIntegrityError(
                f"gross-edge prereg Markdown hash mismatch: {md_hash}"
            )
        if json_hash != PREREG_JSON_SHA256:
            raise ContractIntegrityError(
                f"gross-edge prereg JSON hash mismatch: {json_hash}"
            )
    with open(os.path.join(repo_root(), PREREG_JSON_PATH), encoding="utf-8") as stream:
        data = json.load(stream)
    candidate_ids = [candidate["id"] for candidate in data.get("candidates", [])]
    if len(candidate_ids) != 8 or len(set(candidate_ids)) != 8:
        raise ContractIntegrityError("gross-edge candidate registry must contain 8 unique ids")
    if data.get("windows", {}).get("blind_oos_access") != "forbidden":
        raise ContractIntegrityError("gross-edge blind OOS policy must remain forbidden")
    return _freeze(data)
