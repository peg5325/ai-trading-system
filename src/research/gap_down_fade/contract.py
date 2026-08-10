"""결과 전에 고정된 gap-down fade 계약."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from src.history.exchange_calendar import trading_days

PREREG_JSON_PATH = "docs/research/PREREG_P0_US30_GAP_DOWN_FADE_V1.json"
PREREG_MD_PATH = "docs/research/PREREG_P0_US30_GAP_DOWN_FADE_V1.md"
PREREG_JSON_SHA256 = "5ebd7118a82be7ef969e7677aa2f76df1b591c5f898801e932631fee88364494"
PREREG_MD_SHA256 = "3700a6de62ebe9c1361c3f4b644ab312395a29d0b964345f3bdf55e3f3bd7d16"
SCHEMA = "p0-us30-gap-down-fade-prereg/v1"


class ContractError(RuntimeError):
    pass


def _sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_contract() -> dict[str, Any]:
    if _sha256(PREREG_JSON_PATH) != PREREG_JSON_SHA256:
        raise ContractError("사전등록 JSON SHA-256 불일치")
    if _sha256(PREREG_MD_PATH) != PREREG_MD_SHA256:
        raise ContractError("사전등록 Markdown SHA-256 불일치")
    data = json.loads(Path(PREREG_JSON_PATH).read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA or data.get("scope") != "development-only":
        raise ContractError("사전등록 schema/scope 불일치")
    discipline = data.get("discipline", {})
    if discipline.get("blind_oos_opened") is not False:
        raise ContractError("blind OOS 잠금 선언이 없다")
    if int(discipline.get("primary_specs", 0)) != 1:
        raise ContractError("primary는 정확히 하나여야 한다")
    return data


def _window(contract: Mapping[str, Any], key: str) -> date:
    return date.fromisoformat(str(contract["windows"][key]))


def data_days(contract: Mapping[str, Any]) -> tuple[date, ...]:
    return tuple(trading_days(
        _window(contract, "development_data_start"),
        _window(contract, "development_eval_end"),
    ))


def evaluation_days(contract: Mapping[str, Any]) -> tuple[date, ...]:
    return tuple(trading_days(
        _window(contract, "development_eval_start"),
        _window(contract, "development_eval_end"),
    ))


def fold_boundaries(contract: Mapping[str, Any]) -> tuple[tuple[str, date, date], ...]:
    days = evaluation_days(contract)
    block = int(contract["windows"]["fold_block_trading_days"])
    count = len(days) // block
    head = len(days) - count * block
    blocks = [days[:head + block]]
    blocks.extend(
        days[head + block + i * block:head + block + (i + 1) * block]
        for i in range(count - 1)
    )
    return tuple((f"F{i:02d}", b[0], b[-1]) for i, b in enumerate(blocks, 1))
