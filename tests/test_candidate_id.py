"""candidate_id 순수 함수 테스트 (계약서 docs/DESIGN_DELTA_V2_1.md §2-2).

candidate_id 는 모든 진입 후보의 공통 조인키다. 온라인 판단 시점에 결정론적으로
생성되며 매매 행동과 무관하다. 같은 입력 → 같은 id, 입력이 하나라도 다르면 다른 id.
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.pipeline.candidate_id import (
    CANDIDATE_ID_VERSION,
    canonical_candidate_id,
    decision_epoch_ms,
)

_BASE = dict(session_id="006", run_id="006R", cycle_id="20260706T144500Z",
             symbol="SOXL", market="US")


def test_id_is_deterministic_sha256_hex():
    a = canonical_candidate_id(**_BASE)
    b = canonical_candidate_id(**_BASE)
    assert a == b
    assert len(a) == 64 and all(ch in "0123456789abcdef" for ch in a)


def test_id_changes_when_any_input_changes():
    a = canonical_candidate_id(**_BASE)
    for field, val in (("session_id", "007"), ("run_id", "006"),
                       ("cycle_id", "20260706T145000Z"), ("symbol", "TQQQ"),
                       ("market", "KR")):
        assert canonical_candidate_id(**{**_BASE, field: val}) != a, field


def test_id_version_is_bound_into_hash():
    assert canonical_candidate_id(**_BASE) != canonical_candidate_id(**_BASE, id_version="9")
    assert CANDIDATE_ID_VERSION == "1"


def test_symbol_and_market_are_canonicalized():
    a = canonical_candidate_id(session_id="006", run_id="006R",
                               cycle_id="c", symbol="soxl", market="us")
    b = canonical_candidate_id(session_id="006", run_id="006R",
                               cycle_id="c", symbol=" SOXL ", market=" US ")
    assert a == b


def test_none_session_and_run_are_stable():
    # 세션 격리 미설정(테스트/레거시 config) 에서도 결정론적으로 생성돼야 한다.
    a = canonical_candidate_id(session_id=None, run_id=None,
                               cycle_id="c", symbol="SOXL", market="US")
    b = canonical_candidate_id(session_id=None, run_id=None,
                               cycle_id="c", symbol="SOXL", market="US")
    assert a == b and len(a) == 64


def test_decision_epoch_ms_parses_cycle_id():
    expected = int(datetime(2026, 7, 6, 14, 45, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert decision_epoch_ms("20260706T144500Z") == expected


def test_decision_epoch_ms_returns_none_on_bad_input():
    assert decision_epoch_ms("20260706T1445") is None   # 초/Z 없는 축약형
    assert decision_epoch_ms("nonsense") is None
    assert decision_epoch_ms(None) is None


def test_explicit_decision_ms_overrides_cycle_parse():
    a = canonical_candidate_id(**_BASE, decision_ms=123)
    b = canonical_candidate_id(**_BASE, decision_ms=456)
    assert a != b
