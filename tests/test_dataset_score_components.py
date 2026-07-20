"""PR A(H2): score_components 개별 컴포넌트 export 검증.

배경: 지금까지 FeatureRecord 에는 합계(weighted_component_sum/penalty_score/pre_clamp_score)만
있어서 "어떤 컴포넌트가 예측력을 갖는가"(H2)를 표준 데이터셋 경로로 분석할 수 없었다
(원본 저널 직접 파싱이 필요했다). 이 테스트는 컴포넌트 개별 필드가

  ① 저널 → FeatureRecord 로 정확히 매핑되고
  ② 스크리너 산술 불변식을 만족하며
  ③ to_flat()(CSV/Parquet 경로)에서 누락되지 않고
  ④ 값이 없을 때 0.0 이 아니라 None 으로 남는다

는 것을 강제한다. ④가 중요한 이유: 0.0 은 "계산됐고 0점"과 구분되어야 하며,
조용히 0 을 채우면 상관분석이 가짜 표본을 얻는다.

합성 데이터만 사용 — 네트워크/실데이터 불필요.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.dataset.builder import DatasetBuilder, LabelConfig
from src.dataset.models import (
    SCORE_COMPONENT_FIELDS,
    SCORE_COMPONENT_NAMES,
    score_component_field_names,
)
from src.journal.models import InMemoryJournalStore, JournalCycle
from src.models import Candle
from src.storage.candle_store import CandleStore
from src.universe.screener import ScreenerConfig

CTS = datetime(2026, 7, 7, 4, 15, 0, tzinfo=timezone.utc)
CYCLE_ID = "20260707T041500Z"
CYCLE_TS = "2026-07-07T04:15:00Z"
ENTRY = 100.0

# 실 저널(us-phase6-s001)에서 관측된 형태를 그대로 본뜬 컴포넌트 블록.
# 가중치는 ScreenerConfig 기본값과 동일하다.
_COMPONENTS = {
    "trend": {"raw_inputs": {"close": 100.0, "ema7": 99.0, "ema20": 98.0},
              "normalized_score": 100.0, "weight": 0.30, "weighted_score": 30.0},
    "momentum": {"raw_inputs": {"rsi14": 55.0, "prev_rsi14": 50.0,
                                "stochrsi_k": 0.6, "stochrsi_d": 0.5},
                 "normalized_score": 85.0, "weight": 0.25, "weighted_score": 21.25},
    "volume": {"raw_inputs": {"last_volume": 66899.0, "volume_sma20": 26640.5,
                              "volume_ratio": 2.5111765920309304},
               "normalized_score": 90.0, "weight": 0.20, "weighted_score": 18.0},
    "vwap_position": {"raw_inputs": {"close": 100.0, "vwap": 99.5,
                                     "vwap_distance": 0.005},
                      "normalized_score": 80.0, "weight": 0.15, "weighted_score": 12.0},
    "volatility": {"raw_inputs": {"bb_width": 0.0038},
                   "normalized_score": 45.0, "weight": 0.10, "weighted_score": 4.5},
}
_WEIGHTED_SUM = 85.75          # 30.0 + 21.25 + 18.0 + 12.0 + 4.5
_PENALTY = 10.0
_PRE_CLAMP = 75.75             # _WEIGHTED_SUM - _PENALTY


def _candle(sym, minute_offset, close):
    moment = CTS + timedelta(minutes=minute_offset)
    return Candle.from_moment(sym, "1m", moment, o=close, h=close + 0.5,
                              low=close - 0.5, c=close, v=1000.0,
                              currency="USD", adjusted=False)


def _outcome(sym, *, features):
    return {
        "symbol": sym, "action": "ORDERED", "reason": "ORDERED",
        "indicator_snapshot": {"close": ENTRY, "ema7": 99.0, "ema20": 98.0,
                               "rsi14": 55.0, "vwap": 99.5, "bb_upper": 102.0,
                               "bb_lower": 96.0, "stochrsi_k": 0.6, "stochrsi_d": 0.5},
        "candidate": {"symbol": sym, "score": 75.8, "rank": 1,
                      "candidate_type": "LONG_SETUP", "reasons": ["uptrend"],
                      "risk_flags": [], "features": features},
        "ai_decision": {"decision": "APPROVE_ENTRY", "confidence_score": 70},
        "risk_decision": {"allowed": True, "adjusted_quantity": 10},
        "candidate_id": "cid-1",
    }


def _full_features():
    return {
        "score_components": _COMPONENTS,
        "weighted_component_sum": _WEIGHTED_SUM,
        "penalty_score": _PENALTY,
        "pre_clamp_score": _PRE_CLAMP,
        "candidate_score": 75.8,
        "volume_last": 66899.0, "volume_sma20": 26640.5,
        "volume_ratio": 2.5111765920309304, "dollar_volume": 6689900.0,
        "feature_schema_version": "1.1",
    }


def _build(features, tmp_path):
    store = CandleStore(str(tmp_path / "m.sqlite"))
    store.upsert_many([_candle("SOXL", off, ENTRY + off * 0.1) for off in range(0, 70)])
    journal = InMemoryJournalStore()
    journal.save_cycle(JournalCycle(
        cycle_id=CYCLE_ID, cycle_ts=CYCLE_TS, session="REGULAR",
        universe=["SOXL"], status="RAN", reason=None, error=None,
        outcomes=[_outcome("SOXL", features=features)],
        portfolio={"account_value": 2000.0, "positions": {}, "kill_switch": False}))
    ds = DatasetBuilder(journal, store, config=LabelConfig(windows_min=(30,))).build()
    assert ds.records, "레코드가 생성되어야 한다"
    return ds.records[0]


def test_component_fields_mapped_from_journal(tmp_path):
    """저널 score_components → FeatureRecord 개별 필드로 정확히 옮겨진다."""
    rec = _build(_full_features(), tmp_path)
    f = rec.feature
    for name in SCORE_COMPONENT_NAMES:
        src = _COMPONENTS[name]
        for fld in SCORE_COMPONENT_FIELDS:
            got = getattr(f, f"{name}_{fld}")
            assert got == src[fld], f"{name}_{fld}: {got} != {src[fld]}"


def test_component_invariants_hold(tmp_path):
    """스크리너 산술 불변식 — 합계·곱 모두."""
    f = _build(_full_features(), tmp_path).feature
    total = sum(getattr(f, f"{n}_weighted_score") for n in SCORE_COMPONENT_NAMES)
    assert abs(total - f.weighted_component_sum) <= 1e-9, "Σ weighted_score == weighted_component_sum"
    assert abs((f.weighted_component_sum - f.penalty_score) - f.pre_clamp_score) <= 1e-9, \
        "pre_clamp_score == weighted_component_sum - penalty_score (penalty 는 뺄셈)"
    for n in SCORE_COMPONENT_NAMES:
        w = getattr(f, f"{n}_weight")
        ns = getattr(f, f"{n}_normalized_score")
        ws = getattr(f, f"{n}_weighted_score")
        assert abs(ns * w - ws) <= 1e-9, f"{n}: normalized*weight == weighted"


def test_weights_match_screener_config(tmp_path):
    """export 된 weight 가 실제 스크리너 가중치와 일치(가중치 드리프트 감지)."""
    f = _build(_full_features(), tmp_path).feature
    cfg = ScreenerConfig()
    for n in SCORE_COMPONENT_NAMES:
        assert abs(getattr(f, f"{n}_weight") - cfg.weights[n]) <= 1e-12, \
            f"{n} weight 가 ScreenerConfig 와 다르다"


def test_component_name_set_matches_screener_exactly():
    """드리프트를 **양방향**으로 닫는다.

    이름별 값 비교만 하면 기존 5종의 값 변경은 잡히지만, 스크리너에 6번째
    컴포넌트가 추가되는 건 순회에서 빠져 통과한 채 export 에서만 조용히 누락된다.
    집합 동치로 양쪽 방향을 모두 고정한다.
    """
    assert set(SCORE_COMPONENT_NAMES) == set(ScreenerConfig().weights), (
        "스크리너 가중치 키와 export 컴포넌트 집합이 다르다 — "
        "한쪽에만 추가되면 데이터셋에서 조용히 누락된다")


def test_missing_components_stay_none_not_zero(tmp_path):
    """하드 제외/구 스키마로 score_components 가 없으면 None — 0.0 으로 채우지 않는다."""
    rec = _build({"weighted_component_sum": None}, tmp_path)
    f = rec.feature
    for name in score_component_field_names():
        assert getattr(f, name) is None, f"{name} 은 None 이어야 한다(0.0 금지)"


def test_to_flat_exports_all_component_fields(tmp_path):
    """to_flat 은 수동 dict — 필드 누락 시 CSV/Parquet 에서 조용히 사라지므로 고정한다."""
    rec = _build(_full_features(), tmp_path)
    flat = rec.to_flat()
    expected = score_component_field_names()
    assert len(expected) == 15
    for name in expected:
        assert name in flat, f"to_flat 에 {name} 누락"
    assert flat["vwap_position_normalized_score"] == 80.0
    assert flat["volume_weighted_score"] == 18.0


def test_to_dict_roundtrip_includes_components(tmp_path):
    """JSONL 경로(to_dict=asdict)도 컴포넌트를 포함한다."""
    f = _build(_full_features(), tmp_path).feature
    d = f.to_dict()
    for name in score_component_field_names():
        assert name in d
    assert d["trend_weighted_score"] == 30.0
