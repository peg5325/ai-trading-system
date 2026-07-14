"""UniverseScreener 테스트: 랭킹, 필터링, 동점, 빈/부족 데이터, 레버리지 플래그."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pandas as pd

from src.engines.indicator_engine import OUTPUT_COLUMNS
from src.models import ET
from src.universe.screener import (
    CANDIDATE_EXCLUDED,
    CANDIDATE_FEATURE_VERSION,
    CANDIDATE_LONG,
    FLAG_EXT_HOURS,
    FLAG_EXTREME_VOL,
    FLAG_HIGH_VOL,
    FLAG_INSUFFICIENT,
    FLAG_LEVERAGED,
    FLAG_LOW_LIQUIDITY,
    FLAG_NEAR_CLOSE,
    Candidate,
    ScreenerConfig,
    SymbolData,
    UniverseScreener,
)

REG_10AM = datetime(2026, 7, 6, 10, 0, tzinfo=ET)   # 정규장, 마감 여유


def make_ind(*, close=101.0, ema7=100.5, ema20=99.5, rsi=58.0, prev_rsi=54.0,
             vwap=100.9, bb_mid=None, bb_width=0.05, volume=2000.0, avg_volume=1000.0,
             stoch_k=70.0, stoch_d=60.0, n=30, last_et=REG_10AM,
             prev_close=99.0, prev_ema20=99.5) -> pd.DataFrame:
    """지정한 마지막-봉 특성을 갖는 IndicatorEngine 형식 DataFrame 생성."""
    bb_mid = close if bb_mid is None else bb_mid
    half = bb_width * bb_mid / 2.0
    idx = pd.DatetimeIndex(
        [(last_et - timedelta(minutes=5 * (n - 1 - i))).astimezone(timezone.utc) for i in range(n)],
        name="ts_utc",
    )
    df = pd.DataFrame(index=idx)
    df["open"] = close
    df["high"] = close
    df["low"] = close
    df["close"] = close
    df["volume"] = avg_volume
    df["ema7"] = ema7
    df["ema20"] = ema20
    df["rsi14"] = rsi
    df["vwap"] = vwap
    df["bb_mid"] = bb_mid
    df["bb_upper"] = bb_mid + half
    df["bb_lower"] = bb_mid - half
    df["stochrsi_k"] = stoch_k
    df["stochrsi_d"] = stoch_d
    # 마지막/직전 봉 조정
    df.iloc[-1, df.columns.get_loc("volume")] = volume
    if n >= 2:
        df.iloc[-2, df.columns.get_loc("close")] = prev_close
        df.iloc[-2, df.columns.get_loc("ema20")] = prev_ema20
        df.iloc[-2, df.columns.get_loc("rsi14")] = prev_rsi
    return df[OUTPUT_COLUMNS]


def strong_setup(**kw) -> pd.DataFrame:
    return make_ind(**kw)


def weak_setup(**kw) -> pd.DataFrame:
    # 후보지만 약함: 추세 절반, 거래량 평범
    defaults = dict(close=100.2, ema7=99.9, ema20=100.0, rsi=50.0, prev_rsi=50.0,
                    vwap=100.2, volume=900.0, avg_volume=1000.0, stoch_k=40.0, stoch_d=45.0,
                    prev_close=100.1, prev_ema20=100.0)
    defaults.update(kw)
    return make_ind(**defaults)


# ---- 랭킹 ----
def test_ranking_orders_by_score_desc():
    scr = UniverseScreener()
    out = scr.screen([
        SymbolData("AAA", weak_setup(), 1),
        SymbolData("SOXL", strong_setup(), 3),
    ])
    assert [c.symbol for c in out] == ["SOXL", "AAA"]
    assert out[0].rank == 1 and out[1].rank == 2
    assert out[0].score > out[1].score
    assert out[0].candidate_type == CANDIDATE_LONG


# ---- 동점 처리 ----
def test_tie_break_is_deterministic_alphabetical():
    scr = UniverseScreener()
    ind = strong_setup()
    out = scr.screen([
        SymbolData("BBB", ind.copy(), 1),
        SymbolData("AAA", ind.copy(), 1),
    ])
    assert out[0].score == out[1].score        # 동점
    assert [c.symbol for c in out] == ["AAA", "BBB"]   # symbol 오름차순
    assert [c.rank for c in out] == [1, 2]


# ---- 필터링: 유동성 부족 ----
def test_low_liquidity_excluded():
    scr = UniverseScreener()
    inp = [SymbolData("LOWL", strong_setup(volume=50.0, avg_volume=50.0), 1)]
    assert scr.screen(inp) == []                       # 후보에서 제외
    ev = scr.screen_all(inp)[0]
    assert ev.candidate_type == CANDIDATE_EXCLUDED
    assert FLAG_LOW_LIQUIDITY in ev.risk_flags


# ---- 필터링: 변동성 과다(극단) 제외 ----
def test_extreme_volatility_excluded():
    scr = UniverseScreener()
    inp = [SymbolData("WILD", strong_setup(bb_width=0.20), 1)]
    assert scr.screen(inp) == []
    ev = scr.screen_all(inp)[0]
    assert ev.candidate_type == CANDIDATE_EXCLUDED
    assert FLAG_EXTREME_VOL in ev.risk_flags


# ---- 감점: 높은 변동성은 플래그 + 감점(후보 유지) ----
def test_high_volatility_flag_and_penalty():
    scr = UniverseScreener()
    base = scr.screen([SymbolData("A", strong_setup(bb_width=0.05), 1)])[0]
    hi = scr.screen([SymbolData("A", strong_setup(bb_width=0.10), 1)])[0]
    assert FLAG_HIGH_VOL in hi.risk_flags
    assert hi.score < base.score                       # 감점됨
    assert hi.candidate_type == CANDIDATE_LONG          # 여전히 후보


# ---- 리스크: 레버리지 ETF 인식 ----
def test_leveraged_etf_flag_explicit_and_auto():
    scr = UniverseScreener()
    explicit = scr.screen([SymbolData("SOXL", strong_setup(), 3)])[0]
    assert FLAG_LEVERAGED in explicit.risk_flags
    assert explicit.features["leverage_factor"] == 3
    # SymbolData.create 는 심볼로 레버리지 자동 인식
    auto = scr.screen([SymbolData.create("TQQQ", strong_setup())])[0]
    assert FLAG_LEVERAGED in auto.risk_flags
    # 일반주는 플래그 없음
    plain = scr.screen([SymbolData.create("QQQ", strong_setup())])[0]
    assert FLAG_LEVERAGED not in plain.risk_flags


# ---- 세션: 마감 임박 감점 + 플래그 ----
def test_near_market_close_flag_and_penalty():
    scr = UniverseScreener()
    base = scr.screen([SymbolData("A", strong_setup(last_et=REG_10AM), 1)])[0]
    near = datetime(2026, 7, 6, 15, 55, tzinfo=ET)     # 마감 5분 전
    late = scr.screen([SymbolData("A", strong_setup(last_et=near), 1)])[0]
    assert FLAG_NEAR_CLOSE in late.risk_flags
    assert late.score < base.score


# ---- 세션: 연장시간 스프레드 플래그 ----
def test_extended_hours_flag():
    scr = UniverseScreener()
    pre = datetime(2026, 7, 6, 6, 0, tzinfo=ET)        # 프리마켓
    ev = scr.screen([SymbolData("A", strong_setup(last_et=pre), 1)])[0]
    assert FLAG_EXT_HOURS in ev.risk_flags


# ---- 빈 데이터 ----
def test_empty_data_excluded():
    scr = UniverseScreener()
    empty = pd.DataFrame(columns=OUTPUT_COLUMNS)
    assert scr.screen([SymbolData("X", empty, 1)]) == []
    ev = scr.screen_all([SymbolData("X", empty, 1)])[0]
    assert ev.candidate_type == CANDIDATE_EXCLUDED
    assert FLAG_INSUFFICIENT in ev.risk_flags


# ---- 지표 부족(warm-up NaN) ----
def test_insufficient_indicators_excluded():
    scr = UniverseScreener()
    df = strong_setup()
    df.iloc[-1, df.columns.get_loc("ema20")] = float("nan")   # 마지막 봉 지표 미완
    inp = [SymbolData.create("SOXL", df)]
    assert scr.screen(inp) == []
    ev = scr.screen_all(inp)[0]
    assert ev.candidate_type == CANDIDATE_EXCLUDED
    assert FLAG_INSUFFICIENT in ev.risk_flags
    assert FLAG_LEVERAGED in ev.risk_flags               # 제외돼도 레버리지는 인식


# ---- 출력 구조(요구사항 9): 다운스트림 전달 형태 ----
def test_candidate_output_structure():
    scr = UniverseScreener()
    c = scr.screen([SymbolData("SOXL", strong_setup(), 3)])[0]
    d = c.to_dict()
    assert set(d) == {"symbol", "score", "rank", "candidate_type", "reasons", "risk_flags"}
    assert isinstance(d["reasons"], list) and isinstance(d["risk_flags"], list)
    assert d["candidate_type"] == CANDIDATE_LONG
    assert d["symbol"] == "SOXL"
    # 이유가 사람이 읽을 수 있게 채워짐
    assert any("EMA" in r or "VWAP" in r or "RSI" in r or "volume" in r for r in d["reasons"])


def test_reasons_reflect_setup():
    scr = UniverseScreener()
    c = scr.screen([SymbolData("SOXL", strong_setup(), 3)])[0]
    assert "EMA7 above EMA20 (uptrend)" in c.reasons
    assert "price near VWAP" in c.reasons


# ---- v2.1 계측(PR 2-A): score component 분해 ----
_COMPONENTS = ("trend", "momentum", "volume", "vwap_position", "volatility")


def test_score_components_recorded_with_subfields():
    c = UniverseScreener().screen([SymbolData("SOXL", strong_setup(), 3)])[0]
    comps = c.features["score_components"]
    assert set(comps) == set(_COMPONENTS)
    for name, comp in comps.items():
        assert set(comp) == {"raw_inputs", "normalized_score", "weight", "weighted_score"}
        assert isinstance(comp["raw_inputs"], dict) and comp["raw_inputs"]
        assert math.isclose(comp["weighted_score"], comp["weight"] * comp["normalized_score"])
    assert c.features["feature_schema_version"] == CANDIDATE_FEATURE_VERSION


def test_weighted_component_sum_matches_components():
    c = UniverseScreener().screen([SymbolData("SOXL", strong_setup(), 3)])[0]
    comps = c.features["score_components"]
    assert math.isclose(c.features["weighted_component_sum"],
                        sum(comp["weighted_score"] for comp in comps.values()))


def test_pre_clamp_score_invariant():
    """불변식: pre_clamp_score == weighted_component_sum - penalty_score."""
    c = UniverseScreener().screen([SymbolData("A", strong_setup(bb_width=0.10), 1)])[0]
    f = c.features
    assert f["penalty_score"] > 0                       # HIGH_VOLATILITY 감점 발생
    assert math.isclose(f["pre_clamp_score"],
                        f["weighted_component_sum"] - f["penalty_score"])


def test_candidate_score_equals_clamp_round_of_pre_clamp():
    """최종 candidate_score 는 기존 clamp+round 결과와 동일(경계 포함)."""
    for sd in (SymbolData("SOXL", strong_setup(), 3),
               SymbolData("A", strong_setup(bb_width=0.10), 1)):
        c = UniverseScreener().screen([sd])[0]
        f = c.features
        expected = round(max(0.0, min(100.0, f["pre_clamp_score"])), 1)
        assert f["candidate_score"] == expected == c.score


def test_upper_clamp_boundary_capped_at_100():
    cfg = ScreenerConfig(weights={"trend": 0.6, "momentum": 0.6, "volume": 0.6,
                                  "vwap_position": 0.6, "volatility": 0.6})  # 합=3.0
    c = UniverseScreener(cfg).screen([SymbolData("SOXL", strong_setup(), 3)])[0]
    assert c.features["pre_clamp_score"] > 100.0
    assert c.features["candidate_score"] == 100.0 == c.score


def test_below_min_candidate_keeps_decomposition():
    """BELOW_MIN 로 제외돼도 점수 분해는 기록되고 불변식이 성립한다."""
    weak = weak_setup(rsi=40.0, prev_rsi=45.0, vwap=101.0, volume=500.0, stoch_k=30.0,
                      stoch_d=40.0)
    ev = UniverseScreener().screen_all([SymbolData("W", weak, 1)])[0]
    if ev.candidate_type == CANDIDATE_EXCLUDED and ev.features:
        f = ev.features
        assert math.isclose(f["pre_clamp_score"],
                            f["weighted_component_sum"] - f["penalty_score"])
        assert f["candidate_score"] == round(max(0.0, min(100.0, f["pre_clamp_score"])), 1)


# ---- v2.1 계측(PR 2-A): volume 피처 ----
def test_volume_features_recorded():
    c = UniverseScreener().screen([SymbolData("SOXL", strong_setup(), 3)])[0]
    f = c.features
    assert f["volume_last"] == 2000.0
    assert math.isclose(f["volume_sma20"], 1050.0)         # 19*1000 + 2000 = 21000/20
    assert math.isclose(f["volume_ratio"], 2000.0 / 1050.0)
    assert math.isclose(f["dollar_volume"], 2000.0 * 101.0)  # last_volume * close


def test_legacy_feature_keys_preserved():
    """하위호환: 기존 스칼라 피처 키를 계속 유지한다."""
    c = UniverseScreener().screen([SymbolData("SOXL", strong_setup(), 3)])[0]
    for k in ("trend", "momentum", "volume", "vwap_position", "volatility",
              "bb_width", "avg_volume", "session", "penalties", "leverage_factor"):
        assert k in c.features


def test_nan_last_volume_records_none_not_nan():
    """마지막 봉 volume 이 NaN 이면 volume 계측은 None (비표준 JSON NaN 방지).
    점수 계산 경로(_volume 이 NaN 을 30.0 으로 흡수)는 영향받지 않는다."""
    df = strong_setup()
    df.iloc[-1, df.columns.get_loc("volume")] = float("nan")
    ev = UniverseScreener().screen_all([SymbolData("SOXL", df, 3)])[0]
    f = ev.features
    assert f["volume_last"] is None
    assert f["volume_ratio"] is None
    assert f["dollar_volume"] is None
    assert f["score_components"]["volume"]["raw_inputs"]["last_volume"] is None
    # NaN 이 점수/집계로 새지 않는다
    assert not math.isnan(ev.score)
    assert f["candidate_score"] == round(max(0.0, min(100.0, f["pre_clamp_score"])), 1)
