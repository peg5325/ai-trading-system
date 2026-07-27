"""ORB 구간·fold·캘린더 — 이슈 #91 (4-A).

모든 거래일은 `src.history.exchange_calendar` 로 **재계산**하고, 계약 JSON 의 값과 대조해
불일치면 `ContractIntegrityError` 를 던진다. 계약에 적힌 숫자를 그냥 믿지 않는다 — 캘린더
artifact 가 바뀌면(NYSE 공지 갱신 등) 조용히 썩지 않고 즉시 실패해야 한다(사전등록 §14.6
"캘린더 파생" 부류 / §3.2).

fold 는 사전등록 §3.2 규칙(연속 63 거래일 블록, development 평가 창의 끝에 정렬해 뒤에서부터
자르고, 앞쪽 나머지는 가장 이른 fold 에 흡수)으로 재생성하고 계약 경계와 대조한다.

경계는 **양끝 포함(inclusive)** — 사전등록 §3.3.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from src.history import exchange_calendar as cal
from src.models import Candle
from src.research.orb.contract import ContractIntegrityError


@dataclass(frozen=True)
class Fold:
    name: str          # "F01" .. "F10"
    start: date
    end: date
    trading_days: int


def _windows(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    return contract["windows"]


def development_data_days(contract: Mapping[str, Any]) -> tuple[date, ...]:
    """development 데이터 창(690 거래일, warm-up 포함) — 계약 경계를 캘린더로 재계산해 대조."""
    w = _windows(contract)
    start = date.fromisoformat(w["development_data_start"])
    end = date.fromisoformat(w["development_data_end"])
    days = tuple(cal.trading_days(start, end))
    expected = int(w["development_data_trading_days"])
    if len(days) != expected:
        raise ContractIntegrityError(
            f"development_data_trading_days 재계산 불일치: {len(days)} != {expected}")
    return days


def warmup_days(contract: Mapping[str, Any]) -> tuple[date, ...]:
    """RVOL lookback 형성용 앞 20 거래일(신호 평가 안 함, §3.1)."""
    n = int(_windows(contract)["warmup_trading_days"])
    return development_data_days(contract)[:n]


def development_eval_days(contract: Mapping[str, Any]) -> tuple[date, ...]:
    """development 평가 창(670 거래일) = development 데이터 창 - warm-up."""
    w = _windows(contract)
    n = int(w["warmup_trading_days"])
    days = development_data_days(contract)[n:]
    expected = int(w["development_eval_trading_days"])
    if len(days) != expected:
        raise ContractIntegrityError(
            f"development_eval_trading_days 재계산 불일치: {len(days)} != {expected}")
    if days and days[0].isoformat() != w["development_eval_start"]:
        raise ContractIntegrityError(
            f"development_eval_start 재계산 불일치: {days[0]} != {w['development_eval_start']}")
    if days and days[-1].isoformat() != w["development_eval_end"]:
        raise ContractIntegrityError(
            f"development_eval_end 재계산 불일치: {days[-1]} != {w['development_eval_end']}")
    return days


def blind_oos_days(contract: Mapping[str, Any]) -> tuple[date, ...]:
    """blind OOS 구간의 거래일 **날짜만**(125일) — 캔들 데이터는 여기서도, 어디서도 로드하지 않는다."""
    w = _windows(contract)
    start = date.fromisoformat(w["blind_oos_start"])
    end = date.fromisoformat(w["blind_oos_end"])
    days = tuple(cal.trading_days(start, end))
    expected = int(w["blind_oos_trading_days"])
    if len(days) != expected:
        raise ContractIntegrityError(
            f"blind_oos_trading_days 재계산 불일치: {len(days)} != {expected}")
    return days


def folds(contract: Mapping[str, Any]) -> tuple[Fold, ...]:
    """10개 fold — 63 거래일 블록, development 평가 창의 끝에 정렬해 뒤에서부터 자른다.

    앞쪽 나머지(670 % 63 = 40 거래일)는 가장 이른 fold(F01)에 흡수해 **모든 fold ≥ 63 거래일**
    이 되게 한다(사전등록 §3.2). 재생성 결과를 계약 `folds.boundaries` 와 전부 대조한다.
    """
    f = contract["folds"]
    ev = development_eval_days(contract)
    blk = int(f["block_trading_days"])
    n = len(ev)
    k = n // blk
    head = n - k * blk
    rebuilt: list[tuple[date, ...]] = [ev[0:head + blk]]
    rebuilt += [ev[head + blk + i * blk: head + blk + (i + 1) * blk] for i in range(k - 1)]

    boundaries = f["boundaries"]
    if len(rebuilt) != len(boundaries):
        raise ContractIntegrityError(
            f"fold 개수 재계산 불일치: {len(rebuilt)} != {len(boundaries)}")

    out: list[Fold] = []
    for block, boundary in zip(rebuilt, boundaries):
        if not block:
            raise ContractIntegrityError(f"fold {boundary['fold']} 블록이 비었다")
        if block[0].isoformat() != boundary["start"]:
            raise ContractIntegrityError(
                f"fold {boundary['fold']} 시작 재계산 불일치: "
                f"{block[0]} != {boundary['start']}")
        if block[-1].isoformat() != boundary["end"]:
            raise ContractIntegrityError(
                f"fold {boundary['fold']} 끝 재계산 불일치: {block[-1]} != {boundary['end']}")
        if len(block) != int(boundary["trading_days"]):
            raise ContractIntegrityError(
                f"fold {boundary['fold']} 거래일수 재계산 불일치: "
                f"{len(block)} != {boundary['trading_days']}")
        out.append(Fold(name=str(boundary["fold"]), start=block[0], end=block[-1],
                        trading_days=len(block)))

    total = sum(fold.trading_days for fold in out)
    if total != n:
        raise ContractIntegrityError(
            f"fold 합산 거래일수가 평가 창과 다르다(누락/중복): {total} != {n}")
    return tuple(out)


def fold_of(day: date, folds_: Sequence[Fold]) -> str | None:
    """`day` 가 속한 fold 이름. 어느 fold 경계에도 없으면 None(경계는 양끝 포함)."""
    for fold in folds_:
        if fold.start <= day <= fold.end:
            return fold.name
    return None


def early_close_days_in(days: Sequence[date]) -> tuple[date, ...]:
    """주어진 거래일 목록 중 조기폐장일(캘린더 재계산, 입력 순서 보존)."""
    return tuple(d for d in days if cal.is_early_close(d))


def regular_close_minute_checked(d: date) -> int:
    """`d` 의 정규장 마감(ET 분). `d` 가 거래일이 아니면 `ValueError`(비거래일에 마감 없음)."""
    minute = cal.regular_close_minute(d)
    if minute is None:
        raise ValueError(f"{d.isoformat()} 는 거래일이 아니다 — 정규장 마감이 정의되지 않는다")
    return minute


def minute_of_day(candle: Candle) -> int:
    """`ts_exchange` 의 minute-of-day(0~1439) — 단일 계산 지점(v1.1, 독립 리뷰 D3 반영).

    §5.1(gap 부등호)·§5.2(RVOL 창)·§6.2(결정봉 마감 필터)가 전부 이 값을 부등호로 비교한다.
    이 계산이 여러 곳에 인라인으로 흩어져 있으면(과거 5곳) v1.5 부등호 교정(조기폐장일 연장거래
    봉 제외) 같은 형제 조항 전파가 실패하기 쉽다 — 이 저장소의 반복 실패 유형이다.
    """
    return candle.ts_exchange.hour * 60 + candle.ts_exchange.minute
