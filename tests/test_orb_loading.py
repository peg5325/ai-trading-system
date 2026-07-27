"""ORB 게이트 경유 로딩 테스트 — 이슈 #91 (4-A).

사전등록 §3.5(blind 코드 강제) / §6.2(결정봉) / §8.7(로드 범위)을 검증한다. AST/소스 스캔으로
`purpose="coverage"` 문자열과 `load_candles` 직접 참조가 이 패키지에 없음을 못박는다(그렇지
않으면 §3.5 의 코드 강제가 이 패키지에서는 장식이 될 수 있다).

v1.1(수정 라운드 1, 독립 리뷰 2-lane 반영): `decision_bars`/`expected_decision_minutes` 가
`DecisionBarSpec` 을 필수 인자로 받도록 바뀐 것에 맞춰 갱신하고, blind OOS 실배선(B1)·PRE 봉
누출 금지(B3)·min_bars 관측 가능성(B4)·정렬(B5)·market 하드코딩 부재(B6) 테스트를 추가했다.
"""
from __future__ import annotations

import ast
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.history.oos_guard import OOSLockError
from src.history.store import HistoryStore, ImportStats
from src.models import MARKET_US, Candle, regular_session_window
from src.research.orb.contract import DecisionBarSpec
from src.research.orb.loading import (
    decision_bars,
    et_day_bounds,
    expected_decision_minutes,
    load_symbol_day,
    regular_1m,
)

ET = ZoneInfo("America/New_York")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# H1-b(lane4 최우선, 라운드 3) — 예전엔 `src/research/orb/*.py`(비재귀)라 하위 패키지가
# 스캔에서 통째로 빠졌다(team-lead 재현: `src/research/orb/sim/runner.py` 에
# `purpose="coverage"` + `track="swing"` + `load_candles` 직접 import 를 동시에 넣어도
# 세 스캔은 전부 침묵했다). 재귀화했었다.
#
# R4-8(lane4 최우선, 라운드 4 — team-lead 재현) — 재귀화는 옳았지만 범위가 `src/research/**`
# 로만 한정돼 있었다. 이 저장소의 결과 산출 스크립트는 역사적으로 `analysis/` 에 있다
# (선례: `analysis/screener_ranking_h4.py`). team-lead 재현: `analysis/orb_walkforward_
# runner.py` 에 `research_load(..., unlock=<날조>)` 를 배치하니 154개 테스트가 전부 통과하며
# blind OOS 가 실제로 열렸다(2026-01-21 879봉 / 2026-04-15 952봉 / 2026-07-21 949봉).
# `tests/test_oos_guard_wiring.py` 의 `SCAN_DIRS` 는 `("src","scripts","analysis")` 를
# 덮지만 `load_candles` 만 본다 — `purpose`/`track`/`unlock` 세 규칙에는 그 범위 밖
# 백스톱이 없었다. 이 세 스캔의 범위를 그 `SCAN_DIRS` 와 맞춘다(재귀 유지).
ORB_SCAN_DIRS = ("src", "scripts", "analysis")   # tests/test_oos_guard_wiring.py 의 SCAN_DIRS 와 동일


def _orb_scan_py_files():
    """ORB 3종 스캔(unlock/track AST · coverage 문자열 · load_candles 참조) 대상 파일 전부.

    `tests/test_oos_guard_wiring.py:_py_files` 와 동일한 idiom(os.walk, 재귀) — 새 최상위
    파이썬 디렉터리가 생기면 `ORB_SCAN_DIRS` 에도(그리고 그 파일의 `SCAN_DIRS` 에도) 추가해야
    검사가 미친다.
    """
    for base in ORB_SCAN_DIRS:
        root = os.path.join(REPO, base)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)


# R4-8 — ORB_SCAN_DIRS 를 src/scripts/analysis 전체로 넓히면, 이 저장소에 이미 있던 정당한
# coverage-only/parity 스크립트(§5.5 커버리지 측정·리샘플러 동치 검증·게이트 자신)까지 스캔
# 대상이 된다. `tests/test_oos_guard_wiring.py` 의 `ALLOWED` 관례를 그대로 따른다 — 새 예외가
# 필요해지면 이 목록에 명시적으로 추가해야 하며, 조용히 스캔에서 빠지는 파일이 없게 한다.
_ALLOWED_COVERAGE_STRING = {
    os.path.join("src", "history", "oos_guard.py"),           # purpose="coverage" 의 정의처
    os.path.join("scripts", "history_eval_slices.py"),        # 검증된 coverage-only 스크립트
    os.path.join("scripts", "resampler_parity_check.py"),     # 검증된 coverage-only 스크립트
    os.path.join("scripts", "prereg_orb_coverage_probe.py"),  # §5.5 봉 개수만 세는 ORB coverage probe
}
_ALLOWED_LOAD_CANDLES_REF = {
    os.path.join("src", "history", "loader.py"),
    os.path.join("src", "history", "__init__.py"),
    os.path.join("src", "history", "oos_guard.py"),
}   # tests/test_oos_guard_wiring.py 의 ALLOWED 와 동일(정의처·재노출·게이트 자신)
_ALLOWED_TRACK_UNSPECIFIED = {
    # `research_load(..., purpose="coverage", market=...)` 처럼 `track=` 을 아예 생략하고
    # 함수 기본값(`TRACK_INTRADAY`)에 기댄다 — 값 자체는 안전하지만 명시적이지 않다. ORB
    # 패키지 자신의 코딩 규율(항상 명시)과는 다른, 이미 검토된 pre-ORB 스크립트.
    os.path.join("scripts", "history_eval_slices.py"),
}

# §6.2 primary/sensitivity 공용 결정봉 스펙(5분·min_bars=1) — 계약값과 동일(§contract.py 로
# 별도 검증). 여기서는 리터럴로 고정해 loading.py 테스트를 contract.py 로딩에 결합하지 않는다.
DEFAULT_SPEC = DecisionBarSpec(minutes=5, min_bars=1)

# B1 — 실 스냅샷 절대경로(메인 체크아웃, 워크트리 공유 안 함). 없으면 skip.
REAL_SNAPSHOT_PATH = (
    "/Users/eongyupark/Workspace/02_personal/01_ai_trading/ai_trading_system/"
    "data/research_snapshots/p0_us30_orb_clean_1m_asof_20260721.bd9fc14.sqlite"
)


def _bar(symbol: str, moment_et: datetime, *, o=100.0, h=101.0, low=99.0, c=100.5,
        v=100.0) -> Candle:
    return Candle.from_moment(symbol=symbol, interval="1m", moment=moment_et,
                              o=o, h=h, low=low, c=c, v=v,
                              currency="USD", adjusted=True, market=MARKET_US)


def _write_minutes(store: HistoryStore, symbol: str, start: datetime, end: datetime) -> None:
    """[start, end] 폐구간, 1분 간격 봉을 store 에 적재(포함 양끝)."""
    stats = ImportStats(source="toss_backfill")
    bars = []
    t = start
    i = 0
    while t <= end:
        bars.append(_bar(symbol, t, o=100 + i * 0.01, h=100.5 + i * 0.01,
                         low=99.5 + i * 0.01, c=100.2 + i * 0.01, v=100 + i))
        t += timedelta(minutes=1)
        i += 1
    store.upsert_candles(bars, source="toss_backfill", run_id="r", stats=stats)


# --------------------------------------------------------------------------
# et_day_bounds — DST 전환일에도 정확히 그 ET 달력일을 덮는다
# --------------------------------------------------------------------------
def test_et_day_bounds_spring_forward_2023_03_12():
    lo, hi = et_day_bounds(date(2023, 3, 12))
    assert lo == datetime(2023, 3, 12, 0, 0, tzinfo=ET)
    assert hi == datetime(2023, 3, 13, 0, 0, tzinfo=ET)
    assert lo.utcoffset() == timedelta(hours=-5)     # EST(자정 시점엔 아직 전환 전)
    assert hi.utcoffset() == timedelta(hours=-4)      # EDT(다음날 자정은 전환 후)


def test_et_day_bounds_fall_back_2023_11_05():
    lo, hi = et_day_bounds(date(2023, 11, 5))
    assert lo == datetime(2023, 11, 5, 0, 0, tzinfo=ET)
    assert hi == datetime(2023, 11, 6, 0, 0, tzinfo=ET)
    assert lo.utcoffset() == timedelta(hours=-4)      # EDT
    assert hi.utcoffset() == timedelta(hours=-5)      # EST


def test_et_day_bounds_span_exactly_one_calendar_day():
    for d in (date(2023, 3, 12), date(2023, 11, 5), date(2024, 6, 3)):
        lo, hi = et_day_bounds(d)
        assert lo.date() == d
        assert hi.date() == d + timedelta(days=1)
        assert lo.hour == 0 and lo.minute == 0
        assert hi.hour == 0 and hi.minute == 0


# --------------------------------------------------------------------------
# decision_bars — 정상일
# --------------------------------------------------------------------------
def test_decision_bars_excludes_post_bars(tmp_path):
    d = date(2024, 3, 4)
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 15, 59, tzinfo=ET))
        # POST 봉 — 결정봉 대상 아님, 마지막 REGULAR 버킷을 닫는 시간 증거로만 쓰인다.
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 16, 5, tzinfo=ET))
        store.commit()

    day_candles = load_symbol_day(db, "AAPL", d)
    bars = decision_bars(day_candles, d, spec=DEFAULT_SPEC)
    assert all(b.session == "REGULAR" for b in bars)
    assert len(bars) == 78


def test_decision_bars_normal_day_last_bar_included_first_after_close_excluded(tmp_path):
    d = date(2024, 3, 4)
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 15, 59, tzinfo=ET))
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 16, 5, tzinfo=ET))
        store.commit()

    day_candles = load_symbol_day(db, "AAPL", d)
    bars = decision_bars(day_candles, d, spec=DEFAULT_SPEC)
    minutes = sorted(b.ts_exchange.hour * 60 + b.ts_exchange.minute for b in bars)
    assert 15 * 60 + 55 in minutes            # 15:55 포함(마지막 정상 결정봉)
    assert 16 * 60 not in minutes             # 16:00 이후 제외


def test_last_regular_bucket_closes_with_post_evidence_only(tmp_path):
    """POST 봉이 있으면 [15:55,16:00) 버킷이 닫히고, 없으면 닫히지 않는다."""
    d = date(2024, 3, 4)

    db_with_post = str(tmp_path / "with_post.sqlite")
    with HistoryStore(db_with_post) as store:
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 15, 59, tzinfo=ET))
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET))
        store.commit()
    bars_with_post = decision_bars(load_symbol_day(db_with_post, "AAPL", d), d, spec=DEFAULT_SPEC)
    minutes_with_post = {b.ts_exchange.hour * 60 + b.ts_exchange.minute for b in bars_with_post}
    assert 15 * 60 + 55 in minutes_with_post
    assert len(bars_with_post) == 78

    db_without_post = str(tmp_path / "without_post.sqlite")
    with HistoryStore(db_without_post) as store:
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 15, 59, tzinfo=ET))
        store.commit()
    bars_without_post = decision_bars(
        load_symbol_day(db_without_post, "AAPL", d), d, spec=DEFAULT_SPEC)
    minutes_without_post = {
        b.ts_exchange.hour * 60 + b.ts_exchange.minute for b in bars_without_post}
    assert 15 * 60 + 55 not in minutes_without_post
    assert len(bars_without_post) == 77


def test_decision_bars_are_sorted_ascending_by_ts_utc(tmp_path):
    """B5(독립 리뷰 L3) — 결정봉이 ts_utc 오름차순인지 명시적으로 단언한다.

    4-B 의 순차 스캔이 이 순서에 의존한다 — 이전 판은 개수/포함 여부만 확인해 `reverse=True`
    변조가 살아남았다.
    """
    d = date(2024, 3, 4)
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 15, 59, tzinfo=ET))
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET))   # POST(닫힘 증거)
        store.commit()
    day_candles = load_symbol_day(db, "AAPL", d)
    bars = decision_bars(day_candles, d, spec=DEFAULT_SPEC)
    ts_list = [b.ts_utc for b in bars]
    assert len(ts_list) == 78
    assert ts_list == sorted(ts_list)
    assert len(set(ts_list)) == len(ts_list)


def test_decision_bars_never_include_premarket_bars(tmp_path):
    """B3(독립 리뷰 H4) — PRE 봉이 실제로 존재해도 결정봉에 절대 섞이지 않는다.

    이전 판은 fixture 가 PRE 봉을 한 번도 적재하지 않아 `session == REGULAR` 필터를 지우는
    변조가 살아남았다(제거 시 결정봉 78 → 108, 첫 봉 07:00 로 유입되는 것을 실증).
    """
    d = date(2024, 3, 4)
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 7, 0, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 9, 29, tzinfo=ET))    # PRE
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 15, 59, tzinfo=ET))   # REGULAR
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET))    # POST(닫힘 증거)
        store.commit()

    day_candles = load_symbol_day(db, "AAPL", d)
    assert any(c.session == "PRE" for c in day_candles)
    bars = decision_bars(day_candles, d, spec=DEFAULT_SPEC)
    assert all(b.session == "REGULAR" for b in bars)
    assert all((b.ts_exchange.hour, b.ts_exchange.minute) >= (9, 30) for b in bars)
    assert len(bars) == 78


def test_decision_bars_min_bars_1_includes_bucket_with_missing_minute(tmp_path):
    """B4(독립 리뷰 M1) — §6.2 가 고정한 min_bars=1(커버리지축)이 실제로 관측 가능해야 한다.

    10:00 버킷에서 10:02 분이 결측이어도(4/5) min_bars=1 이면 그 버킷을 만들고, min_bars=5(완전한
    버킷만) 로 바꾸면 사라진다 — 이전 판은 모든 fixture 가 결측 없는 연속 분봉이라 이 축 자체가
    관측 불가능했다.
    """
    d = date(2024, 3, 4)
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 9, 59, tzinfo=ET))
        gapped = [
            _bar("AAPL", datetime(d.year, d.month, d.day, 10, minute, tzinfo=ET))
            for minute in (0, 1, 3, 4)   # 10:02 결측(4/5)
        ]
        stats = ImportStats(source="toss_backfill")
        store.upsert_candles(gapped, source="toss_backfill", run_id="r", stats=stats)
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 10, 5, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 15, 59, tzinfo=ET))
        store.commit()

    day_candles = load_symbol_day(db, "AAPL", d)

    bars_min1 = decision_bars(day_candles, d, spec=DecisionBarSpec(minutes=5, min_bars=1))
    minutes_min1 = {b.ts_exchange.hour * 60 + b.ts_exchange.minute for b in bars_min1}
    assert (10 * 60) in minutes_min1              # 결측이 있어도 min_bars=1 이면 버킷을 만든다

    bars_min5 = decision_bars(day_candles, d, spec=DecisionBarSpec(minutes=5, min_bars=5))
    minutes_min5 = {b.ts_exchange.hour * 60 + b.ts_exchange.minute for b in bars_min5}
    assert (10 * 60) not in minutes_min5          # min_bars=5(완전한 버킷만) 면 그 버킷은 없다


def test_decision_bars_ignores_candles_outside_the_target_day(tmp_path):
    """D1(독립 리뷰 M2) — 이틀치를 한 번에 넘겨도 다음 날 봉이 오늘 마지막 버킷을 조용히
    닫아버리지 않는다(`ts_exchange.date() == d` 로 구조적 차단, `load_symbol_day` 는 이미
    하루치만 반환하지만 `decision_bars` 스스로도 이 불변식을 강제한다)."""
    d = date(2024, 3, 4)
    next_d = date(2024, 3, 5)
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 15, 59, tzinfo=ET))   # POST 없음
        _write_minutes(store, "AAPL", datetime(next_d.year, next_d.month, next_d.day, 9, 30,
                                              tzinfo=ET),
                       datetime(next_d.year, next_d.month, next_d.day, 9, 35, tzinfo=ET))
        store.commit()

    # 두 날짜를 한 번에 로드(오늘 하루치만 반환하는 load_symbol_day 를 우회한 상황을 시뮬레이션).
    from src.history.oos_guard import TRACK_INTRADAY, research_load
    two_days = tuple(research_load(
        db, "AAPL", "1m",
        start=datetime(d.year, d.month, d.day, tzinfo=ET),
        end=datetime(next_d.year, next_d.month, next_d.day, 9, 36, tzinfo=ET),
        purpose="performance", track=TRACK_INTRADAY, market="US",
    ))
    bars = decision_bars(two_days, d, spec=DEFAULT_SPEC)
    assert all(b.ts_exchange.date() == d for b in bars)
    minutes = {b.ts_exchange.hour * 60 + b.ts_exchange.minute for b in bars}
    assert 15 * 60 + 55 not in minutes    # 다음 날 봉이 있어도 조용히 오늘 마지막 버킷을 닫지 않음
    assert len(bars) == 77


def test_decision_bars_ignores_candles_from_a_previous_day_mixed_in(tmp_path):
    """C1(lane4, team-lead 재현) — 위 D1 테스트는 **다음 날** 오염만 본다. 날짜 필터가
    `ts_exchange.date() == d` 에서 `<= d` 로 물러나면(과거 날 유입), 다음 날 케이스는 여전히
    막히지만 **과거 날** 봉이 새어 들어와 78→156 으로 부풀어 오른다(team-lead 재현 수치)."""
    d = date(2024, 3, 4)
    prev_d = date(2024, 3, 1)   # 직전 거래일(금)
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _write_minutes(store, "AAPL", datetime(prev_d.year, prev_d.month, prev_d.day, 9, 30,
                                              tzinfo=ET),
                       datetime(prev_d.year, prev_d.month, prev_d.day, 15, 59, tzinfo=ET))
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 15, 59, tzinfo=ET))
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET))   # POST(닫힘 증거)
        store.commit()

    from src.history.oos_guard import TRACK_INTRADAY, research_load
    two_days = tuple(research_load(
        db, "AAPL", "1m",
        start=datetime(prev_d.year, prev_d.month, prev_d.day, tzinfo=ET),
        end=datetime(d.year, d.month, d.day, 16, 1, tzinfo=ET),
        purpose="performance", track=TRACK_INTRADAY, market="US",
    ))
    bars = decision_bars(two_days, d, spec=DEFAULT_SPEC)
    assert all(b.ts_exchange.date() == d for b in bars)
    assert len(bars) == 78     # `<= d` 로 물러나면 prev_d 78개가 더해져 156 이 된다


def test_decision_bars_date_filter_uses_ts_exchange_not_ts_utc(tmp_path):
    """C2(lane4 L15, team-lead 재현) — 날짜 필터가 `ts_exchange.date()` 대신 `ts_utc.date()`
    를 쓰면 ET 대신 UTC 날짜로 그룹핑된다. 겨울(EST, UTC-5) POST 봉을 19:30 ET 에 놓으면 그
    ts_utc 는 다음날 00:30 UTC 라 `ts_utc.date() == d` 필터에서는 **탈락**하지만 실제로는
    `d` 그 날의 정당한 닫힘 증거다 — 탈락하면 마지막 REGULAR 버킷(15:55~16:00)이 닫히지
    않아 78→77 로 준다."""
    d = date(2024, 1, 8)   # 월요일, 겨울(EST, UTC-5) — ET/UTC 날짜가 저녁에 갈라진다
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 15, 59, tzinfo=ET))
        post_evidence = _bar("AAPL", datetime(d.year, d.month, d.day, 19, 30, tzinfo=ET),
                             o=1, h=1, low=1, c=1, v=1)
        assert post_evidence.ts_utc.date() != d      # 자정 UTC 를 넘어갔다는 전제 확인
        stats = ImportStats(source="toss_backfill")
        store.upsert_candles([post_evidence], source="toss_backfill", run_id="r", stats=stats)
        store.commit()

    day_candles = load_symbol_day(db, "AAPL", d)
    bars = decision_bars(day_candles, d, spec=DEFAULT_SPEC)
    minutes = {b.ts_exchange.hour * 60 + b.ts_exchange.minute for b in bars}
    assert 15 * 60 + 55 in minutes    # 19:30 ET 증거로 마지막 버킷이 닫혀야 한다
    assert len(bars) == 78            # ts_utc 로 그룹핑되면 이 증거를 잃어 77 이 된다


# --------------------------------------------------------------------------
# decision_bars — 조기폐장일
# --------------------------------------------------------------------------
def test_decision_bars_early_close_1255_included_1300_1305_excluded(tmp_path):
    d = date(2024, 7, 3)   # 캘린더 조기폐장일(13:00 ET)
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 13, 5, tzinfo=ET))
        store.commit()

    day_candles = load_symbol_day(db, "AAPL", d)
    bars = decision_bars(day_candles, d, spec=DEFAULT_SPEC)
    minutes = sorted(b.ts_exchange.hour * 60 + b.ts_exchange.minute for b in bars)
    assert 12 * 60 + 55 in minutes            # 12:55 포함
    assert 13 * 60 not in minutes             # 13:00 제외(연장거래 REGULAR 라벨)
    assert 13 * 60 + 5 not in minutes         # 13:05 제외
    assert len(bars) == 42


# --------------------------------------------------------------------------
# expected_decision_minutes
# --------------------------------------------------------------------------
def test_expected_decision_minutes_normal_and_early_close_counts():
    normal = expected_decision_minutes(date(2024, 3, 4), spec=DEFAULT_SPEC)
    early = expected_decision_minutes(date(2024, 7, 3), spec=DEFAULT_SPEC)
    assert len(normal) == 78
    assert len(early) == 42
    assert normal[0] == 9 * 60 + 30 and normal[-1] == 15 * 60 + 55
    assert early[0] == 9 * 60 + 30 and early[-1] == 12 * 60 + 55


def test_decision_bars_uses_spec_minutes_not_hardcoded_5(tmp_path):
    """C3(lane4) — 이 파일의 모든 기존 fixture 가 `DEFAULT_SPEC`(minutes=5) 만 써서
    `decision_bars` 가 `spec.minutes` 를 5 로 하드코딩해도 잡지 못했다. 15분 스펙으로 호출해
    그리드 간격이 실제로 15분인지 확인한다(하드코딩이면 여전히 5분 그리드가 나온다)."""
    d = date(2024, 3, 4)
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 15, 59, tzinfo=ET))
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET))
        store.commit()
    day_candles = load_symbol_day(db, "AAPL", d)
    spec15 = DecisionBarSpec(minutes=15, min_bars=1)
    bars = decision_bars(day_candles, d, spec=spec15)
    minutes = sorted(b.ts_exchange.hour * 60 + b.ts_exchange.minute for b in bars)
    assert len(bars) == 26                  # 390 / 15 = 26(하드코딩 5 면 78 이 나온다)
    assert minutes[1] - minutes[0] == 15    # 그리드 간격 15분(하드코딩 5 면 5)


def test_expected_decision_minutes_uses_spec_minutes_not_hardcoded_5():
    """C4(lane4) — 위와 같은 하드코딩 위험이 `expected_decision_minutes` 에도 있다."""
    spec15 = DecisionBarSpec(minutes=15, min_bars=1)
    normal = expected_decision_minutes(date(2024, 3, 4), spec=spec15)
    assert len(normal) == 26                # (960-570)/15 = 26(하드코딩 5 면 78)
    assert normal[1] - normal[0] == 15


def test_expected_decision_minutes_open_minute_is_not_hardcoded_570(monkeypatch):
    """C5/C11(lane4 N07/N08) — `test_market_is_explicitly_us_and_session_window_is_market_aware`
    의 마지막 단언(`exp[0] == open_min`)은 **항진명제**였다(하드코딩 570 이어도 실제 값이
    570 이므로 항상 참). `regular_session_window` 를 실제로 몽키패치해 다른 개장분을
    흘려보내고 `expected_decision_minutes` 가 그 값을 실제로 쓰는지 확인한다(하드코딩이면
    이 몽키패치와 무관하게 570 이 나와 실패한다)."""
    import src.research.orb.loading as loading_mod

    def fake_window(market: str) -> tuple[int, int]:
        return (600, 960)   # 개장을 570 대신 600 으로

    monkeypatch.setattr(loading_mod, "regular_session_window", fake_window)
    normal = expected_decision_minutes(date(2024, 3, 4), spec=DEFAULT_SPEC)
    assert normal[0] == 600


def test_regular_1m_filters_by_session(tmp_path):
    d = date(2024, 3, 4)
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 9, 35, tzinfo=ET))
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 16, 5, tzinfo=ET))
        store.commit()
    day_candles = load_symbol_day(db, "AAPL", d)
    assert any(c.session == "POST" for c in day_candles)
    reg = regular_1m(day_candles)
    assert reg and all(c.session == "REGULAR" for c in reg)
    assert len(reg) < len(day_candles)


# --------------------------------------------------------------------------
# B6 — market="US" 명시 / 개장분이 market-aware 로 유도됨(하드코딩 570 아님)
# --------------------------------------------------------------------------
def test_market_is_explicitly_us_and_session_window_is_market_aware():
    """`regular_session_window("US")` 가 실제로 (570,960) 을 주고 KR 과 다른지 확인한다.

    ⚠ C11(lane4 정직성 수정) — 예전 docstring 은 "570 하드코딩을 구별한다(독립 리뷰
    L2/L4/N07/N08)"고 주장했으나, 마지막 단언(`exp[0] == open_min`)은 **항진명제**다
    (하드코딩이어도 그 상수가 실제로 570 이므로 이 비교는 늘 참이다 — 두 변수가 같은 상수를
    가리킬 뿐 어느 쪽도 실제로 유도되었는지는 증명하지 못한다). "하드코딩이 아님"의 실제
    증명은 `test_expected_decision_minutes_open_minute_is_not_hardcoded_570`(regular_session_window
    를 몽키패치해 다른 값으로 실제로 갈라지는지 확인)가 한다.
    """
    open_min, close_min = regular_session_window("US")
    assert (open_min, close_min) == (570, 960)
    # KR 과 다른 값이어야 진짜로 market 인자가 쓰이고 있다는 증거가 된다(상수 우연 일치 배제).
    open_min_kr, _close_min_kr = regular_session_window("KR")
    assert open_min_kr != open_min
    exp = expected_decision_minutes(date(2024, 3, 4), spec=DEFAULT_SPEC)
    assert exp[0] == open_min


def test_load_symbol_day_passes_market_us_explicitly():
    """AST: `research_load(...)` 호출이 `market="US"` 리터럴을 명시적으로 넘기는지 확인한다
    (리터럴이 없으면 market 미지정 로드가 되어 동일 심볼의 타 market 행이 섞일 수 있다)."""
    path = os.path.join(REPO, "src", "research", "orb", "loading.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
                node.func.id == "research_load":
            for kw in node.keywords:
                if kw.arg == "market" and isinstance(kw.value, ast.Constant) and \
                        kw.value.value == "US":
                    found = True
    assert found, 'research_load 호출에 market="US" 리터럴이 없다'


# --------------------------------------------------------------------------
# B1 — blind OOS 배선 회귀(잠금 단언은 hermetic·무조건 실행 / 데이터 존재는 합성 fixture /
# 실 스냅샷 판은 참고용 + CI 강제 스위치) — H1-c(lane4 최우선)
# --------------------------------------------------------------------------
def test_blind_oos_lock_fires_before_any_file_access(tmp_path):
    """H1-c(lane4 최우선, team-lead 재현) — `OOSLockError` 는 파일을 열기 **전에** 발동한다.

    `research_load` 는 `assert_window_unlocked` 를 `load_candles`(실제 파일 접근) 보다 먼저
    호출한다(`src/history/oos_guard.py` 참조). 즉 잠금은 **날짜만으로** 판정되며 스냅샷
    파일의 존재 여부와 무관하다. 이전 판은 이 단언이 `test_blind_oos_boundary_on_real_snapshot`
    안에서만, 그것도 실 스냅샷(6.8GB, gitignored)에 `skipif` 로 걸려 있어 CI·새 클론·다른
    머신·파일명 변경이면 조용히 사라졌다 — `OOSLockError` 를 실제로 단언하는 자리가 저장소
    전체에서 그 테스트 하나뿐이었다. 존재하지 않는 경로로도 무조건 실행되게 해 그 취약점을
    없앤다.
    """
    nonexistent_db = str(tmp_path / "does_not_exist.sqlite")
    with pytest.raises(OOSLockError):
        load_symbol_day(nonexistent_db, "AAPL", date(2026, 1, 21))    # blind OOS 첫 날
    with pytest.raises(OOSLockError):
        load_symbol_day(nonexistent_db, "AAPL", date(2026, 7, 21))    # blind OOS 마지막 날
    with pytest.raises(OOSLockError):
        load_symbol_day(nonexistent_db, "AAPL", date(2026, 4, 15))    # OOS 한가운데


def test_development_last_day_loads_from_synthetic_snapshot(tmp_path):
    """H1-c — development 마지막 날(2026-01-20)은 잠기지 않는다는 것을 실 스냅샷 없이도
    hermetic 하게 확인한다(이 파일의 `HistoryStore`/`_write_minutes` idiom 으로 30봉 합성)."""
    d = date(2026, 1, 20)
    db = str(tmp_path / "synthetic.sqlite")
    with HistoryStore(db) as store:
        _write_minutes(store, "AAPL", datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                       datetime(d.year, d.month, d.day, 9, 59, tzinfo=ET))   # 30봉
        store.commit()
    got = load_symbol_day(db, "AAPL", d)
    assert len(got) == 30


_ORB_REQUIRE_SNAPSHOT = os.environ.get("ORB_REQUIRE_SNAPSHOT") == "1"
_real_snapshot_missing = not os.path.exists(REAL_SNAPSHOT_PATH)
if _real_snapshot_missing and _ORB_REQUIRE_SNAPSHOT:
    raise AssertionError(
        f"ORB_REQUIRE_SNAPSHOT=1 인데 실 P0-US30 스냅샷이 없다: {REAL_SNAPSHOT_PATH}")


@pytest.mark.skipif(
    _real_snapshot_missing,
    reason=(
        "실 P0-US30 스냅샷 부재 — 이 워크트리는 data/research_snapshots/ 를 다른 체크아웃과 "
        f"공유하지 않는다({REAL_SNAPSHOT_PATH}). CI 에서 강제하려면 "
        "ORB_REQUIRE_SNAPSHOT=1(저장소 관례, tests/test_s001_parity.py 의 "
        "H2B_REQUIRE_PARITY=1 과 같은 패턴)."
    ),
)
def test_blind_oos_boundary_on_real_snapshot():
    """B1(독립 리뷰 H1, 참고용 — 1차 방어선은 위 hermetic 판). 실 스냅샷에서도
    development 마지막 날은 로드되고, blind OOS 첫/마지막 날은 잠긴다."""
    got = load_symbol_day(REAL_SNAPSHOT_PATH, "AAPL", date(2026, 1, 20))   # development 마지막 날
    assert len(got) > 0
    with pytest.raises(OOSLockError):
        load_symbol_day(REAL_SNAPSHOT_PATH, "AAPL", date(2026, 1, 21))    # blind OOS 첫 날
    with pytest.raises(OOSLockError):
        load_symbol_day(REAL_SNAPSHOT_PATH, "AAPL", date(2026, 7, 21))    # blind OOS 마지막 날


def _research_load_call_names(tree: ast.AST) -> set[str]:
    """이 파일에서 `research_load` 를 가리킬 수 있는 **bare-name** 후보 집합.

    `from src.history.oos_guard import research_load as rl` 처럼 별칭(alias) import 되면
    호출부는 `rl(...)` 로 보이므로, 원래 이름(`research_load`)만 보고서는 그 호출을 놓친다
    (H1-a, team-lead 재현). `ast.Attribute` 형태(`mod.research_load(...)`)는 별칭과 무관하게
    `.attr` 이 항상 `research_load` 이므로 별도 처리한다(아래 offender 판정에서 직접 비교).
    """
    names = {"research_load"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "research_load":
                    names.add(alias.asname or alias.name)
    return names


def _research_load_offenders_in_tree(tree: ast.AST, rel: str) -> list[str]:
    """AST: `rel` 파일의 `research_load` 호출 중 unlock/track 규칙 위반 offender 목록.

    R4-5(lane1 §6-B, team-lead 재현) — 실제 스캔(`test_research_load_calls_never_pass_unlock_
    and_use_intraday_track`)과 거짓 가드 방지 fixture(아래
    `test_research_load_offender_detection_rule_actually_catches_...`)가 **이 함수 하나를
    같이 부른다.** 예전엔 증명 fixture 가 이 판정 로직을 별도로 복제해 갖고 있어서, 실제
    스캐너에서 `kw.arg is None` 분기만 지워도(소스가 깨끗하므로) 증명 테스트는 자기
    사본으로 독립적으로 계속 통과했다 — H1-a 가 바로 이 스캐너의 결함이었으므로, 스캐너
    자체가 약해지는 회귀를 증명 fixture 가 놓치면 값이 없다.

    H1-a(team-lead 재현) — 예전 판은 `kw.arg == "unlock"` 만 봐서 `**_EXTRA`(kwargs 언패킹,
    `kw.arg is None`) 로 넘긴 `unlock` 을 놓쳤고, `ast.Name` 함수 호출만 봐서
    `mod.research_load(...)`(`ast.Attribute`)와 별칭 import 도 놓쳤다. 세 가지를 모두 잡는다:
      1. `**kwargs` 언패킹(`kw.arg is None`) 이 하나라도 있으면 즉시 offender.
      2. `ast.Attribute` 호출(`.attr == "research_load"`)도 `ast.Name` 과 동일하게 검사한다.
      3. 별칭 import(`import ... as rl`)로 가리키는 이름도 `research_load` 와 동일 취급한다.
    """
    offenders: list[str] = []
    aliases = _research_load_call_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id not in aliases:
                continue
        elif isinstance(func, ast.Attribute):
            if func.attr != "research_load":
                continue
        else:
            continue

        if any(kw.arg is None for kw in node.keywords):
            offenders.append(
                f"{rel}:{node.lineno}: **kwargs 언패킹으로 unlock/track 이 은닉될 수 있음")
            continue
        kw_names = {kw.arg for kw in node.keywords}
        if "unlock" in kw_names:
            offenders.append(f"{rel}:{node.lineno}: unlock 인자 존재")
            continue
        track_kw = next((kw for kw in node.keywords if kw.arg == "track"), None)
        is_intraday_const = (
            track_kw is not None
            and isinstance(track_kw.value, ast.Name)
            and track_kw.value.id == "TRACK_INTRADAY"
        )
        if not is_intraday_const:
            offenders.append(f"{rel}:{node.lineno}: track 이 TRACK_INTRADAY 상수가 아님")
    return offenders


def test_research_load_calls_never_pass_unlock_and_use_intraday_track():
    """AST: `src/`·`scripts/`·`analysis/`(R4-8 — `tests/test_oos_guard_wiring.py` 의
    `SCAN_DIRS` 와 동일 범위) 전역의 `research_load` 호출은 `unlock` 인자를 절대 넘기지 않고,
    `track` 은 반드시 `TRACK_INTRADAY` 상수여야 한다(임의 `OOSUnlock` 주입·`track="swing"`
    변조를 정적으로 차단, 독립 리뷰 H1). 판정 로직은 `_research_load_offenders_in_tree`
    (R4-5 — 아래 증명 fixture 와 공유). `_ALLOWED_TRACK_UNSPECIFIED` 는 이미 검토된 pre-ORB
    스크립트(track 생략, 기본값 신뢰) 예외다."""
    offenders: list[str] = []
    for path in sorted(_orb_scan_py_files()):
        rel = os.path.relpath(path, REPO)
        if rel in _ALLOWED_TRACK_UNSPECIFIED:
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        offenders.extend(_research_load_offenders_in_tree(tree, rel))
    assert offenders == [], offenders


def test_research_load_offender_detection_rule_actually_catches_kwargs_and_attribute_and_alias(
        tmp_path):
    """거짓 가드 방지 — 실제 스캔이 쓰는 것과 **같은 함수**(`_research_load_offenders_in_tree`,
    R4-5)로 **kwargs**·`ast.Attribute` 호출·별칭 import 를 탐지하는지 증명한다. 이전 판은
    판정 로직을 복제한 사본으로 검증해, 실제 스캐너가 약해져도(예: `kw.arg is None` 분기
    제거) 이 증명 테스트가 독립적으로 계속 통과해 회귀를 놓쳤다(lane1 §6-B)."""
    offending_src = (
        "from src.history.oos_guard import research_load as rl\n"
        "from src.history.oos_guard import research_load\n"
        "import src.history.oos_guard as og\n"
        "_EXTRA = {'unlock': object(), 'track': 'swing'}\n"
        "rl('db', 'AAPL', '1m', purpose='performance', track=TRACK_INTRADAY, **_EXTRA)\n"
        "og.research_load('db', 'AAPL', '1m', purpose='performance', track='swing')\n"
    )
    tmp_file = tmp_path / "offender.py"
    tmp_file.write_text(offending_src, encoding="utf-8")
    tree = ast.parse(offending_src, filename=str(tmp_file))
    aliases = _research_load_call_names(tree)
    assert "rl" in aliases and "research_load" in aliases

    offenders = _research_load_offenders_in_tree(tree, "offender.py")
    kwargs_hits = [o for o in offenders if "kwargs" in o]
    other_hits = [o for o in offenders if "kwargs" not in o]
    # rl(**_EXTRA) 는 kwargs 은닉으로, og.research_load(...) 는 그 외(track 불일치)로
    # 정확히 하나씩 잡혀야 한다(ast.walk 순회 순서에 의존하지 않는다).
    assert len(kwargs_hits) == 1
    assert len(other_hits) == 1
    assert len(offenders) == 2


def test_orb_scan_catches_a_violation_placed_under_the_real_analysis_directory():
    """R4-8(lane4 최우선, team-lead 재현) — 4-B 의 ORB 러너가 `analysis/` 에 놓일 수 있다
    (선례: `analysis/screener_ranking_h4.py` 가 H4 결과 산출본이었다). team-lead 재현:
    `analysis/orb_walkforward_runner.py` 에 `research_load(..., unlock=<날조>)` 를 배치하니
    154개 테스트가 전부 통과하며 blind OOS 가 실제로 열렸다.

    합성 fixture(위 테스트)와 별개로, **실제 `analysis/` 디렉터리**에 임시 위반 파일을 만들어
    `_orb_scan_py_files`(스캔 대상 목록)와 `_research_load_offenders_in_tree`(판정) 가
    end-to-end 로 실제로 잡는지 확인한다 — 끝나면 파일을 지운다(저장소에 남기지 않는다,
    실패해도 `finally` 로 정리한다)."""
    analysis_dir = os.path.join(REPO, "analysis")
    assert os.path.isdir(analysis_dir), "analysis/ 디렉터리가 없다 — 전제가 깨졌다"
    probe_path = os.path.join(analysis_dir, "_r4_8_scan_probe_scratch.py")
    assert not os.path.exists(probe_path), "이전 실행의 잔여 스크래치 파일이 남아 있다"
    probe_src = (
        "from src.history.oos_guard import TRACK_INTRADAY, research_load\n"
        "\n"
        "\n"
        "def bad_call(db_path, symbol):\n"
        "    _EXTRA = {'unlock': object()}\n"
        "    return research_load(db_path, symbol, '1m', purpose='performance',\n"
        "                          track=TRACK_INTRADAY, **_EXTRA)\n"
    )
    try:
        with open(probe_path, "w", encoding="utf-8") as f:
            f.write(probe_src)

        scanned = {os.path.relpath(p, REPO) for p in _orb_scan_py_files()}
        rel = os.path.relpath(probe_path, REPO)
        assert rel in scanned, "analysis/ 하위 파일이 스캔 대상 목록에 없다(R4-8 회귀)"

        with open(probe_path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=probe_path)
        offenders = _research_load_offenders_in_tree(tree, rel)
        assert offenders != [], "analysis/ 하위 unlock 위반이 잡히지 않았다(R4-8 회귀)"
    finally:
        if os.path.exists(probe_path):
            os.remove(probe_path)
    assert not os.path.exists(probe_path)


# --------------------------------------------------------------------------
# 게이트 배선 — purpose="coverage" 문자열/직접 load_candles 참조 금지 (소스/AST 스캔)
# R4-8 — 스캔 범위는 `_orb_scan_py_files`(src/scripts/analysis, 재귀) 로 이미 확장돼 있다.
# --------------------------------------------------------------------------
_PURPOSE_COVERAGE_RE = re.compile(r"""purpose\s*=\s*["']coverage["']""")


def test_no_coverage_purpose_string_anywhere_in_orb_package():
    """`_ALLOWED_COVERAGE_STRING` 은 이미 검토된 pre-ORB coverage-only 스크립트 예외다(게이트
    자신의 정의처 포함) — 이 문자열 스캔은 AST 가 아니라 순수 텍스트 대조이므로 docstring
    안의 언급도 잡는다.

    R4-8 — 스캔 범위를 `src/`·`scripts/`·`analysis/` 전체로 넓히면서 패턴을
    `purpose="coverage"`(공백 허용) 로 좁혔다. 예전엔 `"coverage" in src` 만 봐서(문자열
    "coverage" 가 quote 안에 있기만 하면 잡음), ORB 와 무관하게 "coverage" 를 변수/딕셔너리
    키로 쓰는 기존 스크립트(`scripts/h2b_backfill_analysis.py`·`scripts/h3_regime_analysis.py`
    ·`src/runtime/isolation/manifest.py` — 데이터/세션 커버리지 통계, `research_load` 의
    `purpose` 인자와 무관)까지 오탐했다. 실제 위협(`purpose="coverage"` 키워드 인자)만
    정확히 잡는다."""
    offenders = []
    for path in sorted(_orb_scan_py_files()):
        rel = os.path.relpath(path, REPO)
        if rel in _ALLOWED_COVERAGE_STRING:
            continue
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if _PURPOSE_COVERAGE_RE.search(src):
            offenders.append(rel)
    assert offenders == [], f"purpose=\"coverage\" 로 읽힐 수 있는 문자열 발견: {offenders}"


def test_no_direct_load_candles_reference_in_orb_package():
    """`_ALLOWED_LOAD_CANDLES_REF` 는 `tests/test_oos_guard_wiring.py` 의 `ALLOWED` 와 동일
    (정의처·재노출·게이트 자신)."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(_orb_scan_py_files()):
        rel = os.path.relpath(path, REPO)
        if rel in _ALLOWED_LOAD_CANDLES_REF:
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        refs = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                    a.name == "load_candles" for a in node.names):
                refs.append(f"L{node.lineno}: import")
            elif isinstance(node, ast.Attribute) and node.attr == "load_candles":
                refs.append(f"L{node.lineno}: attribute")
            elif isinstance(node, ast.Name) and node.id == "load_candles":
                refs.append(f"L{node.lineno}: name")
        if refs:
            offenders[rel] = refs
    assert offenders == {}, f"load_candles 직접 참조 발견: {offenders}"
