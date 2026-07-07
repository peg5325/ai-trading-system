"""Trading Journal 테스트: 저장/조회, 주문 없는 사이클, AI WAIT, Risk block,
주문 발생, 에러, symbol/기간 조회, JSON 직렬화. SQLite + InMemory 양쪽."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from src.journal.models import InMemoryJournalStore, JournalCycle
from src.journal.sqlite_store import SqliteJournalStore
from src.pipeline.models import (
    ACTION_ORDERED,
    ACTION_SKIPPED,
    R_AI_WAIT,
    R_NO_CANDIDATE,
    R_ORDERED,
    R_RISK_BLOCK,
    PipelineResult,
    SymbolOutcome,
)


def make_result(cycle_id, outcomes):
    return PipelineResult(cycle_id, outcomes)


def stores(tmp_path):
    return [SqliteJournalStore(tmp_path / "j.sqlite"), InMemoryJournalStore()]


ORDERED_OUT = SymbolOutcome(
    "SOXL", ACTION_ORDERED, R_ORDERED,
    ai_decision={"decision": "APPROVE_ENTRY", "confidence_score": 84},
    risk_decision={"allowed": True, "reason_codes": [], "risk_flags": ["LEVERAGED_ETF"]},
    order_record={"client_order_id": "SOXL:20260706T144500Z", "status": "FILLED"},
    indicator_snapshot={"close": 194.8, "rsi14": 58.0},
    candidate={"symbol": "SOXL", "score": 82.5, "reasons": ["price above EMA20"]},
    ai_log={"prompt_version": "entry_filter_v1", "model_name": "gpt-5.5-mini",
            "input_hash": "a" * 64, "raw_response": "{...}", "parsed_response": {"decision": "APPROVE_ENTRY"}})


# ---- 저장/조회 (주문 발생 + 전체 링크) ----
def test_save_and_get_cycle_links_everything(tmp_path):
    for st in stores(tmp_path):
        cyc = JournalCycle.from_pipeline("20260706T144500Z", "REGULAR", ["SOXL"],
                                         make_result("20260706T144500Z", [ORDERED_OUT]))
        st.save_cycle(cyc)
        got = st.get_cycle("20260706T144500Z")
        assert got is not None
        assert got.order_count == 1
        assert got.cycle_ts == "2026-07-06T14:45:00Z"    # cycle_id → ts 파싱
        o = got.outcomes[0]
        assert o["order_record"]["client_order_id"] == "SOXL:20260706T144500Z"  # OrderRecord 연결
        assert o["ai_log"]["input_hash"] == "a" * 64                             # AI raw/hash 연결
        assert o["candidate"]["score"] == 82.5
        assert o["indicator_snapshot"]["rsi14"] == 58.0


# ---- 주문 없는 사이클도 저장 ----
def test_no_order_cycle_is_saved(tmp_path):
    for st in stores(tmp_path):
        out = SymbolOutcome("SOXL", ACTION_SKIPPED, R_NO_CANDIDATE,
                            indicator_snapshot={"close": 194.0})
        cyc = JournalCycle.from_pipeline("20260706T145000Z", "REGULAR", ["SOXL"],
                                         make_result("20260706T145000Z", [out]))
        st.save_cycle(cyc)
        got = st.get_cycle("20260706T145000Z")
        assert got.order_count == 0
        assert got.outcomes[0]["reason"] == R_NO_CANDIDATE


# ---- AI WAIT 기록 ----
def test_ai_wait_recorded(tmp_path):
    for st in stores(tmp_path):
        out = SymbolOutcome("SOXL", ACTION_SKIPPED, R_AI_WAIT,
                            ai_decision={"decision": "WAIT", "reason": ["AI_TIMEOUT"]},
                            ai_log={"error": "AI_TIMEOUT", "input_hash": "b" * 64})
        st.save_cycle(JournalCycle.from_pipeline("20260706T145500Z", "REGULAR", ["SOXL"],
                                                 make_result("20260706T145500Z", [out])))
        rows = st.get_by_symbol("SOXL")
        assert rows[0]["reason"] == R_AI_WAIT
        assert rows[0]["ai_decision"]["decision"] == "WAIT"


# ---- Risk block: reason_codes / risk_flags 저장 ----
def test_risk_block_reason_codes_saved(tmp_path):
    for st in stores(tmp_path):
        out = SymbolOutcome("SOXL", ACTION_SKIPPED, R_RISK_BLOCK,
                            ai_decision={"decision": "APPROVE_ENTRY"},
                            risk_decision={"allowed": False,
                                           "reason_codes": ["KILL_SWITCH_ACTIVE"],
                                           "risk_flags": ["LEVERAGED_ETF"]})
        st.save_cycle(JournalCycle.from_pipeline("20260706T150000Z", "REGULAR", ["SOXL"],
                                                 make_result("20260706T150000Z", [out])))
        o = st.get_cycle("20260706T150000Z").outcomes[0]
        assert o["risk_decision"]["reason_codes"] == ["KILL_SWITCH_ACTIVE"]
        assert o["risk_decision"]["risk_flags"] == ["LEVERAGED_ETF"]


# ---- 에러 사이클 저장 ----
def test_error_cycle_saved(tmp_path):
    for st in stores(tmp_path):
        st.save_cycle(JournalCycle.error_cycle("20260706T150500Z", ["SOXL"], "boom"))
        got = st.get_cycle("20260706T150500Z")
        assert got.status == "ERROR" and got.error == "boom"


# ---- symbol 별 조회 (여러 사이클) ----
def test_query_by_symbol(tmp_path):
    st = SqliteJournalStore(tmp_path / "j.sqlite")
    for i, cid in enumerate(["20260706T144500Z", "20260706T145000Z"]):
        out = SymbolOutcome("SOXL", ACTION_SKIPPED, R_NO_CANDIDATE)
        st.save_cycle(JournalCycle.from_pipeline(cid, "REGULAR", ["SOXL"], make_result(cid, [out])))
    rows = st.get_by_symbol("SOXL")
    assert len(rows) == 2
    assert [r["cycle_id"] for r in rows] == ["20260706T144500Z", "20260706T145000Z"]


# ---- 기간별 조회 ----
def test_query_by_time_range(tmp_path):
    st = SqliteJournalStore(tmp_path / "j.sqlite")
    for cid in ["20260706T140000Z", "20260706T144500Z", "20260706T160000Z"]:
        st.save_cycle(JournalCycle.from_pipeline(cid, "REGULAR", ["SOXL"],
                                                 make_result(cid, [SymbolOutcome("SOXL", ACTION_SKIPPED, R_NO_CANDIDATE)])))
    got = st.get_by_time_range(datetime(2026, 7, 6, 14, 30), datetime(2026, 7, 6, 15, 0))
    assert [c.cycle_id for c in got] == ["20260706T144500Z"]   # 14:45 만 범위 내


# ---- JSON 직렬화 가능 ----
def test_json_serializable(tmp_path):
    cyc = JournalCycle.from_pipeline("20260706T144500Z", "REGULAR", ["SOXL"],
                                     make_result("20260706T144500Z", [ORDERED_OUT]))
    s = json.dumps(cyc.to_dict())            # 예외 없이 직렬화
    assert JournalCycle.from_dict(json.loads(s)).cycle_id == "20260706T144500Z"


# ---- 멱등 저장(같은 cycle_id 재저장) ----
def test_resave_is_idempotent(tmp_path):
    st = SqliteJournalStore(tmp_path / "j.sqlite")
    cid = "20260706T144500Z"
    st.save_cycle(JournalCycle.from_pipeline(cid, "REGULAR", ["SOXL"],
                                             make_result(cid, [SymbolOutcome("SOXL", ACTION_SKIPPED, R_NO_CANDIDATE)])))
    st.save_cycle(JournalCycle.from_pipeline(cid, "REGULAR", ["SOXL"],
                                             make_result(cid, [SymbolOutcome("SOXL", ACTION_SKIPPED, R_NO_CANDIDATE)])))
    assert len(st.get_by_symbol("SOXL")) == 1     # 중복 outcome 행 없음
