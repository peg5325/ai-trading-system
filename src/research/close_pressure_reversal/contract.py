"""추적 사전등록을 실행 가능한 불변 계약으로 읽는다."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


STRATEGY_ID = "p0_us30_close_pressure_reversal_v1"
DEFAULT_CONTRACT_PATH = Path(
    "docs/research/PREREG_P0_US30_CLOSE_PRESSURE_REVERSAL_V1.json"
)


class ContractError(ValueError):
    """사전등록과 실행 계약이 일치하지 않는다."""


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_contract(path: str | Path = DEFAULT_CONTRACT_PATH) -> dict[str, Any]:
    contract_path = Path(path)
    with contract_path.open(encoding="utf-8") as handle:
        doc = json.load(handle)
    _validate_contract(doc)
    doc["_contract_path"] = contract_path.as_posix()
    doc["_contract_sha256"] = sha256_file(contract_path)
    return doc


def _validate_contract(doc: dict[str, Any]) -> None:
    if doc.get("schema") != "p0_us30_close_pressure_reversal_prereg/v1":
        raise ContractError("알 수 없는 prereg schema")
    if doc.get("status") != "PREREGISTERED_RESULTS_UNSEEN":
        raise ContractError("prereg status가 결과 미열람 상태가 아니다")
    if doc.get("strategy_id") != STRATEGY_ID:
        raise ContractError("strategy_id 불일치")
    data = doc.get("data") or {}
    if data.get("track") != "swing" or data.get("interval") != "1d":
        raise ContractError("swing/1d 계약 불일치")
    if data.get("development_end_inclusive") != "2025-07-18":
        raise ContractError("development 종료일 불일치")
    if data.get("blind_oos_start") != "2025-07-21":
        raise ContractError("blind OOS 시작일 불일치")
    if doc.get("discipline", {}).get("blind_oos_opened") is not False:
        raise ContractError("blind OOS는 닫혀 있어야 한다")
    if doc.get("discipline", {}).get("oos_grant_allowed") is not False:
        raise ContractError("development 실행은 OOS grant를 받을 수 없다")
