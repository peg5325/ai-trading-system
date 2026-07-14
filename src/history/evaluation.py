"""분리 평가 데이터셋 — 시장/세션/out-of-time 슬라이스 + split(PR 3-B).

계약(docs/DESIGN_DELTA_V2_1.md §3-7): **공식 평가/모델선택 경로는 chronological /
embargo / LOSO 만 허용**. random split 은 시계열 누수를 유발하므로 공식 경로에서 금지하고,
진단 전용 함수로만 제공하며 `INVALID FOR MODEL SELECTION` 워터마크를 반환한다.

결정론: 저장 캔들과 인자만 사용(현재 시각 미사용), 세션 그룹/정렬은 입력 순서와 무관.
원본 DB 는 read-only(`mode=ro`). Runtime/Paper 무관.
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import date

from src.models import Candle

RANDOM_CV_WATERMARK = "INVALID FOR MODEL SELECTION"
OFFICIAL_SPLIT_METHODS = ("chronological", "embargo", "loso")


@dataclass(frozen=True)
class Slice:
    """(시장, 종목, interval) 커버리지 슬라이스 — 시장 분리 평가 단위."""
    market: str
    symbol: str
    interval: str
    earliest: str
    latest: str
    bars: int
    days: int


def list_slices(db_path: str, interval: str = "1m") -> list[Slice]:
    """통합 저장소를 시장/종목별 슬라이스로 열거(read-only). 시장(US/KR) 분리."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT market, symbol, interval, MIN(ts_utc) AS earliest, "
            "MAX(ts_utc) AS latest, COUNT(*) AS bars, "
            "COUNT(DISTINCT substr(ts_exchange,1,10)) AS days "
            "FROM candle_history WHERE interval=? "
            "GROUP BY market, symbol, interval ORDER BY market, symbol", (interval,),
        ).fetchall()
    finally:
        con.close()
    return [Slice(r["market"], r["symbol"], r["interval"], r["earliest"],
                  r["latest"], r["bars"], r["days"]) for r in rows]


def group_by_session(candles: list[Candle]) -> dict[date, list[Candle]]:
    """거래소 로컬 날짜(세션 ≈ 하루) 기준 그룹. 날짜 오름차순·세션 내 시간순(결정론)."""
    groups: dict[date, list[Candle]] = {}
    for c in candles:
        groups.setdefault(c.ts_exchange.date(), []).append(c)
    return {d: sorted(groups[d], key=lambda x: x.ts_utc) for d in sorted(groups)}


def chronological_split(candles: list[Candle], ratio: float = 0.7,
                        embargo_bars: int = 0) -> tuple[list[Candle], list[Candle]]:
    """out-of-time 분할(시간순). embargo_bars 만큼 train/test 사이 봉을 버려 누수 방지."""
    if not 0.0 < ratio < 1.0:
        raise ValueError("ratio must be in (0,1)")
    if embargo_bars < 0:
        raise ValueError("embargo_bars must be >= 0")
    ordered = sorted(candles, key=lambda c: c.ts_utc)
    k = int(len(ordered) * ratio)
    return ordered[:k], ordered[k + embargo_bars:]


def leave_one_session_out(candles: list[Candle]):
    """LOSO: 각 세션(거래일)을 test, 나머지를 train 으로 yield. (held_date, train, test)."""
    groups = group_by_session(candles)
    days = list(groups)
    for held in days:
        test = groups[held]
        train = [c for d in days if d != held for c in groups[d]]
        yield held, train, test


def official_split(candles: list[Candle], method: str, *, ratio: float = 0.7,
                   embargo_bars: int = 0):
    """공식 평가 split 디스패치. random 은 거부(계약 §3-7)."""
    if method == "random":
        raise ValueError(
            f"random split is {RANDOM_CV_WATERMARK}: 공식 평가/모델선택·승격 판정에서 금지. "
            "진단은 random_split_diagnostic 을 사용.")
    if method not in OFFICIAL_SPLIT_METHODS:
        raise ValueError(f"unknown split method: {method!r} (허용: {OFFICIAL_SPLIT_METHODS})")
    if method == "chronological":
        return chronological_split(candles, ratio=ratio, embargo_bars=embargo_bars)
    if method == "embargo":
        if embargo_bars <= 0:
            raise ValueError("embargo split 은 embargo_bars>=1 이 필요하다(gap 강제).")
        return chronological_split(candles, ratio=ratio, embargo_bars=embargo_bars)
    # loso → 리스트로 구체화
    return list(leave_one_session_out(candles))


def random_split_diagnostic(candles: list[Candle], ratio: float = 0.7, *,
                            seed: int) -> tuple[list[Candle], list[Candle], str]:
    """⚠ 진단 전용(누수 시연). 모델선택/승격 판정 금지. seed 고정 결정론.

    반환 3번째 원소는 워터마크(RANDOM_CV_WATERMARK) — 소비 측이 무효 표식을 강제 인지."""
    if not 0.0 < ratio < 1.0:
        raise ValueError("ratio must be in (0,1)")
    rng = random.Random(seed)
    idx = list(range(len(candles)))
    rng.shuffle(idx)
    k = int(len(candles) * ratio)
    train_idx, test_idx = set(idx[:k]), set(idx[k:])
    train = [candles[i] for i in range(len(candles)) if i in train_idx]
    test = [candles[i] for i in range(len(candles)) if i in test_idx]
    return train, test, RANDOM_CV_WATERMARK
