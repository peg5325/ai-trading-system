"""AI Replay System 테스트: 없는 cycle, 정상 replay, JSON roundtrip,
replay 후 원본 불변, 캔들 복원, GPT 입력 가능, 파이프라인 분리/주문 없음."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from src.journal.models import JournalCycle
from src.journal.sqlite_store import SqliteJournalStore
from src.models import ET, Candle
from src.pipeline.models import ACTION_ORDERED, R_ORDERED, PipelineResult, SymbolOutcome
from src.replay.models import CycleNotFoundError, ReplayContext
from src.replay.system import ReplaySystem
from src.risk.models import PortfolioSnapshot, Position
from src.storage.candle_store import CandleStore

CID = "20260706T150000Z"


def ordered_outcome():
    return SymbolOutcome(
        "SOXL", ACTION_ORDERED, R_ORDERED,
        ai_decision={"decision": "APPROVE_ENTRY", "confidence_score": 84},
        risk_decision={"allowed": True, "reason_codes": [], "risk_flags": ["LEVERAGED_ETF"]},
        order_record={"client_order_id": f"SOXL:{CID}", "status": "FILLED", "filled_quantity": 25},
        indicator_snapshot={"close": 194.8, "ema20": 193.2, "rsi14": 58.0, "vwap": 193.9},
        candidate={"symbol": "SOXL", "score": 82.5, "reasons": ["price above EMA20"]},
        ai_log={"prompt_version": "entry_filter_v1", "model_name": "gpt-5.5-mini",
                "input_hash": "a" * 64, "raw_response": "{...}",
                "parsed_response": {"decision": "APPROVE_ENTRY"}})


def saved_journal(tmp_path, cid=CID):
    st = SqliteJournalStore(tmp_path / "j.sqlite")
    portfolio = PortfolioSnapshot(
        account_value=100_000,
        positions={"SOXL": Position("SOXL", 25, 194.1, 194.8, leverage_factor=3)})
    cyc = JournalCycle.from_pipeline(
        cid, "REGULAR", ["SOXL"], PipelineResult(cid, [ordered_outcome()]),
        portfolio=portfolio, market={"session": "REGULAR", "is_eod_flatten_window": False})
    st.save_cycle(cyc)
    return st


def make_1m(n, start_et=datetime(2026, 7, 6, 10, 0, tzinfo=ET)):
    out, prev = [], 100.0
    for i in range(n):
        m = start_et + timedelta(minutes=i)
        c = 100.0 + (i % 8) * 0.2
        out.append(Candle.from_moment("SOXL", "1m", m, prev, max(prev, c) + 0.1,
                                      min(prev, c) - 0.1, c, 1000, "USD", True))
        prev = c
    return out


# ---- 없는 cycle ----
def test_missing_cycle_raises(tmp_path):
    st = SqliteJournalStore(tmp_path / "j.sqlite")
    with pytest.raises(CycleNotFoundError):
        ReplaySystem(st).replay("NOPE")


# ---- 정상 replay: 모든 복원 대상 포함 ----
def test_normal_replay_reconstructs_everything(tmp_path):
    st = saved_journal(tmp_path)
    ctx = ReplaySystem(st).replay(CID)
    assert ctx.cycle_id == CID and ctx.session == "REGULAR"
    assert ctx.cycle_ts == "2026-07-06T15:00:00Z"
    # market snapshot
    assert ctx.market_snapshot["session"] == "REGULAR"
    assert ctx.market_snapshot["last_prices"]["SOXL"] == 194.8
    # portfolio + position
    assert ctx.portfolio["account_value"] == 100_000
    s = ctx.symbols[0]
    assert s["position"]["quantity"] == 25
    # 복원 대상 전부
    for key in ("candidate", "indicator_snapshot", "ai_decision", "ai_log",
                "risk_decision", "order_record"):
        assert s[key] is not None
    assert ctx.journal is not None          # 원본 journal 포함


# ---- JSON roundtrip ----
def test_json_roundtrip(tmp_path):
    ctx = ReplaySystem(saved_journal(tmp_path)).replay(CID)
    s = json.dumps(ctx.to_dict())
    back = ReplayContext.from_dict(json.loads(s))
    assert back.cycle_id == CID
    assert back.to_dict() == ctx.to_dict()


# ---- replay 후 원본(저널/캔들) 불변 (read-only) ----
def test_replay_does_not_mutate_source(tmp_path):
    st = saved_journal(tmp_path)
    store = CandleStore(tmp_path / "m.sqlite")
    store.upsert_many(make_1m(30))
    before_cycle = st.get_cycle(CID).to_dict()
    before_candles = store.count("SOXL", "1m")

    rp = ReplaySystem(st, candle_store=store)
    ctx = rp.replay(CID)
    ctx.symbols[0]["candidate"]["score"] = 0    # 반환값을 바꿔도 원본엔 영향 없어야
    ctx.to_prompt_dict()

    assert st.get_cycle(CID).to_dict() == before_cycle     # 저널 불변
    assert store.count("SOXL", "1m") == before_candles      # 캔들 불변


# ---- 캔들 복원 (as_of 이하만) ----
def test_candle_reconstruction_read_only(tmp_path):
    st = saved_journal(tmp_path)
    store = CandleStore(tmp_path / "m.sqlite")
    store.upsert_many(make_1m(30))          # 10:00~10:29 ET = 14:00~ UTC
    ctx = ReplaySystem(st, candle_store=store).replay(CID)   # as_of 15:00 UTC
    candles = ctx.symbols[0]["candles"]
    assert candles                          # 복원됨
    from src.replay.system import _parse_ts
    as_of = _parse_ts(ctx.cycle_ts)
    assert all(c["ts_utc"] <= as_of.isoformat() for c in candles)   # look-ahead 없음


# ---- 캔들 스토어 없어도 동작 ----
def test_replay_without_candle_store(tmp_path):
    ctx = ReplaySystem(saved_journal(tmp_path)).replay(CID)
    assert ctx.symbols[0]["candles"] == []
    # 캔들 없어도 indicator snapshot 의 close 로 last_price 복원
    assert ctx.symbols[0]["last_price"] == 194.8


# ---- GPT 입력용 to_prompt_dict (직렬화 가능, 캔들 절단) ----
def test_prompt_dict_is_gpt_ready(tmp_path):
    st = saved_journal(tmp_path)
    store = CandleStore(tmp_path / "m.sqlite")
    store.upsert_many(make_1m(200))
    ctx = ReplaySystem(st, candle_store=store).replay(CID)
    pd = ctx.to_prompt_dict(max_candles=5)
    json.dumps(pd)                          # 직렬화 예외 없음
    assert len(pd["symbols"][0]["candles"]) <= 5
    assert pd["symbols"][0].get("candles_truncated") is True


# ---- 파이프라인 분리 / 주문 기능 없음 ----
def test_replay_has_no_order_capability(tmp_path):
    rp = ReplaySystem(saved_journal(tmp_path))
    assert not hasattr(rp, "submit")
    assert not hasattr(rp, "place_order")
    assert not hasattr(rp, "run_once")
