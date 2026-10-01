"""Screen v2 tests — synthetic bars only (no snapshot, no operating DB, no OOS/H2)."""
from __future__ import annotations

import json
import random
from dataclasses import fields
from datetime import date, datetime

import numpy as np
import pytest

import scripts.run_gross_edge_screen_v2 as cli
from src.history import exchange_calendar as cal
from src.history.oos_guard import (
    TRACK_INTRADAY,
    OOSLockError,
    assert_window_unlocked,
    research_load,
)
from src.models import ET, Candle
from src.research.gross_edge_v2 import arms as A
from src.research.gross_edge_v2 import data as D
from src.research.gross_edge_v2 import run as R
from src.research.gross_edge_v2.contract import (
    ARM_IDS,
    PREREG_JSON_SHA256,
    PREREG_MD_SHA256,
    compute_json_sha256,
    compute_md_sha256,
    load_contract,
)
from src.research.gross_edge_v2.counts import condition_counts
from src.research.gross_edge_v2.daily import (
    DailyBar,
    Indicators,
    atr,
    compute_indicators,
    rsi,
    valid_daily_bars,
)
from src.research.gross_edge_v2.evaluation import (
    BaselineCache,
    bootstrap_p_value,
    daily_returns,
    evaluate_arms,
    holm_adjust,
    leg_statistics,
    verify_statistics,
)
from src.research.gross_edge_v2.report import arm_row, render_markdown, write_new

M = D.hm
DAY = date(2025, 3, 4)


# ---------------------------------------------------------------------------
# synthetic builders
# ---------------------------------------------------------------------------
def flat_day(symbol: str, day: date, price: float = 100.0, *, overrides=None, drop=(),
             count: int = 390, volume: float = 1000.0) -> D.DayBars:
    """Flat bars o=h=l=c=price. ``count`` < 390 drops bars from 11:00 onward."""
    rows = {}
    dropped = set(drop) | set(range(M("11:00"), M("11:00") + (390 - count)))
    for minute in range(D.OPEN_MINUTE, D.OPEN_MINUTE + D.SLOTS):
        if minute not in dropped:
            rows[minute] = (price, price, price, price, volume)
    for minute, bar in (overrides or {}).items():
        rows[minute] = bar if len(bar) == 5 else (*bar, volume)
    for minute in drop:
        rows.pop(minute, None)
    return D.DayBars(symbol, day, rows)


def ind(**values) -> Indicators:
    base = {f.name: None for f in fields(Indicators)}
    base.update(day=DAY, close=100.0, range=1.0)
    base.update(values)
    return Indicators(**base)


def trade(arm: str, symbol: str, day: date, gross: float, minute: int = 600) -> A.Trade:
    return A.Trade(arm, symbol, day, minute, 100.0, 955, 100.0 + gross, "time", gross)


def random_day(symbol: str, day: date, rng: np.random.Generator, start: float) -> D.DayBars:
    steps = rng.normal(0, 0.002, D.SLOTS)
    closes = start * np.exp(np.cumsum(steps))
    opens = np.concatenate([[start], closes[:-1]])
    highs = np.maximum(opens, closes) * (1 + np.abs(rng.normal(0, 0.001, D.SLOTS)))
    lows = np.minimum(opens, closes) * (1 - np.abs(rng.normal(0, 0.001, D.SLOTS)))
    vols = rng.integers(100, 5000, D.SLOTS).astype(float)
    rows = {
        D.OPEN_MINUTE + i: (opens[i], highs[i], lows[i], closes[i], vols[i])
        for i in range(D.SLOTS)
    }
    return D.DayBars(symbol, day, rows)


def random_universe(start: date, end: date, seed: int = 7):
    rng = np.random.default_rng(seed)
    out = {}
    for symbol in A.UNIVERSE:
        price, days = 100.0, {}
        for day in cal.trading_days(start, end):
            if cal.is_early_close(day):
                continue
            bars = random_day(symbol, day, rng, price)
            days[day] = bars
            price = float(bars.close[-1]) * float(np.exp(rng.normal(0, 0.02)))
        out[symbol] = days
    return out


# ---------------------------------------------------------------------------
# contract and registry
# ---------------------------------------------------------------------------
def test_prereg_file_hashes_are_frozen_and_registry_has_eight_arms() -> None:
    assert compute_md_sha256() == PREREG_MD_SHA256
    assert compute_json_sha256() == PREREG_JSON_SHA256
    contract = load_contract()
    A.verify_registry(contract)
    verify_statistics(contract)
    assert tuple(A.ARM_SYMBOLS) == ARM_IDS == ("A1", "A2", "B1", "B2", "B3", "C1", "C2", "D1")
    assert A.A1_LEGS == (("QQQ", 1.5, "TQQQ", "SQQQ"), ("SOXL", 4.5, "SOXL", "SOXS"))
    assert (A.A_SIGNAL_MINUTE, A.A_ENTRY_MINUTE, D.EXIT_MINUTE) == (M("14:44"), M("14:45"), 955)
    assert (A.B_FIRST_MINUTE, A.SCAN_LAST_MINUTE) == (M("09:31"), M("15:30"))
    assert (A.C2_RANGE_LAST, A.C2_FIRST_MINUTE) == (M("10:29"), M("10:30"))
    assert (A.D1_SIGNAL_FIRST, A.D1_SIGNAL_LAST, A.D1_K, A.D1_STOP_ATR) == (600, 900, 0.6, 0.3)
    assert len(A.A2_SYMBOLS) == 13 and len(A.D1_SYMBOLS) == 9 and len(A.UNIVERSE) == 20


def test_contract_is_immutable_and_registry_tamper_fails_closed() -> None:
    contract = load_contract()
    with pytest.raises(TypeError):
        contract["windows"]["blind_oos_access"] = "allowed"
    tampered = json.loads(json.dumps(
        contract, default=lambda value: dict(value) if hasattr(value, "items") else list(value)
    ))
    tampered["arms"][7]["stop_loss"] = "entry - 0.5 * ATR14(t-1)"
    with pytest.raises(A.RegistryMismatchError):
        A.verify_registry(tampered)


def test_windows_counts_match_contract() -> None:
    contract = load_contract()
    windows = D.build_windows(contract)
    assert D.window_defects(contract, windows) == []
    assert (len(windows.trade_days), len(windows.excluded_block_days),
            len(windows.evaluated_days)) == (670, 44, 619)
    evaluated = set(windows.evaluated_days)
    assert not any(date(2024, 7, 1) <= day <= date(2024, 8, 30) for day in evaluated)
    assert not any(cal.is_early_close(day) for day in evaluated)
    assert max(evaluated) == date(2026, 1, 20)


# ---------------------------------------------------------------------------
# fill model
# ---------------------------------------------------------------------------
def test_buy_stop_fills_at_max_of_stop_and_open_and_never_on_0930() -> None:
    bars = flat_day("SPY", DAY, overrides={
        570: (100, 101, 100, 100),          # 09:30 high above stop is ignored
        M("09:40"): (101, 101.2, 100.9, 101.1),  # gap above stop -> open
    })
    trade_ = A.b_arm("B1", bars, ind(nr7=True, stretch10=0.5))
    assert isinstance(trade_, A.Trade)
    assert (trade_.entry_minute, trade_.entry_price) == (M("09:40"), 101)
    bars = flat_day("SPY", DAY, overrides={M("09:41"): (100.2, 100.6, 100.1, 100.4)})
    trade_ = A.b_arm("B1", bars, ind(nr7=True, stretch10=0.5))
    assert (trade_.entry_minute, trade_.entry_price) == (M("09:41"), 100.5)


def test_buy_stop_scan_skips_missing_bars() -> None:
    assert A.buy_stop_fill(
        flat_day("SPY", DAY, overrides={M("09:46"): (100.2, 100.7, 100.2, 100.3)},
                 drop=(M("09:45"),)), 100.5, 571, 930,
    ) == (M("09:46"), 100.5)


def test_strict_limit_touch_does_not_fill_and_open_below_fills_at_open() -> None:
    touch = flat_day("SPY", DAY, overrides={M("10:00"): (99.5, 99.5, 99.0, 99.2)})
    assert A.c1(touch, ind(rsi2=3.0, sma200=90.0, atr10=2.0)) == A.NO_FILL
    through = flat_day("SPY", DAY, overrides={M("10:00"): (99.5, 99.5, 98.9, 99.2)})
    filled = A.c1(through, ind(rsi2=3.0, sma200=90.0, atr10=2.0))
    assert (filled.entry_minute, filled.entry_price) == (M("10:00"), 99.0)
    gap = flat_day("SPY", DAY, price=98.5)
    filled = A.c1(gap, ind(rsi2=3.0, sma200=90.0, atr10=2.0))
    assert (filled.entry_minute, filled.entry_price) == (570, 98.5)
    assert A.c1(gap, ind(rsi2=5.0, sma200=90.0, atr10=2.0)) == A.NO_SIGNAL
    assert A.c1(gap, ind(rsi2=3.0, sma200=None, atr10=2.0)) == A.UNDEFINED


def test_entry_bar_stop_books_stop_price_later_bar_min_of_stop_and_open() -> None:
    entry_bar_stop = flat_day("SPY", DAY, overrides={
        M("09:45"): (100.2, 100.6, 99.9, 100.4),
        M("09:46"): (100.4, 100.4, 100.4, 100.4),
    })
    result = A.b_arm("B1", entry_bar_stop, ind(nr7=True, stretch10=0.5))
    assert (result.exit_minute, result.exit_price, result.exit_reason) == (M("09:45"), 100.0, "stop")
    assert result.gross_pct_points == pytest.approx((100 / 100.5 - 1) * 100)
    later = flat_day("SPY", DAY, price=100.5, overrides={
        570: (100, 100, 100, 100.5),
        M("09:45"): (100.5, 100.6, 100.1, 100.4),
        M("09:50"): (99.8, 99.9, 99.7, 99.8),
    })
    result = A.b_arm("B1", later, ind(nr7=True, stretch10=0.5))
    assert (result.exit_minute, result.exit_price) == (M("09:50"), 99.8)


def test_no_reentry_after_stop_one_trade_per_arm_symbol_day() -> None:
    bars = flat_day("SPY", DAY, overrides={
        M("09:45"): (100.2, 100.6, 99.9, 100.4),
        M("10:30"): (100.6, 102.0, 100.6, 101.9),
    })
    result = A.b_arm("B1", bars, ind(nr7=True, stretch10=0.5))
    assert isinstance(result, A.Trade) and result.exit_minute == M("09:45")


def d_day(extra=None) -> D.DayBars:
    overrides = {
        M("10:00"): (100, 100, 99.3, 99.3),
        M("10:01"): (99.3, 100.2, 99.3, 99.4),   # entry bar: high above VWAP, no target
        M("10:02"): (99.5, 100.5, 99.5, 100.0),
    }
    overrides.update(extra or {})
    return flat_day("TSLA", DAY, overrides=overrides)


def test_d_entry_next_bar_and_target_is_previous_bar_vwap_from_bar_after_entry() -> None:
    bars = d_day()
    result = A.d1(bars, ind(atr14=1.0))
    assert (result.entry_minute, result.entry_price) == (M("10:01"), 99.3)
    assert result.exit_minute == M("10:02") and result.exit_reason == "target"
    assert result.exit_price == pytest.approx(bars.vwap_through(M("10:01")))
    assert result.exit_price != pytest.approx(bars.vwap_through(M("10:02")))


def test_d_same_bar_stop_first_and_missing_entry_bar_skips() -> None:
    result = A.d1(d_day({M("10:02"): (99.5, 100.5, 98.9, 100.0)}), ind(atr14=1.0))
    assert (result.exit_reason, result.exit_price) == ("stop", pytest.approx(99.0))
    missing = flat_day("TSLA", DAY, overrides={M("10:00"): (100, 100, 99.3, 99.3)},
                       drop=(M("10:01"),))
    assert A.d1(missing, ind(atr14=1.0)) == A.MISSING_BAR
    assert A.d1(d_day(), ind(atr14=None)) == A.UNDEFINED


def test_zero_volume_vwap_is_undefined() -> None:
    bars = flat_day("TSLA", DAY, volume=0.0)
    assert bars.vwap_through(M("12:00")) is None
    assert A.d1(bars, ind(atr14=1.0)) == A.NO_SIGNAL


def a_day(symbol: str, close_1444: float, *, open_1445: float = 103.3, drop=()) -> D.DayBars:
    return flat_day(symbol, DAY, overrides={
        M("14:44"): (100, max(100, close_1444), min(100, close_1444), close_1444),
        M("14:45"): (open_1445, open_1445, open_1445, open_1445),
        955: (104, 104, 104, 104),
    }, drop=drop)


def test_a2_next_bar_entry_and_threshold() -> None:
    result = A.a2(a_day("NVDA", 103.2))
    assert (result.entry_minute, result.entry_price, result.exit_price) == (M("14:45"), 103.3, 104)
    assert A.a2(a_day("NVDA", 102.9)) == A.NO_SIGNAL
    assert A.a2(a_day("NVDA", 103.2, drop=(M("14:45"),))) == A.MISSING_BAR
    assert A.a2(a_day("NVDA", 103.2, drop=(M("15:55"),))) == A.MISSING_BAR


def test_a1_requires_both_symbols_bars_and_routes_inverse() -> None:
    qqq_up = a_day("QQQ", 101.6)
    traded = {"TQQQ": a_day("TQQQ", 100.0), "SQQQ": a_day("SQQQ", 100.0)}
    result = A.a1_leg(A.A1_LEGS[0], qqq_up, traded)
    assert result.symbol == "TQQQ" and result.entry_minute == M("14:45")
    down = A.a1_leg(A.A1_LEGS[0], a_day("QQQ", 98.4), traded)
    assert down.symbol == "SQQQ" and down.inverse
    assert A.a1_leg(A.A1_LEGS[0], a_day("QQQ", 101.4), traded) == A.NO_SIGNAL
    missing_traded = {"TQQQ": a_day("TQQQ", 100.0, drop=(M("14:44"),))}
    assert A.a1_leg(A.A1_LEGS[0], qqq_up, missing_traded) == A.MISSING_BAR
    missing_signal = a_day("QQQ", 101.6, drop=(M("14:45"),))
    assert A.a1_leg(A.A1_LEGS[0], missing_signal, traded) == A.MISSING_BAR
    assert A.a1_leg(A.A1_LEGS[0], qqq_up, {"TQQQ": None}) == A.INVALID_DAY


def test_required_exact_bars_common_and_c2() -> None:
    prev = ind(nr7=True, stretch10=0.5, rsi3_roc1=10.0)
    assert A.b_arm("B1", flat_day("SPY", DAY, drop=(570,)), prev) == A.MISSING_BAR
    assert A.b_arm("B1", flat_day("SPY", DAY, drop=(955,)), prev) == A.MISSING_BAR
    assert A.c2(flat_day("SPY", DAY, drop=(M("10:29"),)), prev) == A.MISSING_BAR
    c2 = A.c2(flat_day("SPY", DAY, overrides={
        M("10:00"): (100, 100.8, 99.5, 100),
        M("10:40"): (100.5, 100.9, 100.4, 100.6),
    }), prev)
    assert (c2.entry_minute, c2.entry_price) == (M("10:40"), 100.8)
    assert A.c2(flat_day("SPY", DAY), ind(rsi3_roc1=30.0)) == A.NO_SIGNAL


# ---------------------------------------------------------------------------
# daily bars and indicators
# ---------------------------------------------------------------------------
def test_valid_day_threshold_and_early_close_invalid() -> None:
    assert D.is_valid_symbol_day(DAY, 371) and not D.is_valid_symbol_day(DAY, 370)
    early = date(2025, 11, 28)
    assert not D.is_valid_symbol_day(early, 390)
    bars = {DAY: flat_day("SPY", DAY, count=370), date(2025, 3, 5): flat_day("SPY", date(2025, 3, 5))}
    assert [bar.day for bar in valid_daily_bars(bars)] == [date(2025, 3, 5)]


def test_atr_warmup_3n_and_first_tr_is_high_low() -> None:
    bars = [DailyBar(date(2025, 1, 1 + i), 10, 12, 9, 11) for i in range(7)]
    values = atr(bars, 2)
    assert values[:5] == [None] * 5 and values[5] is not None
    assert atr(bars[:1], 1) == [None] and atr(bars[:3], 1)[2] is not None


def test_rsi_edge_cases_and_warmup() -> None:
    assert rsi([1, 2, 3, 4, 5, 6], 2)[-1] == 100.0
    assert rsi([5.0] * 6, 2)[-1] == 50.0
    assert rsi([1, 2, 3, 4, 5], 2)[-1] is None  # fewer than 3n values
    assert rsi([3, 2, 1, 0, -1, -2], 2)[-1] == 0.0


def test_indicators_use_only_data_through_t_minus_1() -> None:
    rng = random.Random(3)
    bars = []
    for i, day in enumerate(cal.trading_days(date(2024, 1, 2), date(2024, 12, 31))):
        o = 100 + rng.uniform(-5, 5)
        bars.append(DailyBar(day, o, o + rng.uniform(0, 3), o - rng.uniform(0, 3), o + rng.uniform(-2, 2)))
    full = compute_indicators(bars)
    for cut in (30, 120, 220):
        prefix = compute_indicators(bars[:cut])
        assert prefix[bars[cut - 1].day] == full[bars[cut - 1].day]
    last = full[bars[-1].day]
    assert last.rsi3_roc1 is not None and last.sma200 is not None and last.atr14 is not None
    early = compute_indicators(bars[:10])
    assert early[bars[9].day].rsi3_roc1 is not None
    assert compute_indicators(bars[:9])[bars[8].day].rsi3_roc1 is None
    assert full[bars[6].day].nr7 is not None and full[bars[5].day].nr7 is None


def series_days(symbol: str, start: date, end: date, *, skip_block: bool = False):
    out = {}
    for i, day in enumerate(d for d in cal.trading_days(start, end) if not cal.is_early_close(d)):
        if skip_block and date(2024, 7, 1) <= day <= date(2024, 8, 30):
            continue
        out[day] = flat_day(symbol, day, price=100 + i * 0.1)
    return out


def test_excluded_block_feeds_indicators_and_is_not_traded() -> None:
    target = date(2024, 9, 3)  # t-1 = 2024-08-30, inside the excluded block
    bars = {"SPY": series_days("SPY", date(2024, 6, 1), target)}
    extraction = A.extract_trades(bars, [target])
    b3 = [t for t in extraction.trades if t.arm_id == "B3"]
    assert [(t.symbol, t.day) for t in b3] == [("SPY", target)]
    without = {"SPY": series_days("SPY", date(2024, 6, 1), target, skip_block=True)}
    extraction = A.extract_trades(without, [target])
    assert not [t for t in extraction.trades if t.arm_id == "B3"]
    with pytest.raises(ValueError):
        A.extract_trades(bars, [date(2024, 8, 5)])
    with pytest.raises(ValueError):
        A.extract_trades(bars, [date(2025, 11, 28)])


def test_invalid_t_minus_1_blocks_b_c_d() -> None:
    days = series_days("TSLA", date(2024, 10, 1), date(2025, 3, 4))
    prev = date(2025, 3, 3)
    days[prev] = flat_day("TSLA", prev, count=300)
    extraction = A.extract_trades({"TSLA": days}, [date(2025, 3, 4)])
    for arm_id in ("B1", "B2", "B3", "C1", "C2", "D1"):
        assert extraction.skip_counts[arm_id].get(A.PREV_INVALID) == 1
    assert not [t for t in extraction.trades if t.symbol == "TSLA"]
    assert extraction.invalid_symbol_days["TSLA"] == 0
    assert extraction.invalid_symbol_days["SPY"] == 1


# ---------------------------------------------------------------------------
# loading boundary
# ---------------------------------------------------------------------------
def test_loader_requests_only_development_window_and_rejects_later_bars(monkeypatch) -> None:
    calls = []

    def fake_research_load(db, symbol, interval, **kwargs):
        calls.append(kwargs)
        moment = datetime(2026, 1, 20, 9, 30, tzinfo=ET)
        return [Candle.from_moment(symbol, "1m", moment, 1, 1, 1, 1, 1, "USD", True)]

    monkeypatch.setattr(D, "research_load", fake_research_load)
    bars = D.load_symbol_bars("/nowhere.sqlite", "SPY")
    assert list(bars) == [date(2026, 1, 20)]
    (kwargs,) = calls
    assert kwargs["end"] == datetime(2026, 1, 21, tzinfo=ET)
    assert kwargs["start"] == datetime(2023, 1, 3, tzinfo=ET)
    assert (kwargs["purpose"], kwargs["track"]) == ("performance", TRACK_INTRADAY)
    assert_window_unlocked(kwargs["start"], kwargs["end"], track=TRACK_INTRADAY,
                           strategy_id=None, execution_grant=None)

    def leaking(db, symbol, interval, **kwargs):
        moment = datetime(2026, 1, 21, 9, 30, tzinfo=ET)
        return [Candle.from_moment(symbol, "1m", moment, 1, 1, 1, 1, 1, "USD", True)]

    monkeypatch.setattr(D, "research_load", leaking)
    with pytest.raises(D.DataWindowError):
        D.load_symbol_bars("/nowhere.sqlite", "SPY")


def test_blind_oos_and_h2_loads_fail_closed() -> None:
    for start in (datetime(2026, 1, 21, tzinfo=ET), datetime(2026, 7, 21, tzinfo=ET)):
        with pytest.raises(OOSLockError):
            research_load("/does/not/exist.sqlite", "SPY", "1m", start=start, end=None,
                          purpose="performance", track=TRACK_INTRADAY, market="US")


def test_group_day_bars_is_input_order_invariant_and_rejects_duplicates() -> None:
    candles = [
        Candle.from_moment("SPY", "1m", datetime(2025, 3, 4, 9, 30 + i, tzinfo=ET),
                           100 + i, 101 + i, 99 + i, 100.5 + i, 10 + i, "USD", True)
        for i in range(20)
    ]
    shuffled = list(candles)
    random.Random(1).shuffle(shuffled)
    a, b = D.group_day_bars(candles, "SPY"), D.group_day_bars(shuffled, "SPY")
    assert np.array_equal(a[DAY].open, b[DAY].open, equal_nan=True)
    assert np.array_equal(a[DAY].volume, b[DAY].volume, equal_nan=True)
    with pytest.raises(D.DataDefectError):
        D.group_day_bars(candles + candles[:1], "SPY")


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def test_daily_aggregation_is_mean_of_same_day_trades() -> None:
    trades = [trade("B1", "SPY", DAY, 1.0), trade("B1", "QQQ", DAY, -0.5),
              trade("B1", "SPY", date(2025, 3, 5), 2.0)]
    assert daily_returns(trades) == [(DAY, 0.25), (date(2025, 3, 5), 2.0)]


def test_bootstrap_is_deterministic_and_matches_reference() -> None:
    values = list(np.random.default_rng(5).normal(0.3, 1.0, 37))
    p1, p2 = bootstrap_p_value(values), bootstrap_p_value(values)
    assert p1 == p2
    rng = np.random.default_rng(20261001)
    n, blocks = len(values), -(-len(values) // 5)
    starts = rng.integers(0, n, size=(10_000, blocks))
    hits = 0
    for row in starts:
        sample = [values[(s + k) % n] for s in row for k in range(5)][:n]
        hits += np.mean(sample) <= 0.34
    assert p1 == pytest.approx((hits + 1) / 10_001, abs=1e-12)
    assert bootstrap_p_value([5.0] * 10) == 1 / 10_001
    assert bootstrap_p_value([0.0] * 10) == 1.0
    assert bootstrap_p_value([]) == 1.0


def test_holm_8_step_down_with_cumulative_max() -> None:
    p = dict(zip(ARM_IDS, (0.001, 0.01, 0.02, 0.03, 0.04, 0.2, 0.5, 0.9)))
    adjusted = holm_adjust(p)
    assert [adjusted[arm] for arm in ARM_IDS] == pytest.approx(
        [0.008, 0.07, 0.12, 0.15, 0.16, 0.6, 1.0, 1.0]
    )
    p = dict(zip(ARM_IDS, (0.01, 0.0105, 0.9, 0.9, 0.9, 0.9, 0.9, 0.9)))
    adjusted = holm_adjust(p)
    assert adjusted["A1"] == pytest.approx(0.08) and adjusted["A2"] == pytest.approx(0.08)
    with pytest.raises(ValueError):
        holm_adjust({"A1": 0.1})


def recent_days(count: int) -> list[date]:
    return cal.trading_days(date(2025, 1, 2), date(2026, 1, 20))[:count]


def test_g4_split_uses_floor_half_in_trade_order() -> None:
    days = recent_days(5)
    # order: 0.5, 0.5 | 0.35, 0.35, 0.36 -> both halves > 0.34
    trades = [trade("B1", "SPY", d, g) for d, g in zip(days, (0.5, 0.5, 0.35, 0.35, 0.36))]
    stats = leg_statistics(list(reversed(trades)), ["SPY", "QQQ"])
    assert stats["first_half_trades"] == 2
    assert stats["first_half_mean"] == pytest.approx(0.5)
    assert stats["gates"]["G4"]
    trades = [trade("B1", "SPY", d, g) for d, g in zip(days, (0.5, 0.1, 2, 2, 2))]
    assert not leg_statistics(trades, ["SPY", "QQQ"])["gates"]["G4"]


def test_g5_drops_best_month_by_sum_not_mean() -> None:
    jan = [trade("B1", "SPY", d, 1.0) for d in cal.trading_days(date(2025, 1, 2), date(2025, 1, 31))[:10]]
    feb = [trade("B1", "SPY", date(2025, 2, 3), 3.0)]  # highest mean, lower sum
    mar = [trade("B1", "SPY", d, 0.0) for d in cal.trading_days(date(2025, 3, 3), date(2025, 3, 10))[:5]]
    stats = leg_statistics(jan + feb + mar, ["SPY", "QQQ"])
    assert stats["best_month_by_sum"] == "2025-01"
    assert stats["mean_excluding_best_month"] == pytest.approx(3.0 / 6)


def test_g7_drops_best_symbol_by_sum_and_is_na_for_single_symbol() -> None:
    days = recent_days(6)
    trades = [trade("D1", "SOXL", d, 1.0) for d in days[:4]]
    trades += [trade("D1", "TSLA", days[4], 2.0), trade("D1", "MU", days[5], 0.2)]
    stats = leg_statistics(trades, ["SOXL", "TSLA", "MU"])
    assert stats["best_symbol_by_sum"] == "SOXL"
    assert stats["mean_excluding_best_symbol"] == pytest.approx(1.1)
    assert stats["gates"]["G7"] is True
    single = leg_statistics(trades[:4], ["SOXL"])
    assert single["gates"]["G7"] is None and single["best_symbol_by_sum"] is None
    only_one_traded = leg_statistics(trades[:4], ["SOXL", "TSLA"])
    assert only_one_traded["gates"]["G7"] is False


def test_g8_recent_year_window_boundaries() -> None:
    inside = cal.trading_days(date(2025, 1, 2), date(2025, 3, 31))[:29]
    trades = [trade("B1", "SPY", d, 1.0) for d in inside]
    trades += [trade("B1", "SPY", date(2024, 12, 31), 1.0)]
    stats = leg_statistics(trades, ["SPY", "QQQ"])
    assert stats["recent_year_trades"] == 29 and not stats["gates"]["G8"]
    trades.append(trade("B1", "SPY", date(2026, 1, 20), 1.0))
    stats = leg_statistics(trades, ["SPY", "QQQ"])
    assert stats["recent_year_trades"] == 30 and stats["gates"]["G8"]
    flat = [trade("B1", "SPY", d, 0.34) for d in recent_days(30)]
    assert not leg_statistics(flat, ["SPY", "QQQ"])["gates"]["G8"]


def strong_trades(arm: str, symbols: tuple[str, ...], days: list[date]) -> list[A.Trade]:
    return [trade(arm, symbols[i % len(symbols)], day, 1.0) for i, day in enumerate(days)]


def empty_baseline() -> BaselineCache:
    return BaselineCache({}, [])


def test_verdict_shortlist_lists_all_passing_arms_and_samples_others() -> None:
    days = recent_days(120)
    trades = strong_trades("B1", ("SPY", "QQQ"), days) + strong_trades("C2", ("AAPL", "MSFT"), days)
    results, verdict = evaluate_arms(trades, empty_baseline())
    status = {r["arm_id"]: r["status"] for r in results}
    assert verdict == "SHORTLIST"
    assert status["B1"] == status["C2"] == "LONG_CANDIDATE"
    assert {status[a] for a in ("A1", "A2", "B2", "B3", "C1", "D1")} == {"SAMPLE"}
    b1 = next(r for r in results if r["arm_id"] == "B1")
    assert b1["bootstrap_p"] == 1 / 10_001 and b1["holm_p"] == pytest.approx(8 / 10_001)
    assert b1["inverse_leg"]["trade_count"] == 0
    assert b1["net_after_cost_pct_points"]["stress"] == pytest.approx(0.49)


@pytest.mark.parametrize(("non_inverse_days", "expected"), [(25, "LONG_CANDIDATE"), (24, "INVERSE_ONLY")])
def test_inverse_policy_requires_non_inverse_leg_alone(non_inverse_days, expected) -> None:
    days = recent_days(120)
    trades = strong_trades("B1", ("SOXS",), days)
    for day in days[:non_inverse_days]:
        trades += [trade("B1", "SPY", day, 1.0), trade("B1", "QQQ", day, 1.0)]
    results, verdict = evaluate_arms(trades, empty_baseline())
    b1 = next(r for r in results if r["arm_id"] == "B1")
    assert b1["screen_pass"]
    assert b1["status"] == expected
    assert b1["inverse_leg"]["trade_count"] == 120
    assert verdict == ("SHORTLIST" if expected == "LONG_CANDIDATE" else "NO_GROSS_EDGE")


def test_no_gross_edge_with_failures() -> None:
    days = recent_days(120)
    weak = [trade("B3", "SPY", d, 0.40) for d in days]
    results, verdict = evaluate_arms(weak, empty_baseline())
    b3 = next(r for r in results if r["arm_id"] == "B3")
    assert verdict == "NO_GROSS_EDGE" and b3["status"] == "FAIL" and not b3["all"]["gates"]["G3"]


def test_evaluation_is_input_order_invariant() -> None:
    days = recent_days(60)
    trades = strong_trades("B1", ("SPY", "SOXS", "QQQ"), days)
    trades += [trade("D1", "MU", d, -0.2) for d in days[:20]]
    shuffled = list(trades)
    random.Random(9).shuffle(shuffled)
    a = evaluate_arms(trades, empty_baseline())
    b = evaluate_arms(shuffled, empty_baseline())
    assert R.canonical_json_bytes({"x": a}) == R.canonical_json_bytes({"x": b})


def test_unconditional_baseline_uses_evaluated_valid_days_with_both_bars() -> None:
    d1, d2, d3 = date(2025, 3, 3), date(2025, 3, 4), date(2025, 3, 5)
    bars = {"SPY": {
        d1: flat_day("SPY", d1, overrides={600: (100, 100, 100, 100), 955: (101, 101, 101, 101)}),
        d2: flat_day("SPY", d2, overrides={600: (100, 100, 100, 100), 955: (99, 99, 99, 99)}),
        d3: flat_day("SPY", d3, drop=(600,), overrides={955: (150, 150, 150, 150)}),
    }}
    cache = BaselineCache(bars, [d1, d2, d3])
    assert cache.value("SPY", 600) == pytest.approx(0.0)
    results, _ = evaluate_arms([trade("B1", "SPY", d1, 1.0, minute=600)], cache)
    diag = next(r for r in results if r["arm_id"] == "B1")["diagnostics"]
    assert diag["unconditional_baseline_mean"] == pytest.approx(0.0)
    assert diag["trade_mean_minus_baseline"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# payload, identity, determinism, report, CLI
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def synthetic_payload_inputs():
    contract = load_contract()
    bars = random_universe(date(2024, 10, 1), date(2025, 2, 28))
    evaluated = tuple(cal.trading_days(date(2025, 1, 2), date(2025, 2, 28)))
    windows = D.Windows(evaluated, (), evaluated)
    identity = {"snapshot_file": {"expected": "x", "actual": "x", "status": "match"}}
    return contract, windows, identity, bars


def test_payload_is_byte_deterministic_and_markdown_matches_json(synthetic_payload_inputs) -> None:
    contract, windows, identity, bars = synthetic_payload_inputs
    first = R.build_payload(contract, windows, identity, bars)
    second = R.build_payload(contract, windows, identity, bars)
    assert R.canonical_json_bytes(first) == R.canonical_json_bytes(second)
    assert first["verdict"] in {"SHORTLIST", "NO_GROSS_EDGE"}
    assert first["determinism"]["evaluation_rerun_reversed_input_identical"] is True
    assert [arm["arm_id"] for arm in first["arms"]] == list(ARM_IDS)
    assert sum(arm["all"]["trade_count"] for arm in first["arms"]) > 0
    body = dict(first)
    digest = body.pop("payload_sha256")
    import hashlib
    assert hashlib.sha256(R.canonical_json_bytes(body)).hexdigest() == digest
    reparsed = json.loads(R.canonical_json_bytes(first))
    assert R.canonical_json_bytes(reparsed) == R.canonical_json_bytes(first)
    report = render_markdown(reparsed)
    assert render_markdown(reparsed) == report
    assert f"verdict: **{first['verdict']}**" in report
    rows = {line.split("|")[1].strip(): line for line in report.splitlines()
            if line.startswith("| ") and line.split("|")[2].strip() in
            {"LONG_CANDIDATE", "INVERSE_ONLY", "SAMPLE", "FAIL"}}
    for arm in first["arms"]:
        assert rows[arm["arm_id"]] == arm_row(arm)
        cells = [cell.strip() for cell in rows[arm["arm_id"]].split("|")[1:-1]]
        assert int(cells[2]) == arm["all"]["trade_count"]
        assert int(cells[3]) == arm["all"]["trading_days"]


def test_quality_breach_is_inconclusive_without_arm_statistics(synthetic_payload_inputs) -> None:
    contract, windows, identity, bars = synthetic_payload_inputs
    partial = {symbol: bars[symbol] for symbol in A.UNIVERSE[:18]}
    payload = R.build_payload(contract, windows, identity, partial)
    assert payload["verdict"] == "INCONCLUSIVE_DATA"
    assert payload["arms"] == [] and payload["data_quality"]["breach"]
    assert payload["data_quality"]["invalid_symbol_days"] == 2 * len(windows.evaluated_days)
    one_missing = {symbol: dict(days) for symbol, days in bars.items()}
    del one_missing["SPY"][windows.evaluated_days[0]]
    payload = R.build_payload(contract, windows, identity, one_missing)
    assert payload["verdict"] != "INCONCLUSIVE_DATA"


def test_identity_failure_raises_setup_error_before_any_load(monkeypatch, tmp_path) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("prices must not be loaded after an identity failure")

    monkeypatch.setattr(R, "load_symbol_bars", forbidden)
    operating = tmp_path / "market_history.sqlite"
    operating.write_bytes(b"not a snapshot")
    with pytest.raises(R.SetupError, match="basename"):
        R.run_screen(snapshot_path=str(operating), raw_root=str(tmp_path),
                     manifest_path=R.default_manifest_path())
    fake = tmp_path / "p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite"
    fake.write_bytes(b"tampered")
    with pytest.raises(R.SetupError):
        R.run_screen(snapshot_path=str(fake), raw_root=str(tmp_path),
                     manifest_path=R.default_manifest_path())


def test_write_new_and_cli_refuse_to_overwrite(monkeypatch, tmp_path) -> None:
    target = tmp_path / "result.json"
    write_new(str(target), b"{}\n")
    with pytest.raises(FileExistsError):
        write_new(str(target), b"{}\n")
    assert not (tmp_path / "result.json.partial").exists()

    def forbidden(**kwargs):
        raise AssertionError("must not run when a result exists")

    monkeypatch.setattr(cli, "run_screen", forbidden)
    code = cli.main([
        "--snapshot-db", "x", "--raw-root", "y",
        "--output-json", str(target), "--output-md", str(tmp_path / "result.md"),
    ])
    assert code == 2


# ---------------------------------------------------------------------------
# review round 1: setup isolation, CLI modes, boundaries, numeric references
# ---------------------------------------------------------------------------
SNAPSHOT_NAME = "p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite"


def cli_args(tmp_path, snapshot, raw_root, *extra):
    return ["--snapshot-db", str(snapshot), "--raw-root", str(raw_root),
            "--output-json", str(tmp_path / "out.json"),
            "--output-md", str(tmp_path / "out.md"), *extra]


def no_outputs(tmp_path) -> bool:
    return not (tmp_path / "out.json").exists() and not (tmp_path / "out.md").exists()


def test_cli_missing_snapshot_writes_nothing(tmp_path, capsys) -> None:
    code = cli.main(cli_args(tmp_path, tmp_path / "absent" / SNAPSHOT_NAME, tmp_path))
    assert code == 1 and no_outputs(tmp_path)
    assert "setup failed" in capsys.readouterr().err


def test_cli_wrong_raw_root_writes_nothing(tmp_path, capsys) -> None:
    snapshot = tmp_path / SNAPSHOT_NAME
    snapshot.write_bytes(b"junk")
    code = cli.main(cli_args(tmp_path, snapshot, tmp_path / "no_raw_root"))
    assert code == 1 and no_outputs(tmp_path)
    assert "raw snapshot root missing" in capsys.readouterr().err


def test_cli_preflight_writes_nothing(monkeypatch, tmp_path, capsys) -> None:
    ok_identity = {"snapshot_file": {"status": "match"}}
    monkeypatch.setattr(cli, "prepare", lambda **kw: (None, None, ok_identity, {"git_commit": "c"}))
    monkeypatch.setattr(cli, "run_screen", lambda **kw: pytest.fail("preflight must not run"))
    code = cli.main(cli_args(tmp_path, tmp_path / SNAPSHOT_NAME, tmp_path, "--preflight"))
    assert code == 0 and no_outputs(tmp_path)
    assert json.loads(capsys.readouterr().out)["preflight"] == "ok"
    assert not any(tmp_path.iterdir())
    monkeypatch.undo()
    code = cli.main(cli_args(tmp_path, tmp_path / SNAPSHOT_NAME, tmp_path, "--preflight"))
    assert code == 1 and not any(tmp_path.iterdir())


def test_cli_official_setup_error_writes_nothing(monkeypatch, tmp_path) -> None:
    def dirty(**kwargs):
        raise R.SetupError("worktree is not clean")

    monkeypatch.setattr(cli, "run_screen", dirty)
    assert cli.main(cli_args(tmp_path, tmp_path / SNAPSHOT_NAME, tmp_path)) == 1
    assert no_outputs(tmp_path)


def test_dirty_tree_is_a_setup_error(monkeypatch) -> None:
    outputs = {"rev-parse": "abc\n", "status": "?? new.py\n"}
    monkeypatch.setattr(R, "_git", lambda *args: outputs[args[0]])
    with pytest.raises(R.SetupError, match="not clean"):
        R.implementation_provenance(require_clean=True)
    assert R.implementation_provenance(require_clean=False) == {
        "git_commit": "abc", "worktree_clean": False}
    outputs["status"] = ""
    assert R.implementation_provenance(require_clean=True)["worktree_clean"] is True


def test_snapshot_change_during_run_raises(monkeypatch, synthetic_payload_inputs) -> None:
    contract, windows, _, bars = synthetic_payload_inputs
    ident = {"snapshot_file": {"expected": "a", "actual": "a", "status": "match"}}
    monkeypatch.setattr(R, "prepare", lambda **kw: (contract, windows, ident, {"git_commit": "c"}))
    monkeypatch.setattr(R, "load_all", lambda path: bars)
    monkeypatch.setattr(R, "file_sha256", lambda path: "b")
    with pytest.raises(R.SetupError, match="changed"):
        R.run_screen(snapshot_path="x", raw_root="y", manifest_path="z")
    monkeypatch.setattr(R, "file_sha256", lambda path: "a")
    result = R.run_screen(snapshot_path="x", raw_root="y", manifest_path="z")
    assert result["implementation"] == {"git_commit": "c"}
    assert "implementation commit: `c`" in render_markdown(result)


def test_data_defect_after_identity_is_inconclusive(monkeypatch, synthetic_payload_inputs) -> None:
    contract, windows, _, _ = synthetic_payload_inputs
    ident = {"snapshot_file": {"expected": "a", "actual": "a", "status": "match"}}
    monkeypatch.setattr(R, "prepare", lambda **kw: (contract, windows, ident, {"git_commit": "c"}))

    def defect(path):
        raise D.DataDefectError("duplicate regular minute")

    monkeypatch.setattr(R, "load_all", defect)
    monkeypatch.setattr(R, "file_sha256", lambda path: "a")
    result = R.run_screen(snapshot_path="x", raw_root="y", manifest_path="z")
    assert result["verdict"] == "INCONCLUSIVE_DATA" and result["arms"] == []


def test_count_conditions_mode_prints_counts_only(monkeypatch, tmp_path, capsys,
                                                  synthetic_payload_inputs) -> None:
    _, windows, _, bars = synthetic_payload_inputs
    monkeypatch.setattr(cli, "prepare", lambda **kw: (None, windows, {}, {"git_commit": "c"}))
    monkeypatch.setattr(cli, "load_all", lambda path: bars)
    code = cli.main(cli_args(tmp_path, tmp_path / SNAPSHOT_NAME, tmp_path, "--count-conditions"))
    assert code == 0 and not any(tmp_path.iterdir())
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert set(payload) == {"counts", "definitions", "evaluated_days", "implementation"}
    for name, value in payload["counts"].items():
        assert set(value) == {"symbol_days", "days"}, name
    assert "gross" not in out and "pct_points" not in out


def test_condition_counts_cover_every_traded_signal(synthetic_payload_inputs) -> None:
    _, windows, _, bars = synthetic_payload_inputs
    counts = condition_counts(bars, windows.evaluated_days)["counts"]
    extraction = A.extract_trades(bars, windows.evaluated_days)
    for arm, name in (("A2", "A2"), ("B1", "B1_nr7"), ("B2", "B2_inside_nr4"),
                      ("B3", "B3_close_gt_sma20"), ("C2", "C2_rsi3_roc1_lt30"),
                      ("D1", "D_k0.6")):
        traded = sum(1 for t in extraction.trades if t.arm_id == arm)
        assert counts[name]["symbol_days"] >= traded > 0, arm
    a1_trades = sum(1 for t in extraction.trades if t.arm_id == "A1")
    assert counts["A1_pooled"]["symbol_days"] >= a1_trades
    assert counts["D_k1.0"]["symbol_days"] <= counts["D_k0.6"]["symbol_days"]


def test_holiday_regular_bar_is_a_data_defect() -> None:
    candle = Candle.from_moment("SPY", "1m", datetime(2025, 7, 4, 9, 30, tzinfo=ET),
                                1, 1, 1, 1, 1, "USD", True)
    with pytest.raises(D.DataDefectError, match="non-trading"):
        D.group_day_bars([candle], "SPY")


def signal_day(symbol: str, open_price: float, close_1444: float) -> D.DayBars:
    return flat_day(symbol, DAY, price=open_price, overrides={
        M("14:44"): (open_price, max(open_price, close_1444), min(open_price, close_1444),
                     close_1444),
    })


@pytest.mark.parametrize(("leg", "open_price", "close", "bought"), [
    (0, 400.00, 406.00, "TQQQ"), (0, 400.00, 394.00, "SQQQ"),
    (1, 20.00, 20.90, "SOXL"), (1, 20.00, 19.10, "SOXS"),
])
def test_a1_exact_cent_boundaries_trigger(leg, open_price, close, bought) -> None:
    traded = {s: flat_day(s, DAY) for s in ("TQQQ", "SQQQ", "SOXS")}
    signal = signal_day(A.A1_LEGS[leg][0], open_price, close)
    traded["SOXL"] = signal
    result = A.a1_leg(A.A1_LEGS[leg], signal, traded)
    assert isinstance(result, A.Trade) and result.symbol == bought


def test_a2_exact_cent_boundary_triggers() -> None:
    assert isinstance(A.a2(signal_day("NVDA", 20.00, 20.60)), A.Trade)
    assert A.a2(signal_day("NVDA", 20.00, 20.59)) == A.NO_SIGNAL


def test_g7_fails_when_all_non_inverse_a1_trades_are_soxl() -> None:
    results, _ = evaluate_arms(strong_trades("A1", ("SOXL",), recent_days(120)), empty_baseline())
    a1 = next(r for r in results if r["arm_id"] == "A1")
    assert a1["non_inverse_leg"]["gates"]["G7"] is False
    assert a1["all"]["gates"]["G7"] is False
    assert a1["status"] != "LONG_CANDIDATE"


def test_rsi2_mixed_series_reference_value() -> None:
    values = rsi([10, 11, 10.5, 11.5, 11, 12], 2)
    # gains 1,0,1,0,1 / losses 0,.5,0,.5,0: seed .5/.25 -> .75/.125 -> .375/.3125
    # -> .6875/.15625; RSI = 100 * 4.4 / 5.4
    assert values[:5] == [None] * 5
    assert values[5] == pytest.approx(100 * 4.4 / 5.4)


def test_rsi3_roc1_reference_value() -> None:
    closes = [100.0]
    for i in range(9):
        closes.append(closes[-1] * (1.1 if i % 2 == 0 else 0.9))
    days = cal.trading_days(date(2025, 1, 2), date(2025, 1, 31))
    bars = [DailyBar(days[i], c, c, c, c) for i, c in enumerate(closes)]
    values = compute_indicators(bars)
    # ROC1 = +.1,-.1,...; Wilder(3) on ROC changes in exact fractions -> 59.12208504801097
    assert values[bars[-1].day].rsi3_roc1 == pytest.approx(59.12208504801097, abs=1e-9)
    assert values[bars[-2].day].rsi3_roc1 is None


def tr_bars(true_ranges: list[float]) -> list[DailyBar]:
    return [DailyBar(date(2024, 1, 1), 100, 100 + tr / 2, 100 - tr / 2, 100)
            for tr in true_ranges]


def test_atr10_and_atr14_reference_values() -> None:
    atr14 = atr(tr_bars([float(i) for i in range(1, 15)] + [2.0] * 28), 14)
    assert atr14[40] is None
    assert atr14[41] == pytest.approx(2 + 5.5 * (13 / 14) ** 28)
    atr10 = atr(tr_bars([float(i) for i in range(1, 11)] + [2.0] * 20), 10)
    assert atr10[28] is None
    assert atr10[29] == pytest.approx(2 + 3.5 * 0.9 ** 20)


def test_nr7_nr4_inside_reference_values() -> None:
    ranges = [5, 6, 7, 4, 8, 9, 10, 3, 3]
    days = cal.trading_days(date(2025, 1, 2), date(2025, 1, 31))
    bars = [DailyBar(days[i], 100, 100 + r / 2, 100 - r / 2, 100) for i, r in enumerate(ranges)]
    values = [compute_indicators(bars)[bar.day] for bar in bars]
    assert [v.nr4 for v in values] == [None, None, None, True, False, False, False, True, True]
    assert [v.nr7 for v in values] == [None] * 6 + [False, True, True]
    assert [v.inside for v in values] == [
        None, False, False, True, False, False, False, True, False]


def test_stretch10_sma20_sma200_reference_values() -> None:
    days = cal.trading_days(date(2024, 1, 2), date(2025, 12, 31))[:250]
    bars = []
    for i, day in enumerate(days):
        up, down = (i % 3) + 1.0, (i % 4) + 0.5
        bars.append(DailyBar(day, 1000.0, 1000.0 + up, 1000.0 - down, float(i + 1)))
    values = compute_indicators(bars)
    stretch = [min((i % 3) + 1.0, (i % 4) + 0.5) for i in range(240, 250)]
    assert values[days[249]].stretch10 == pytest.approx(sum(stretch) / 10)
    assert values[days[8]].stretch10 is None
    assert values[days[18]].sma20 is None and values[days[19]].sma20 == pytest.approx(10.5)
    assert values[days[100]].sma20 == pytest.approx(101 - 9.5)
    assert values[days[198]].sma200 is None and values[days[199]].sma200 == pytest.approx(100.5)
    assert values[days[249]].sma200 == pytest.approx(150.5)


def test_b_and_c1_scan_window_ends_at_1530() -> None:
    prev = ind(nr7=True, stretch10=0.5)
    at_1530 = A.b_arm("B1", flat_day("SPY", DAY, overrides={M("15:30"): (100, 100.6, 100, 100)}),
                      prev)
    assert at_1530.entry_minute == M("15:30")
    assert A.b_arm("B1", flat_day("SPY", DAY, overrides={M("15:31"): (100, 100.6, 100, 100)}),
                   prev) == A.NO_FILL
    c1_prev = ind(rsi2=3.0, sma200=90.0, atr10=2.0)
    c1 = A.c1(flat_day("SPY", DAY, overrides={M("15:30"): (100, 100, 98.5, 100)}), c1_prev)
    assert c1.entry_minute == M("15:30")
    assert A.c1(flat_day("SPY", DAY, overrides={M("15:31"): (100, 100, 98.5, 100)}),
                c1_prev) == A.NO_FILL


@pytest.mark.parametrize(("minute", "expected_entry"), [
    ("09:59", None), ("15:01", None), ("15:00", "15:01"), ("10:00", "10:01"),
])
def test_d1_signal_window_is_1000_to_1500(minute, expected_entry) -> None:
    bars = flat_day("TSLA", DAY, overrides={M(minute): (100, 100, 99.0, 99.0)})
    result = A.d1(bars, ind(atr14=1.0))
    if expected_entry is None:
        assert result == A.NO_SIGNAL
    else:
        assert result.entry_minute == M(expected_entry)


def test_c2_cannot_fill_before_1030_and_stops_at_first_hour_low() -> None:
    prev = ind(rsi3_roc1=10.0)
    high_at_1029 = flat_day("SPY", DAY, overrides={M("10:29"): (100, 101, 100, 100.9)})
    assert A.c2(high_at_1029, prev) == A.NO_FILL
    bars = flat_day("SPY", DAY, overrides={
        M("10:10"): (100, 100, 99.0, 100),
        M("10:29"): (100, 101, 100, 100.9),
        M("10:40"): (100.9, 101.2, 100.8, 101.1),
        M("11:00"): (99.5, 99.6, 98.8, 99.0),
    })
    result = A.c2(bars, prev)
    assert (result.entry_minute, result.entry_price) == (M("10:40"), 101)
    assert (result.exit_minute, result.exit_price, result.exit_reason) == (
        M("11:00"), 99.0, "stop")


def test_a1_two_legs_same_day_are_averaged_into_one_daily_return() -> None:
    bars = {
        "QQQ": {DAY: signal_day("QQQ", 400.0, 406.0)},
        "TQQQ": {DAY: flat_day("TQQQ", DAY, overrides={
            M("14:45"): (50, 50, 50, 50), 955: (51, 51, 51, 51)})},
        "SOXL": {DAY: flat_day("SOXL", DAY, price=20.0, overrides={
            M("14:44"): (20, 20.9, 20, 20.9), M("14:45"): (21, 21, 21, 21),
            955: (20.79, 20.79, 20.79, 20.79)})},
    }
    trades = [t for t in A.extract_trades(bars, [DAY]).trades if t.arm_id == "A1"]
    assert sorted(t.symbol for t in trades) == ["SOXL", "TQQQ"]
    ((day, value),) = daily_returns(trades)
    assert day == DAY and value == pytest.approx((2.0 + (20.79 / 21 - 1) * 100) / 2)


def test_early_close_t_minus_1_blocks_b_c_d() -> None:
    monday = date(2024, 12, 2)
    days = series_days("TSLA", date(2024, 6, 1), monday)
    days[date(2024, 11, 29)] = flat_day("TSLA", date(2024, 11, 29), price=130.0)
    extraction = A.extract_trades({"TSLA": days}, [monday])
    for arm_id in ("B1", "B2", "B3", "C1", "C2", "D1"):
        assert extraction.skip_counts[arm_id].get(A.PREV_INVALID) == 1
    assert not [t for t in extraction.trades if t.symbol == "TSLA"]
