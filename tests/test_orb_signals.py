from datetime import date

from src.research.orb.contract import load_contract, primary_spec
from src.research.orb.signals import (
    detect_breakout,
    is_strategy_candidate,
    rank_signals,
)
from tests.orb_test_helpers import bar, features


def test_candidate_requires_data_eligibility_and_both_thresholds():
    contract = load_contract()
    spec = primary_spec(contract)
    day = date(2024, 3, 4)
    assert is_strategy_candidate(features(day), spec)
    assert not is_strategy_candidate(features(day, gap=0.0029), spec)
    assert not is_strategy_candidate(features(day, rvol=1.49), spec)
    assert not is_strategy_candidate(
        features(day, excluded=("incomplete_opening_range",)), spec)


def test_breakout_is_strict_and_cannot_fire_inside_opening_range():
    spec = primary_spec(load_contract())
    day = date(2024, 3, 4)
    feat = features(day)
    assert detect_breakout(feat, bar(day, 580, c=101), spec, decision_bar_minutes=5) is None
    assert detect_breakout(feat, bar(day, 585, c=100), spec, decision_bar_minutes=5) is None
    signal = detect_breakout(
        feat, bar(day, 585, c=100.01), spec, decision_bar_minutes=5)
    assert signal is not None
    assert signal.signal_minute == 585
    assert signal.fill_minute == 590
    assert signal.target_r_multiple == 2.0


def test_rank_is_rvol_then_abs_gap_then_ascii_symbol():
    spec = primary_spec(load_contract())
    day = date(2024, 3, 4)
    signals = [
        detect_breakout(features(day, symbol="MSFT", rvol=2, gap=.02),
                        bar(day, 585, symbol="MSFT", c=101), spec,
                        decision_bar_minutes=5),
        detect_breakout(features(day, symbol="AAPL", rvol=2, gap=.02),
                        bar(day, 585, symbol="AAPL", c=101), spec,
                        decision_bar_minutes=5),
        detect_breakout(features(day, symbol="NVDA", rvol=3, gap=.01),
                        bar(day, 585, symbol="NVDA", c=101), spec,
                        decision_bar_minutes=5),
    ]
    ranked = rank_signals([s for s in signals if s is not None])
    assert [s.symbol for s in ranked] == ["NVDA", "AAPL", "MSFT"]
