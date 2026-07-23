"""Blind OOS 접근 게이트 — 성과 계산은 fail-closed, coverage/schema 는 허용(경계 분리).

계약: `docs/research/DATA_CONTRACT_4STRATEGY_V1.md` §5.1(코드 게이트화) / AMENDMENT A1 · 이슈 #75.

계약 §5.1 은 blind OOS 경계를 **문서 규율**로만 두고 코드 강제는 "후속 고려"로 남겼다. 이 모듈이
그 코드 게이트다:
- **연구 로더 성과 경로는 기본 차단**(fail-closed). blind OOS 창과 겹치는 [start,end) 를
  purpose="performance" 로 열려면 **유효한 unlock artifact** 가 필요하다.
- **coverage/schema/결측 검사는 OOS 에서도 허용**(purpose="coverage") — 가격 수익률·전략 결과와
  경계를 분리한다.
- unlock 은 단순 boolean 이 아니라 **provenance(사전등록 merge commit·구현 commit·snapshot
  hash·review 상태)** 를 요구한다 → CLI 플래그 하나로 우회 불가.
- **이 모듈은 unlock artifact 를 생성하지 않는다.** 구조만 제공한다(향후 사전등록·리뷰 통과 후 생성).
- **배선(opt-in 아님)**: 공식 연구/평가 코드는 이 로더를 경유해야 한다. `load_candles` 직접
  import 는 `tests/test_oos_guard_wiring.py` 의 AST 경계 테스트가 src/·scripts/·analysis/ 전역에서
  금지한다(PR #77 독립 리뷰 차단 1 반영).
- **unlock 검증 깊이 한계**: 현재 `OOSUnlock.is_valid_for` 는 **형식**(필드 비어있지 않음 +
  review=approved)만 본다. 실제 blind OOS 개봉 전에는 commit 실존·snapshot hash 일치·리뷰 증거를
  대조하는 검증 로더가 선행돼야 한다(**#78, 개봉 전 필수**).

경계(계약 §5.1, as-of 2026-07-21):
- intraday(1m): blind OOS = 2026-01-21 ~ 2026-07-21.
- swing(1d):    blind OOS = 2025-07-21 ~ 2026-07-21.

★ 자본 안전: 주문/broker/risk/pipeline 을 import 하지 않는다. read-only 로더만 위임.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from src.history.loader import load_candles
from src.models import ET, Candle

TRACK_INTRADAY = "intraday"
TRACK_SWING = "swing"

# 계약 §5.1 고정 경계(inclusive 마지막 거래일). as-of 2026-07-21.
_BLIND_OOS: dict[str, tuple[date, date]] = {
    TRACK_INTRADAY: (date(2026, 1, 21), date(2026, 7, 21)),
    TRACK_SWING: (date(2025, 7, 21), date(2026, 7, 21)),
}

# unlock 이 유효로 인정되는 review 상태(승인). 그 외(pending/rejected/빈값)는 잠금 유지.
_APPROVED_REVIEW = frozenset({"approved"})


class OOSLockError(RuntimeError):
    """blind OOS 성과 접근을 unlock 없이 시도(fail-closed)."""


@dataclass(frozen=True)
class OOSUnlock:
    """blind OOS 개봉 근거(provenance). 단순 boolean 우회 방지.

    이번 작업에서는 **생성하지 않는다**. 향후 사전등록 merge·구현 commit·snapshot hash·독립
    리뷰 승인 뒤에만 유효한 인스턴스를 만든다.

    한계: `is_valid_for` 는 형식 검증(비어있지 않은 문자열 + approved)이다. 실제 개봉 전에는
    artifact 파일을 읽어 commit 실존·snapshot hash 일치·리뷰 증거를 대조하는 검증 로더가
    필요하다(#78) — 임의 문자열로 만든 unlock 이 개봉에 쓰이면 안 된다.
    """

    track: str
    prereg_merge_commit: str
    implementation_commit: str
    snapshot_hash: str
    review_status: str

    def is_valid_for(self, track: str) -> bool:
        return (
            self.track == track
            and bool(self.prereg_merge_commit)
            and bool(self.implementation_commit)
            and bool(self.snapshot_hash)
            and self.review_status in _APPROVED_REVIEW
        )


def blind_oos_window(track: str) -> tuple[date, date]:
    if track not in _BLIND_OOS:
        raise ValueError(f"알 수 없는 track {track!r} — 허용: {sorted(_BLIND_OOS)}")
    return _BLIND_OOS[track]


def _oos_bounds_utc(track: str) -> tuple[datetime, datetime]:
    """blind OOS 창을 ET 거래일 기준 half-open [lo, hi) UTC datetime 으로."""
    lo_d, hi_d = blind_oos_window(track)
    lo = datetime(lo_d.year, lo_d.month, lo_d.day, tzinfo=ET)
    hi = datetime(hi_d.year, hi_d.month, hi_d.day, tzinfo=ET) + timedelta(days=1)  # inclusive→exclusive
    return lo, hi


def overlaps_blind_oos(start: datetime | None, end: datetime | None, track: str) -> bool:
    """요청 [start, end) 가 blind OOS 창과 겹치는가.

    start/end 중 하나라도 None(무제한)이면 그쪽은 OOS 를 덮는 것으로 본다(fail-closed).
    """
    lo, hi = _oos_bounds_utc(track)
    after_lo = (end is None) or (end > lo)      # 요청이 lo 이후로 뻗음
    before_hi = (start is None) or (start < hi)  # 요청이 hi 이전에서 시작
    return after_lo and before_hi


def assert_window_unlocked(start: datetime | None, end: datetime | None, *,
                           track: str, unlock: OOSUnlock | None) -> None:
    """성과 경로 게이트: [start,end) 가 blind OOS 와 겹치면 유효 unlock 없이는 raise."""
    if not overlaps_blind_oos(start, end, track):
        return                                  # train/validation 구간 → 허용
    if unlock is not None and unlock.is_valid_for(track):
        return                                  # 유효 unlock → 개봉
    lo, hi = blind_oos_window(track)
    raise OOSLockError(
        f"blind OOS({track}: {lo}~{hi}) 성과 접근은 fail-closed — 유효한 OOSUnlock "
        f"(사전등록 merge·구현 commit·snapshot hash·review=approved) 필요. "
        f"(coverage/schema 검사는 purpose='coverage' 로 허용)")


def guard_returns_for_candles(candles: list[Candle], *, track: str,
                              unlock: OOSUnlock | None = None) -> None:
    """캔들 집합에서 [min ts, max ts] 를 유도해 성과 경로 게이트 적용(빈 리스트는 no-op).

    연구 forward-return/전략 성과 계산 **직전**에 호출한다(가격 수익률 계산 경계).
    """
    if not candles:
        return
    tss = [c.ts_utc for c in candles]
    lo, hi = min(tss), max(tss)
    assert_window_unlocked(lo, hi + timedelta(microseconds=1), track=track, unlock=unlock)


def research_load(db_path: str, symbol: str, interval: str, *,
                  start: datetime | None = None, end: datetime | None = None,
                  purpose: str = "performance", track: str = TRACK_INTRADAY,
                  unlock: OOSUnlock | None = None,
                  market: str | None = None) -> list[Candle]:
    """연구용 read-only 로더 게이트.

    - purpose="performance"(기본, fail-closed): [start,end) 가 blind OOS 와 겹치면 유효 unlock
      없이는 OOSLockError. 겹치지 않으면(train/validation) 정상 로드.
    - purpose="coverage": 게이트 없이 로드(coverage/schema/결측 검사 — OOS 에서도 허용).
      **성과·수익률을 계산하지 않는 호출자 전용**(경계 분리는 호출자 책임·문서 규율).
    """
    if purpose not in ("performance", "coverage"):
        raise ValueError(f"purpose 는 'performance'|'coverage' — got {purpose!r}")
    if purpose == "performance":
        assert_window_unlocked(start, end, track=track, unlock=unlock)
    return load_candles(db_path, symbol, interval, start=start, end=end, market=market)
