"""결과 전에 고정한 SPY 장중 모멘텀 계약을 fail-closed로 읽는다."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from src.history.exchange_calendar import trading_days

PREREG_JSON_PATH = "docs/research/PREREG_SPY_INTRADAY_MOMENTUM_V1.json"
PREREG_MD_PATH = "docs/research/PREREG_SPY_INTRADAY_MOMENTUM_V1.md"
PREREG_JSON_SHA256 = "36a5061c466a5e1a3dc25105f9aba6e77912edfa9f89c8e4cba521f0facbc5cf"
PREREG_MD_SHA256 = "b6d327eaaa57755b72760024358bec19fb64c8f46342b7c0420dcfd805e80a9d"
SCHEMA = "spy-intraday-momentum-prereg/v1"


class ContractError(RuntimeError):
    """사전등록 파일이나 필수 불변식이 달라졌을 때 중단한다."""


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
    if discipline.get("results_examined") is not None:
        # results_examined는 최상위 고정값이다. 중복 필드가 생기면 계약 혼동으로 거부한다.
        raise ContractError("discipline.results_examined 중복 선언")
    if data.get("results_examined") is not False:
        raise ContractError("결과 미열람 선언이 없다")
    if discipline.get("blind_oos_opened") is not False:
        raise ContractError("blind OOS 잠금 선언이 없다")
    if int(discipline.get("primary_specs", 0)) != 1:
        raise ContractError("primary는 정확히 하나여야 한다")
    return data


def _window(contract: Mapping[str, Any], key: str) -> date:
    return date.fromisoformat(str(contract["windows"][key]))


def evaluation_days(contract: Mapping[str, Any]) -> tuple[date, ...]:
    days = tuple(trading_days(
        _window(contract, "development_eval_start"),
        _window(contract, "development_eval_end"),
    ))
    if days and days[-1] >= _window(contract, "blind_oos_start"):
        raise ContractError("development 평가창이 blind OOS와 겹친다")
    return days


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
    result = tuple((f"F{i:02d}", values[0], values[-1])
                   for i, values in enumerate(blocks, 1))
    expected = int(contract["windows"]["fold_count"])
    if len(result) != expected:
        raise ContractError(f"fold 개수 불일치: {len(result)} != {expected}")
    return result
