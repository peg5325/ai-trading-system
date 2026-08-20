"""사전등록 JSON 로드·검증과 결정론적 직렬화."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

STRATEGY_ID = "p0_sector_etf_weekly_momentum_v1"
DEFAULT_CONTRACT_PATH = Path(
    "docs/research/PREREG_P0_SECTOR_ETF_WEEKLY_MOMENTUM_V1.json"
)


class ContractError(ValueError):
    """잠긴 실행 계약과 문서가 다를 때 발생한다."""


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
        document = json.load(handle)
    _validate_contract(document)
    document["_contract_path"] = str(contract_path)
    document["_contract_sha256"] = sha256_file(contract_path)
    return document


def _validate_contract(document: dict[str, Any]) -> None:
    if document.get("schema") != "p0_sector_etf_weekly_momentum_prereg/v1":
        raise ContractError("사전등록 schema 불일치")
    if document.get("status") != "PREREGISTERED_RESULTS_UNSEEN":
        raise ContractError("결과 미열람 사전등록 상태가 아니다")
    if document.get("strategy_id") != STRATEGY_ID:
        raise ContractError("strategy id 불일치")
    data = document.get("data", {})
    if data.get("development_end_inclusive") != "2025-07-18":
        raise ContractError("development 종료일 불일치")
    if data.get("blind_oos_start") != "2025-07-21":
        raise ContractError("blind OOS 시작일 불일치")
    if data.get("track") != "swing":
        raise ContractError("swing OOS track 불일치")
    if sorted(data.get("expected_symbols", [])) != [
        "SMH", "XLE", "XLF", "XLI", "XLK", "XLU", "XLV", "XLY"
    ]:
        raise ContractError("공식 섹터 ETF 유니버스 불일치")
    discipline = document.get("discipline", {})
    if discipline.get("blind_oos_opened") is not False:
        raise ContractError("blind OOS는 닫혀 있어야 한다")
    if discipline.get("oos_grant_allowed") is not False:
        raise ContractError("development 실행은 OOS grant를 받을 수 없다")
