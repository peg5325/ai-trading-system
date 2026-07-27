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
import glob
import os
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
ORB_SRC_GLOB = os.path.join(REPO, "src", "research", "orb", "*.py")

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
    """§loading.py 가 개장분을 570 으로 하드코딩하지 않고 market-aware
    `regular_session_window("US")` 로 유도하는지 확인한다(독립 리뷰 L2/L4/N07/N08)."""
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
# B1 — blind OOS 배선 회귀(실 스냅샷 경계 + unlock 부재/track=intraday AST 고정)
# --------------------------------------------------------------------------
@pytest.mark.skipif(
    not os.path.exists(REAL_SNAPSHOT_PATH),
    reason=(
        "실 P0-US30 스냅샷 부재 — 이 워크트리는 data/research_snapshots/ 를 다른 체크아웃과 "
        f"공유하지 않는다({REAL_SNAPSHOT_PATH})."
    ),
)
def test_blind_oos_boundary_on_real_snapshot():
    """B1(독립 리뷰 H1) — development 마지막 날은 로드되고, blind OOS 첫/마지막 날은 잠긴다.

    이전 판은 어떤 테스트도 OOS 구간을 실제로 로드해 보지 않아, 임의 `OOSUnlock` 주입이나
    `track="swing"` 같은 변조가 79 테스트를 전부 통과할 수 있었다.
    """
    got = load_symbol_day(REAL_SNAPSHOT_PATH, "AAPL", date(2026, 1, 20))   # development 마지막 날
    assert len(got) > 0
    with pytest.raises(OOSLockError):
        load_symbol_day(REAL_SNAPSHOT_PATH, "AAPL", date(2026, 1, 21))    # blind OOS 첫 날
    with pytest.raises(OOSLockError):
        load_symbol_day(REAL_SNAPSHOT_PATH, "AAPL", date(2026, 7, 21))    # blind OOS 마지막 날


def test_research_load_calls_never_pass_unlock_and_use_intraday_track():
    """AST: `src/research/orb/**` 의 `research_load` 호출은 `unlock` 인자를 절대 넘기지 않고,
    `track` 은 반드시 `TRACK_INTRADAY` 상수여야 한다(임의 `OOSUnlock` 주입·`track="swing"`
    변조를 정적으로 차단, 독립 리뷰 H1)."""
    offenders: list[str] = []
    for path in sorted(glob.glob(ORB_SRC_GLOB)):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "research_load"):
                continue
            rel = os.path.relpath(path, REPO)
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
    assert offenders == [], offenders


# --------------------------------------------------------------------------
# 게이트 배선 — purpose="coverage" 문자열/직접 load_candles 참조 금지 (소스/AST 스캔)
# --------------------------------------------------------------------------
def test_no_coverage_purpose_string_anywhere_in_orb_package():
    offenders = []
    for path in sorted(glob.glob(ORB_SRC_GLOB)):
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if "coverage" in src and ('"coverage"' in src or "'coverage'" in src):
            offenders.append(path)
    assert offenders == [], f"purpose=\"coverage\" 로 읽힐 수 있는 문자열 발견: {offenders}"


def test_no_direct_load_candles_reference_in_orb_package():
    offenders: dict[str, list[str]] = {}
    for path in sorted(glob.glob(ORB_SRC_GLOB)):
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
            offenders[path] = refs
    assert offenders == {}, f"load_candles 직접 참조 발견: {offenders}"
