"""ORB 구간·fold·캘린더 테스트 — 이슈 #91 (4-A).

사전등록 §3 의 690/20/670/125 거래일 경계와 10-fold 분할(F01=103일, F02~F10=63일)이 캘린더에서
재현되는지, 그리고 계약 경계가 변조되면 `ContractIntegrityError` 로 죽는지 확인한다.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from src.history.exchange_calendar import CalendarRangeError
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
    """R5-3 — `folds()` 는 6개 이상의 raise 지점을 가진다(개수·블록없음·시작·끝·거래일수·합산).
    `match=` 로 어느 지점인지 고정한다."""
    raw_contract["folds"]["boundaries"][1]["trading_days"] = 999
    with pytest.raises(ContractIntegrityError, match="거래일수 재계산 불일치"):
        w.folds(raw_contract)


def test_folds_raise_when_boundary_end_date_tampered(raw_contract):
    raw_contract["folds"]["boundaries"][0]["end"] = "2023-10-14"   # 하루 밀림
    with pytest.raises(ContractIntegrityError, match="끝 재계산 불일치"):
        w.folds(raw_contract)


def test_folds_raise_when_a_boundary_row_is_dropped(raw_contract):
    raw_contract["folds"]["boundaries"].pop()   # F10 삭제 → 개수 불일치
    with pytest.raises(ContractIntegrityError, match="fold 개수 재계산 불일치"):
        w.folds(raw_contract)


def test_folds_raise_when_block_trading_days_source_value_tampered(raw_contract):
    """F4(lane3 Medium) — `windows.py` 의 `blk = int(f["block_trading_days"])` 를 63 으로
    하드코딩해도(실제 계약값과 같으므로) 기존 boundary-row 변조 테스트들은 이 특정 read 를
    거치지 않아 통과했다. `block_trading_days` 자체를 바꾼 fixture 로 `folds()` 가 실제로
    계약을 다시 읽는지(하드코딩이 아닌지) 확인한다. (block_trading_days=999 는 rebuilt 블록을
    1개로 무너뜨려 "fold 개수 재계산 불일치"와 동일한 지점을 겨냥한다 — 위 드롭 테스트와
    같은 지점이지만 다른 변조 경로다.)"""
    raw_contract["folds"]["block_trading_days"] = 999
    with pytest.raises(ContractIntegrityError, match="fold 개수 재계산 불일치"):
        w.folds(raw_contract)


def test_development_data_days_raise_when_trading_day_count_tampered(raw_contract):
    raw_contract["windows"]["development_data_trading_days"] = 691
    with pytest.raises(ContractIntegrityError):
        w.development_data_days(raw_contract)


# --------------------------------------------------------------------------
# M4(lane4) — `warmup_trading_days` 하드코딩 생존 방지(`block_trading_days` 형제 테스트 F4 와
# 같은 유형: 실제 계약값이 20 이므로 하드코딩해도 대부분의 기존 테스트는 통과한다).
# --------------------------------------------------------------------------
def test_development_eval_days_raise_when_warmup_trading_days_source_value_tampered(
        raw_contract):
    """`development_eval_days` 의 `n = int(w["warmup_trading_days"])` 를 20 으로 하드코딩해도
    실제 계약값이 20 이므로 이 read 를 거치지 않는 기존 테스트는 통과한다.
    `warmup_trading_days` 자체를 바꾼 fixture 로 실제로 계약을 다시 읽는지 확인한다 —
    올바른 코드는 분할 지점이 바뀌어 `development_eval_trading_days`/`development_eval_start`
    (둘 다 변경하지 않음)와 충돌해 예외를 낸다. 하드코딩이면 분할 지점이 그대로라 통과해
    변조가 조용히 생존한다."""
    raw_contract["windows"]["warmup_trading_days"] = 21
    with pytest.raises(ContractIntegrityError, match="development_eval_trading_days 재계산 불일치"):
        w.development_eval_days(raw_contract)


def test_warmup_days_raise_when_warmup_trading_days_source_value_tampered(raw_contract):
    """`warmup_days`(`development_data_days(contract)[:n]`)는 다른 계약값과 대조하지 않으므로
    출력 길이로 직접 확인한다 — 하드코딩(20)이면 탬퍼된 값(21)과 무관하게 20 이 나온다."""
    raw_contract["windows"]["warmup_trading_days"] = 21
    assert len(w.warmup_days(raw_contract)) == 21


def test_development_eval_days_raise_when_eval_start_tampered(raw_contract):
    raw_contract["windows"]["development_eval_start"] = "2023-05-19"
    with pytest.raises(ContractIntegrityError, match="development_eval_start 재계산 불일치"):
        w.development_eval_days(raw_contract)


def test_development_eval_days_raise_when_eval_end_tampered(raw_contract):
    """F5(lane3 Medium) — 시작 경계 변조 테스트는 있었는데 끝 경계 변조가 없어
    `development_eval_days` 의 끝 경계 대조를 제거해도 통과했다(비대칭 커버리지)."""
    raw_contract["windows"]["development_eval_end"] = "2026-01-19"
    with pytest.raises(ContractIntegrityError, match="development_eval_end 재계산 불일치"):
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


# --------------------------------------------------------------------------
# previous_trading_day — M2(lane4) §5.1 "직전 거래일은 exchange_calendar.trading_days 기준"
# --------------------------------------------------------------------------
def test_previous_trading_day_simple_weekday():
    assert w.previous_trading_day(date(2024, 3, 5)) == date(2024, 3, 4)   # 화 -> 월


def test_previous_trading_day_skips_weekend():
    assert w.previous_trading_day(date(2024, 3, 4)) == date(2024, 3, 1)   # 월 -> 금(주말 건너뜀)


def test_previous_trading_day_skips_holiday_and_weekend():
    """2024-01-15(MLK Day, 휴장) 뒤 첫 거래일(01-16, 화)의 직전 거래일은 01-12(금) 이어야
    한다(01-13/14 주말 + 01-15 휴장을 함께 건너뛴다)."""
    assert w.previous_trading_day(date(2024, 1, 16)) == date(2024, 1, 12)


def test_previous_trading_day_skips_early_close_day_is_still_a_trading_day():
    """조기폐장일은 휴장이 아니라 정상 거래일이다 — 건너뛰지 않는다.
    2024-07-05(금) 의 직전 거래일은 2024-07-03(수, 조기폐장) 이다(07-04 목요일 독립기념일
    휴장만 건너뛴다)."""
    assert w.previous_trading_day(date(2024, 7, 5)) == date(2024, 7, 3)


def test_previous_trading_day_propagates_calendar_range_error_at_the_boundary():
    """R4-7(lane3 q-2) — docstring 이 "캘린더 artifact 커버 구간 밖으로 벗어나면
    `cal.is_trading_day` 가 그대로 `CalendarRangeError` 를 던진다(fail-closed)"고 적었는데,
    그 예외를 삼키고 전날을 반환하는 변조가 테스트 없이 생존했다. 2023-01-03 의 직전
    거래일을 역방향으로 걸어가면 2023-01-02(신정 관찰 휴장)·2023-01-01(신정, 주말)을 지나
    캘린더 커버 구간(2023-01-01~) 밖인 2022-12-31 에서 실제로 예외가 전파돼야 한다."""
    with pytest.raises(CalendarRangeError):
        w.previous_trading_day(date(2023, 1, 3))
