"""AI Learning Dataset Builder 테스트.

합성 Journal + Candle 로 forward return / MFE / MAE / win-loss-neutral / exporter / 결정론 검증.
실데이터/네트워크 불필요.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.dataset.builder import DatasetBuilder, LabelConfig
from src.dataset.exporters import JsonlExporter, ParquetExporter
from src.dataset.models import DATASET_VERSION, FEATURE_VERSION
from src.journal.models import InMemoryJournalStore, JournalCycle
from src.models import Candle
from src.storage.candle_store import CandleStore

CTS = datetime(2026, 7, 7, 4, 15, 0, tzinfo=timezone.utc)
CYCLE_ID = "20260707T041500Z"
CYCLE_TS = "2026-07-07T04:15:00Z"
ENTRY = 100.0


def _candle(sym, minute_offset, close, currency="USD"):
    moment = CTS + timedelta(minutes=minute_offset)
    return Candle.from_moment(sym, "1m", moment,
                              o=close, h=close + 0.5, low=close - 0.5, c=close,
                              v=1000.0, currency=currency, adjusted=False)


def _fill_candles(store, sym, n=40, slope=0.1, currency="USD"):
    """offset 0..n 캔들. close = ENTRY + offset*slope. offset 0 은 look-ahead 제외 검증용."""
    cs = [_candle(sym, off, ENTRY + off * slope, currency) for off in range(0, n + 1)]
    store.upsert_many(cs)


def _outcome(sym, *, action="ORDERED", reason="ORDERED", close=ENTRY, order=True):
    o = {
        "symbol": sym, "action": action, "reason": reason,
        "indicator_snapshot": {"close": close, "ema7": 99.0, "ema20": 98.0,
                               "rsi14": 55.0, "vwap": 99.5, "bb_upper": 102.0,
                               "bb_lower": 96.0, "stochrsi_k": 0.6, "stochrsi_d": 0.5},
        "candidate": {"symbol": sym, "score": 72.5, "rank": 1,
                      "candidate_type": "LONG_SETUP", "reasons": ["uptrend"],
                      "risk_flags": ["LEVERAGED_ETF"]},
        "ai_decision": {"decision": "APPROVE_ENTRY", "confidence_score": 70,
                        "entry_quality": "GOOD", "risk_level": "HIGH",
                        "reason": ["ok"], "warnings": [],
                        "model_name": "gpt-test", "prompt_version": "entry-v1"},
        "risk_decision": {"allowed": True, "adjusted_quantity": 10},
        "ai_log": {"model_name": "gpt-test", "prompt_version": "entry-v1"},
    }
    if order:
        o["order_record"] = {"side": "BUY", "kind": "NEW_ENTRY", "status": "FILLED",
                             "filled_quantity": 10.0, "avg_fill_price": 100.2, "fee": 0.5}
    return o


def _cycle(outcomes, cycle_id=CYCLE_ID, cts=CYCLE_TS, session="OVERNIGHT"):
    return JournalCycle(
        cycle_id=cycle_id, cycle_ts=cts, session=session,
        universe=[o["symbol"] for o in outcomes], status="RAN", reason="OK", error=None,
        outcomes=outcomes, order_count=sum(1 for o in outcomes if o.get("order_record")),
        portfolio={"account_value": 100000.0, "positions": {"SOXL": {"qty": 5}},
                   "kill_switch": False},
        market={"market": "US"})


def _store(tmp_path):
    return CandleStore(tmp_path / "candle.sqlite")


# ---- forward label 정확성 ----
def test_forward_metrics_win(tmp_path):
    store = _store(tmp_path)
    _fill_candles(store, "SOXL", n=40, slope=0.1)   # +30m close = 103.0
    journal = InMemoryJournalStore()
    journal.save_cycle(_cycle([_outcome("SOXL")]))

    ds = DatasetBuilder(journal, store, config=LabelConfig()).build()
    assert len(ds) == 1
    rec = ds.records[0]
    fmap = rec.label.forward_map()

    f30 = fmap[30]
    assert f30.n_bars == 30                                  # offset 1..30 (0 은 제외)
    assert abs(f30.forward_return - 0.03) < 1e-9             # (103-100)/100
    assert abs(f30.end_price - 103.0) < 1e-9
    assert abs(f30.mfe - 0.035) < 1e-9                       # high@30 = 103.5
    assert abs(f30.mae - (-0.004)) < 1e-9                    # low@1 = 99.6
    assert rec.label.label_class == "WIN"
    assert rec.label.label_window_min == 30
    assert rec.label.complete is True
    assert rec.label.holding_minutes == 60                   # 최대 창까지 데이터 존재


def test_lookahead_excludes_entry_bar(tmp_path):
    # offset 0(진입봉) 을 극단값으로 넣어도 forward 에 절대 포함되면 안 됨
    store = _store(tmp_path)
    cs = [_candle("SOXL", 0, 999.0)] + [_candle("SOXL", off, ENTRY + off * 0.1)
                                        for off in range(1, 41)]
    store.upsert_many(cs)
    journal = InMemoryJournalStore()
    journal.save_cycle(_cycle([_outcome("SOXL")]))
    ds = DatasetBuilder(journal, store, config=LabelConfig()).build()
    f5 = ds.records[0].label.forward_map()[5]
    assert f5.mfe < 0.1                                      # 999 가 새면 mfe 폭발


def test_label_neutral_and_loss(tmp_path):
    store = _store(tmp_path)
    _fill_candles(store, "FLAT", n=40, slope=0.0)           # 변화 없음 → NEUTRAL
    _fill_candles(store, "DOWN", n=40, slope=-0.1)          # 하락 → LOSS
    journal = InMemoryJournalStore()
    journal.save_cycle(_cycle([_outcome("FLAT"), _outcome("DOWN")]))
    ds = DatasetBuilder(journal, store, config=LabelConfig()).build()
    by = {r.feature.symbol: r for r in ds.records}
    assert by["FLAT"].label.label_class == "NEUTRAL"
    assert by["DOWN"].label.label_class == "LOSS"
    assert by["DOWN"].label.label_return < 0


def test_incomplete_label_when_no_forward(tmp_path):
    store = _store(tmp_path)
    # 진입 이후 캔들 없음 → primary 창 미충족
    store.upsert_many([_candle("SOXL", 0, ENTRY)])
    journal = InMemoryJournalStore()
    journal.save_cycle(_cycle([_outcome("SOXL")]))
    ds = DatasetBuilder(journal, store, config=LabelConfig()).build()
    rec = ds.records[0]
    assert rec.label.complete is False
    assert rec.label.label_class is None
    # drop 옵션이면 제외
    ds2 = DatasetBuilder(journal, store,
                         config=LabelConfig(drop_incomplete_labels=True)).build()
    assert len(ds2) == 0


# ---- feature / meta 추출 ----
def test_feature_and_meta_fields(tmp_path):
    store = _store(tmp_path)
    _fill_candles(store, "SOXL", n=40)
    journal = InMemoryJournalStore()
    journal.save_cycle(_cycle([_outcome("SOXL")]))
    ds = DatasetBuilder(journal, store, strategy_version="screener-9.9").build()
    rec = ds.records[0]
    f, lab, m = rec.feature, rec.label, rec.meta
    assert f.symbol == "SOXL" and f.market == "US" and f.session == "OVERNIGHT"
    assert f.ai_decision == "APPROVE_ENTRY" and f.ai_confidence == 70
    assert f.candidate_score == 72.5 and "LEVERAGED_ETF" in f.risk_flags
    assert f.risk_allowed is True and f.account_value == 100000.0 and f.open_positions == 1
    # 재현성 버전 필수
    assert m.feature_version == FEATURE_VERSION and m.dataset_version == DATASET_VERSION
    assert m.prompt_version == "entry-v1" and m.model_name == "gpt-test"
    assert m.strategy_version == "screener-9.9"
    # 실제 체결 passthrough
    assert lab.trade_taken is True and lab.entry_side == "BUY"
    assert lab.entry_fill_price == 100.2 and lab.entry_qty == 10.0


def test_exit_and_nodata_outcomes_excluded(tmp_path):
    store = _store(tmp_path)
    _fill_candles(store, "SOXL", n=40)
    journal = InMemoryJournalStore()
    # 청산 outcome(지표/후보/AI 없음) + NO_DATA outcome → 관찰 아님
    exit_o = {"symbol": "SOXL", "action": "ORDERED", "reason": "EXIT_ORDERED",
              "risk_decision": {"allowed": True}, "order_record": {"side": "SELL",
              "status": "FILLED", "filled_quantity": 5.0}}
    nodata = {"symbol": "QQQ", "action": "SKIPPED", "reason": "NO_DATA"}
    journal.save_cycle(_cycle([exit_o, nodata, _outcome("SOXL")]))
    ds = DatasetBuilder(journal, store).build()
    assert len(ds) == 1 and ds.records[0].feature.reason == "ORDERED"


def test_symbol_filter_and_determinism(tmp_path):
    store = _store(tmp_path)
    _fill_candles(store, "SOXL", n=40)
    _fill_candles(store, "QQQ", n=40)
    journal = InMemoryJournalStore()
    journal.save_cycle(_cycle([_outcome("SOXL"), _outcome("QQQ")]))
    b = DatasetBuilder(journal, store)
    ds = b.build(symbols={"SOXL"})
    assert {r.feature.symbol for r in ds.records} == {"SOXL"}
    # 결정론: 두 번 빌드 결과 동일
    a1 = DatasetBuilder(journal, store, built_at="2026-07-07T05:00:00Z").build()
    a2 = DatasetBuilder(journal, store, built_at="2026-07-07T05:00:00Z").build()
    assert a1.to_dicts() == a2.to_dicts()


# ---- exporters ----
def test_jsonl_exporter(tmp_path):
    store = _store(tmp_path)
    _fill_candles(store, "SOXL", n=40)
    journal = InMemoryJournalStore()
    journal.save_cycle(_cycle([_outcome("SOXL")]))
    ds = DatasetBuilder(journal, store).build()
    p = tmp_path / "out" / "ds.jsonl"
    n = JsonlExporter().export(ds, p)
    assert n == 1
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2                                  # meta 헤더 + 1 record
    meta = json.loads(lines[0])
    assert meta["__meta__"]["n_records"] == 1
    row = json.loads(lines[1])
    assert set(row.keys()) == {"feature", "label", "meta"}
    assert row["feature"]["symbol"] == "SOXL"
    assert row["label"]["label_class"] == "WIN"


# ---- v2.1 계측(PR 2-A): candidate_id / trade_id / feature 전파 ----
def test_dataset_propagates_candidate_and_trade_id(tmp_path):
    store = _store(tmp_path)
    _fill_candles(store, "SOXL", n=40)
    journal = InMemoryJournalStore()
    o = _outcome("SOXL")
    o["candidate_id"] = "abc123"
    o["trade_id"] = "paper-SOXL:c1"
    o["candidate"]["features"] = {
        "weighted_component_sum": 80.0, "penalty_score": 5.0, "pre_clamp_score": 75.0,
        "candidate_score": 72.5, "volume_last": 2000.0, "volume_sma20": 1050.0,
        "volume_ratio": 1.9, "dollar_volume": 202000.0,
    }
    journal.save_cycle(_cycle([o]))
    ds = DatasetBuilder(journal, store).build()
    f = ds.records[0].feature
    assert f.candidate_id == "abc123" and f.trade_id == "paper-SOXL:c1"
    assert f.weighted_component_sum == 80.0 and f.penalty_score == 5.0
    assert f.pre_clamp_score == 75.0
    assert f.volume == 2000.0 and f.volume_sma20 == 1050.0
    assert f.volume_ratio == 1.9 and f.dollar_volume == 202000.0
    flat = ds.records[0].to_flat()
    assert flat["candidate_id"] == "abc123" and flat["trade_id"] == "paper-SOXL:c1"
    assert flat["volume"] == 2000.0 and flat["dollar_volume"] == 202000.0


def test_dataset_handles_missing_candidate_id(tmp_path):
    """레거시 outcome(candidate_id/trade_id 없음) → None 으로 안전 처리."""
    store = _store(tmp_path)
    _fill_candles(store, "SOXL", n=40)
    journal = InMemoryJournalStore()
    journal.save_cycle(_cycle([_outcome("SOXL")]))
    f = DatasetBuilder(journal, store).build().records[0].feature
    assert f.candidate_id is None and f.trade_id is None
    assert f.volume is None and f.dollar_volume is None


def test_dataset_schema_version_bumped():
    assert FEATURE_VERSION != "1.0" and DATASET_VERSION != "1.0"


# ---- v2.1 PR 2-B: benchmark market context 전파 ----
_BENCH = {
    "benchmark_symbol": "QQQ", "benchmark_close": 400.0, "benchmark_ema20": 398.0,
    "benchmark_ema_period": 20,
    "benchmark_vs_ema20": 0.005, "benchmark_ema20_slope": 0.001,
    "benchmark_return_trailing_30m": 0.002, "benchmark_return_trailing_60m": 0.004,
    "benchmark_trend": "UP", "benchmark_source_bar_end": "2026-07-07T04:15:00Z",
    "benchmark_context_asof": "2026-07-07T04:15:00Z", "benchmark_timeframe": "5m",
    "market_context_version": "1.0",
}


def test_dataset_propagates_benchmark_context(tmp_path):
    store = _store(tmp_path)
    _fill_candles(store, "SOXL", n=40)
    journal = InMemoryJournalStore()
    cyc = _cycle([_outcome("SOXL")])
    cyc.benchmark_context = dict(_BENCH)
    journal.save_cycle(cyc)
    rec = DatasetBuilder(journal, store).build().records[0]
    f = rec.feature
    assert f.benchmark_symbol == "QQQ" and f.benchmark_close == 400.0
    assert f.benchmark_ema20 == 398.0 and f.benchmark_ema_period == 20 and f.benchmark_trend == "UP"
    assert f.benchmark_return_trailing_30m == 0.002 and f.benchmark_return_trailing_60m == 0.004
    assert f.market_context_version == "1.0"
    flat = rec.to_flat()
    assert flat["benchmark_symbol"] == "QQQ" and flat["benchmark_vs_ema20"] == 0.005


def test_dataset_benchmark_context_null_when_absent(tmp_path):
    store = _store(tmp_path)
    _fill_candles(store, "SOXL", n=40)
    journal = InMemoryJournalStore()
    journal.save_cycle(_cycle([_outcome("SOXL")]))     # benchmark_context 없음
    f = DatasetBuilder(journal, store).build().records[0].feature
    assert f.benchmark_symbol is None and f.benchmark_close is None
    assert f.market_context_version is None


def test_parquet_exporter_roundtrip(tmp_path):
    import importlib.util
    if importlib.util.find_spec("pyarrow") is None:      # pragma: no cover
        import pytest
        pytest.skip("pyarrow 미설치")
    import pandas as pd

    store = _store(tmp_path)
    _fill_candles(store, "SOXL", n=40)
    _fill_candles(store, "QQQ", n=40, slope=-0.1)
    journal = InMemoryJournalStore()
    journal.save_cycle(_cycle([_outcome("SOXL"), _outcome("QQQ")]))
    ds = DatasetBuilder(journal, store).build()
    p = tmp_path / "out" / "ds.parquet"
    n = ParquetExporter().export(ds, p)
    assert n == 2
    df = pd.read_parquet(p)
    assert len(df) == 2
    assert "forward_return_30m" in df.columns and "label_class" in df.columns
    assert "feature_version" in df.columns
    soxl = df[df["symbol"] == "SOXL"].iloc[0]
    assert soxl["label_class"] == "WIN"
    assert abs(soxl["forward_return_30m"] - 0.03) < 1e-9
