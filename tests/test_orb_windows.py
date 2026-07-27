"""ORB 구간·fold·캘린더 테스트 — 이슈 #91 (4-A).

사전등록 §3 의 690/20/670/125 거래일 경계와 10-fold 분할(F01=103일, F02~F10=63일)이 캘린더에서
재현되는지, 그리고 계약 경계가 변조되면 `ContractIntegrityError` 로 죽는지 확인한다.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from src.models import MARKET_US, Candle
from src.research.orb import windows as w
from src.research.orb.contract import ContractIntegrityError, load_contract

ET = ZoneInfo("America/New_York")


@pytest.fixture(scope="module")
def contract() -> dict:
    return load_contract()


@pytest.fixture()
def raw_contract() -> dict:
    """변조용 평범한(mappingproxy 아닌) dict 사본 — 실제 tracked JSON 에서 새로 읽는다."""
    import os
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo, "docs", "research", "PREREG_P0_US30_ORB_V1.json"),
             encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# 690 / 20 / 670 / 125 거래일 수
# --------------------------------------------------------------------------
def test_trading_day_counts(contract):
    assert len(w.development_data_days(contract)) == 690
    assert len(w.warmup_days(contract)) == 20
    assert len(w.development_eval_days(contract)) == 670
    assert len(w.blind_oos_days(contract)) == 125


def test_warmup_plus_eval_equals_data_days_no_overlap(contract):
    dd = w.development_data_days(contract)
    wu = w.warmup_days(contract)
    ev = w.development_eval_days(contract)
    assert wu + ev == dd
    assert set(wu) & set(ev) == set()
    assert len(set(dd)) == len(dd)          # 중복 없음


def test_eval_window_boundaries(contract):
    ev = w.development_eval_days(contract)
    assert ev[0] == date(2023, 5, 18)
    assert ev[-1] == date(2026, 1, 20)


# --------------------------------------------------------------------------
# fold 10개, 경계 일치, F01=103일 · F02~F10=63일
# --------------------------------------------------------------------------
def test_folds_count_and_lengths(contract):
    folds = w.folds(contract)
    assert len(folds) == 10
    assert folds[0].name == "F01" and folds[0].trading_days == 103
    for f in folds[1:]:
        assert f.trading_days == 63
    assert [f.name for f in folds] == [f"F{i:02d}" for i in range(1, 11)]


def test_folds_match_contract_boundaries_exactly(contract):
    folds = w.folds(contract)
    boundaries = contract["folds"]["boundaries"]
    for got, want in zip(folds, boundaries):
        assert got.name == want["fold"]
        assert got.start.isoformat() == want["start"]
        assert got.end.isoformat() == want["end"]
        assert got.trading_days == want["trading_days"]


def test_folds_tile_the_eval_window_without_gap_or_overlap(contract):
    folds = w.folds(contract)
    ev = w.development_eval_days(contract)
    covered: list[date] = []
    for f in folds:
        block = [d for d in ev if f.start <= d <= f.end]
        assert len(block) == f.trading_days
        covered.extend(block)
    assert covered == list(ev)               # 순서 보존, 빈틈·중복 없이 정확히 덮음
    assert len(set(covered)) == len(ev)


def test_fold_of_maps_every_eval_day_to_exactly_one_fold(contract):
    folds = w.folds(contract)
    ev = w.development_eval_days(contract)
    for d in (ev[0], ev[-1], folds[4].start, folds[4].end):
        name = w.fold_of(d, folds)
        assert name is not None
    # 평가 창 밖 날짜는 None.
    assert w.fold_of(date(2020, 1, 2), folds) is None


# --------------------------------------------------------------------------
# 조기폐장일
# --------------------------------------------------------------------------
def test_early_close_days_development_and_oos(contract):
    ev = w.development_eval_days(contract)
    oos = w.blind_oos_days(contract)
    dev_ec = w.early_close_days_in(ev)
    oos_ec = w.early_close_days_in(oos)
    assert len(dev_ec) == 8
    assert len(oos_ec) == 0
    assert tuple(d.isoformat() for d in dev_ec) == tuple(
        contract["folds"]["development_early_close_days"])


# --------------------------------------------------------------------------
# development 마지막 날 < blind OOS 첫 날
# --------------------------------------------------------------------------
def test_development_ends_before_oos_starts(contract):
    ev = w.development_eval_days(contract)
    oos = w.blind_oos_days(contract)
    assert ev[-1] < oos[0]


# --------------------------------------------------------------------------
# regular_close_minute_checked
# --------------------------------------------------------------------------
def test_regular_close_minute_checked_normal_and_early_close(contract):
    assert w.regular_close_minute_checked(date(2024, 3, 4)) == 16 * 60
    assert w.regular_close_minute_checked(date(2024, 7, 3)) == 13 * 60   # 조기폐장


def test_regular_close_minute_checked_raises_on_non_trading_day():
    with pytest.raises(ValueError):
        w.regular_close_minute_checked(date(2024, 1, 1))   # 신정, 휴장일


# --------------------------------------------------------------------------
# 계약 fold 경계를 변조한 fixture → ContractIntegrityError (mutation 가드 증명)
# --------------------------------------------------------------------------
def test_folds_raise_when_boundary_trading_days_tampered(raw_contract):
    raw_contract["folds"]["boundaries"][1]["trading_days"] = 999
    with pytest.raises(ContractIntegrityError):
        w.folds(raw_contract)


def test_folds_raise_when_boundary_end_date_tampered(raw_contract):
    raw_contract["folds"]["boundaries"][0]["end"] = "2023-10-14"   # 하루 밀림
    with pytest.raises(ContractIntegrityError):
        w.folds(raw_contract)


def test_folds_raise_when_a_boundary_row_is_dropped(raw_contract):
    raw_contract["folds"]["boundaries"].pop()   # F10 삭제 → 개수 불일치
    with pytest.raises(ContractIntegrityError):
        w.folds(raw_contract)


def test_folds_raise_when_block_trading_days_source_value_tampered(raw_contract):
    """F4(lane3 Medium) — `windows.py` 의 `blk = int(f["block_trading_days"])` 를 63 으로
    하드코딩해도(실제 계약값과 같으므로) 기존 boundary-row 변조 테스트들은 이 특정 read 를
    거치지 않아 통과했다. `block_trading_days` 자체를 바꾼 fixture 로 `folds()` 가 실제로
    계약을 다시 읽는지(하드코딩이 아닌지) 확인한다."""
    raw_contract["folds"]["block_trading_days"] = 999
    with pytest.raises(ContractIntegrityError):
        w.folds(raw_contract)


def test_development_data_days_raise_when_trading_day_count_tampered(raw_contract):
    raw_contract["windows"]["development_data_trading_days"] = 691
    with pytest.raises(ContractIntegrityError):
        w.development_data_days(raw_contract)


def test_development_eval_days_raise_when_eval_start_tampered(raw_contract):
    raw_contract["windows"]["development_eval_start"] = "2023-05-19"
    with pytest.raises(ContractIntegrityError):
        w.development_eval_days(raw_contract)


def test_development_eval_days_raise_when_eval_end_tampered(raw_contract):
    """F5(lane3 Medium) — 시작 경계 변조 테스트는 있었는데 끝 경계 변조가 없어
    `development_eval_days` 의 끝 경계 대조를 제거해도 통과했다(비대칭 커버리지)."""
    raw_contract["windows"]["development_eval_end"] = "2026-01-19"
    with pytest.raises(ContractIntegrityError):
        w.development_eval_days(raw_contract)


def test_blind_oos_days_raise_when_trading_day_count_tampered(raw_contract):
    raw_contract["windows"]["blind_oos_trading_days"] = 126
    with pytest.raises(ContractIntegrityError):
        w.blind_oos_days(raw_contract)


# --------------------------------------------------------------------------
# minute_of_day — D3(독립 리뷰 정리 지적) 단일 계산 지점
# --------------------------------------------------------------------------
def _bar(hh: int, mm: int) -> Candle:
    moment = datetime(2024, 3, 4, hh, mm, tzinfo=ET)
    return Candle.from_moment(symbol="AAPL", interval="1m", moment=moment,
                              o=1, h=1, low=1, c=1, v=1,
                              currency="USD", adjusted=True, market=MARKET_US)


def test_minute_of_day_hand_calculated():
    assert w.minute_of_day(_bar(9, 30)) == 9 * 60 + 30
    assert w.minute_of_day(_bar(0, 0)) == 0
    assert w.minute_of_day(_bar(13, 0)) == 13 * 60
    assert w.minute_of_day(_bar(23, 59)) == 23 * 60 + 59
