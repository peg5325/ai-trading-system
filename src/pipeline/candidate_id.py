"""candidate_id — 모든 진입 후보의 공통 조인키 (계약서 §2-2).

미체결 후보는 `trade_id` 가 없어 `cycle_id+symbol+trade_id` 로는 후보와 사후 결과를
이을 수 없다. 그래서 온라인 판단 시점에 항상 존재하는 canonical `candidate_id` 를 만든다.
매매 행동과 무관한 순수 식별자 — 어떤 결정 입력에도 되먹임되지 않는다.

결정론: 입력 문자열/해시 알고리즘/UTC epoch ms/market·symbol canonicalization/id_version
을 고정한다. 같은 입력 → 같은 candidate_id (now()/난수 미사용).
`src/runtime/isolation/identity.py` 의 sha256-over-sorted-dict 관용구를 재사용한다.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

CANDIDATE_ID_VERSION = "1"      # candidate_id 산출 규약 버전 (해시 입력에 포함)

# cycle_id 포맷(정본): 'YYYYMMDDTHHMMSSZ' (paper_loop 가 생성).
_CYCLE_FMT = "%Y%m%dT%H%M%SZ"


def decision_epoch_ms(cycle_id: str | None) -> int | None:
    """cycle_id('YYYYMMDDTHHMMSSZ') → UTC epoch milliseconds. 파싱 실패 시 None."""
    if not cycle_id:
        return None
    try:
        dt = datetime.strptime(cycle_id, _CYCLE_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return int(dt.timestamp() * 1000)


def _canon_symbol(symbol) -> str:
    return str(symbol or "").strip().upper()


def _canon_market(market) -> str:
    return str(market or "").strip().upper()


def canonical_candidate_id(*, session_id, run_id, cycle_id, symbol, market,
                           decision_ms: int | None = None,
                           id_version: str = CANDIDATE_ID_VERSION) -> str:
    """진입 후보의 canonical sha256 식별자(hex).

    decision_ms 미지정 시 cycle_id 에서 유도(파싱 실패=None). session/run 미설정(None)도
    허용하며 빈 문자열로 canonicalize 해 결정론을 유지한다.
    """
    if decision_ms is None:
        decision_ms = decision_epoch_ms(cycle_id)
    payload = {
        "id_version": str(id_version),
        "session_id": session_id or "",
        "run_id": run_id or "",
        "cycle_id": cycle_id or "",
        "decision_ms": decision_ms,
        "symbol": _canon_symbol(symbol),
        "market": _canon_market(market),
    }
    s = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()
