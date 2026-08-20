"""결과 확인 전에 고정된 박스권 평균회귀 계약 로더."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from src.history.exchange_calendar import trading_days

PREREG_JSON_PATH = "docs/research/PREREG_P0_US30_BOX_RSI_VOLUME_MR_V1.json"
PREREG_MD_PATH = "docs/research/PREREG_P0_US30_BOX_RSI_VOLUME_MR_V1.md"
PREREG_JSON_SHA256 = "9dd6997ae7d4a6054b844b7e429f9091396f731ed29062ff7d0ef0f8c28f314f"
PREREG_MD_SHA256 = "ef074b48cd6e783c54a2e2fbba66bedf195180b3e5fcc3b4d82e08cc754cdbee"
SCHEMA = "p0-us30-box-rsi-volume-mean-reversion-prereg/v1"


class ContractError(RuntimeError):
    pass


def _sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _require(value: Mapping[str, Any], *keys: str) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping) or key not in current:
            raise ContractError(f"계약 필드 누락: {'.'.join(keys)}")
        current = current[key]
    return current


def load_contract() -> dict[str, Any]:
    if _sha256(PREREG_JSON_PATH) != PREREG_JSON_SHA256:
        raise ContractError("사전등록 JSON SHA-256 불일치")
    if _sha256(PREREG_MD_PATH) != PREREG_MD_SHA256:
        raise ContractError("사전등록 Markdown SHA-256 불일치")
    data = json.loads(Path(PREREG_JSON_PATH).read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ContractError("사전등록 schema 불일치")
    if data.get("scope") != "development-only":
        raise ContractError("development-only 계약이 아니다")
    if data.get("discipline", {}).get("blind_oos_opened") is not False:
        raise ContractError("blind OOS 잠금 선언이 없다")
    if int(data.get("discipline", {}).get("primary_specs", 0)) != 1:
        raise ContractError("primary는 정확히 하나여야 한다")
    return data


def development_days(contract: Mapping[str, Any]) -> tuple[date, ...]:
    w = _require(contract, "windows")
    start = date.fromisoformat(str(_require(w, "development_eval_start")))
    end = date.fromisoformat(str(_require(w, "development_eval_end")))
    return tuple(trading_days(start, end))


def fold_boundaries(contract: Mapping[str, Any]) -> tuple[tuple[str, date, date], ...]:
    days = development_days(contract)
    block = int(_require(contract, "windows", "fold_block_trading_days"))
    if block <= 0 or not days:
        raise ContractError("fold block 또는 development days가 유효하지 않다")
    count = len(days) // block
    head = len(days) - count * block
    blocks = [days[:head + block]]
    blocks.extend(
        days[head + block + i * block:head + block + (i + 1) * block]
        for i in range(count - 1)
    )
    return tuple((f"F{i:02d}", b[0], b[-1]) for i, b in enumerate(blocks, 1))
