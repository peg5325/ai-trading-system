"""Toss 과거 캔들 backfill 드라이버(PR 3-A) 검증.

FakeProvider 로 실 Toss API·실 sleep 없이 계약 불변식을 강제한다:
- source="toss_backfill", ts_utc.isoformat() → import 경로와 PK 정합
- resume(next_before 커서 저장·소비), 재실행 idempotent(전부 duplicate)
- nextBefore=null·빈 페이지 → exhausted=1, oldest ts 비감소 → no_progress 정지(무한루프 차단)
- 429 → 지수 backoff 재시도, max_retries 초과 → 종목 격리(다른 종목 계속)
- API 조회는 주문과 완전 분리(order/broker import 0)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from src.history import HistoryStore
from src.ingest.backfill_history import backfill_symbol, backfill_symbols
from src.models import Candle


def _c(symbol: str, minute: int, close: float = 100.0, currency: str = "USD") -> Candle:
    ts = f"2026-07-07T00:{minute:02d}:00+00:00"
    m = datetime.fromisoformat(ts)
    return Candle.from_moment(symbol=symbol, interval="1m", moment=m,
                             o=close, h=close + 1, low=close - 1, c=close, v=1000.0,
                             currency=currency, adjusted=True)


class FakeProvider:
    """(symbol, before) → (candles, nextBefore) 페이지 맵. fetch_page 만 노출."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[tuple[str, str | None]] = []

    def fetch_page(self, symbol: str, interval: str, *, count: int = 200,
                   before: str | None = None):
        self.calls.append((symbol, before))
        return self.pages[(symbol, before)]


class _HTTP429(Exception):
    def __init__(self):
        super().__init__("429 Too Many Requests")
        self.response = type("R", (), {"status_code": 429})()


class RateLimitedThenOk:
    """앞 fail_times 번은 429, 그 다음 정상 페이지 1개(nextBefore=None)."""

    def __init__(self, symbol: str, fail_times: int):
        self.symbol = symbol
        self.fail_times = fail_times
        self.calls = 0

    def fetch_page(self, symbol, interval, *, count=200, before=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _HTTP429()
        return ([_c(symbol, 0)], None)


class AlwaysRateLimited:
    def __init__(self):
        self.calls = 0

    def fetch_page(self, symbol, interval, *, count=200, before=None):
        self.calls += 1
        raise _HTTP429()


def _no_sleep():
    calls: list[float] = []
    return calls, (lambda s: calls.append(s))


def test_basic_pagination_upsert_exhausted(tmp_path):
    """2페이지 backfill → 전부 저장, source=toss_backfill, exhausted, market/adjusted 보존."""
    prov = FakeProvider({
        ("QQQ", None): ([_c("QQQ", 2), _c("QQQ", 1)], "cur1"),
        ("QQQ", "cur1"): ([_c("QQQ", 0)], None),
    })
    dbp = str(tmp_path / "market_history.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        res = backfill_symbol(store, prov, "QQQ", "1m", max_pages=10, sleep=sleep)
        assert res.pages_fetched == 2
        assert res.rows_seen == 3 and res.inserted == 3
        assert res.exhausted is True and res.stopped_reason == "nextbefore_null"
        assert store.total_rows() == 3
        # 저장 provenance/도메인 검증
        rows = store._con.execute(
            "SELECT DISTINCT source, market, adjusted FROM candle_history").fetchall()
        assert len(rows) == 1
        assert rows[0]["source"] == "toss_backfill"
        assert rows[0]["market"] == "US" and rows[0]["adjusted"] == 1
        # ts_utc 는 isoformat 문자열(import 경로와 PK 정합)
        earliest = store._con.execute("SELECT MIN(ts_utc) FROM candle_history").fetchone()[0]
        assert earliest == "2026-07-07T00:00:00+00:00"
        # ingest_log 감사 기록
        nlog = store._con.execute(
            "SELECT COUNT(*) FROM ingest_log WHERE source='toss_backfill'").fetchone()[0]
        assert nlog == 1


def test_max_pages_then_resume(tmp_path):
    """max_pages=1 로 중단(커서 저장) → resume 재실행이 next_before 부터 이어받는다."""
    prov = FakeProvider({
        ("QQQ", None): ([_c("QQQ", 2), _c("QQQ", 1)], "cur1"),
        ("QQQ", "cur1"): ([_c("QQQ", 0)], None),
    })
    dbp = str(tmp_path / "market_history.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        r1 = backfill_symbol(store, prov, "QQQ", "1m", max_pages=1, sleep=sleep)
        assert r1.pages_fetched == 1 and r1.exhausted is False
        assert r1.stopped_reason == "max_pages"
        cur = store.get_cursor("QQQ", "1m")
        assert cur is not None and cur["next_before"] == "cur1" and cur["exhausted"] is False
        assert store.total_rows() == 2

        r2 = backfill_symbol(store, prov, "QQQ", "1m", max_pages=10, sleep=sleep)
        # resume 는 next_before='cur1' 부터 fetch 해야 한다
        assert ("QQQ", "cur1") in prov.calls
        assert r2.exhausted is True
        assert store.total_rows() == 3


def test_tail_repair_preserves_existing_deep_cursor(tmp_path):
    """최신 구간 repair는 새 데이터를 저장하되 기존 과거 탐색 cursor를 덮어쓰지 않는다."""
    prov = FakeProvider({
        ("QQQ", None): ([_c("QQQ", 2), _c("QQQ", 1)], "recent-next"),
    })
    dbp = str(tmp_path / "market_history.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        store.save_cursor(
            "QQQ", "1m",
            oldest_ts_utc="2023-07-01T00:00:00+00:00",
            next_before="deep-history-next", exhausted=False)
        store.commit()
        before = store.get_cursor("QQQ", "1m")

        result = backfill_symbol(
            store, prov, "QQQ", "1m", max_pages=1, sleep=sleep,
            resume=False, persist_cursor=False, run_id="TAIL-REPAIR")

        assert result.cursor_persisted is False
        assert result.inserted == 2
        assert result.stopped_reason == "max_pages"
        assert store.total_rows() == 2
        assert store.get_cursor("QQQ", "1m") == before
        log = store._con.execute(
            "SELECT note FROM ingest_log WHERE run_id='TAIL-REPAIR'"
        ).fetchone()[0]
        assert "cursor_persisted=false" in log


def test_idempotent_rerun(tmp_path):
    """full backfill 재실행 → 신규 insert 0, 전부 duplicate, 총 행수 불변."""
    prov = FakeProvider({
        ("QQQ", None): ([_c("QQQ", 2), _c("QQQ", 1)], "cur1"),
        ("QQQ", "cur1"): ([_c("QQQ", 0)], None),
    })
    dbp = str(tmp_path / "market_history.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        backfill_symbol(store, prov, "QQQ", "1m", max_pages=10, sleep=sleep)
        assert store.total_rows() == 3
        # exhausted 커서 → resume 시 처음부터 재스캔(멱등 흡수)
        r2 = backfill_symbol(store, prov, "QQQ", "1m", max_pages=10, sleep=sleep)
        assert r2.inserted == 0 and r2.duplicate == 3 and r2.conflict == 0
        assert store.total_rows() == 3


def test_empty_page_stops_exhausted(tmp_path):
    prov = FakeProvider({("QQQ", None): ([], "cur1")})
    dbp = str(tmp_path / "market_history.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        res = backfill_symbol(store, prov, "QQQ", "1m", max_pages=10, sleep=sleep)
        assert res.rows_seen == 0 and res.exhausted is True
        assert res.stopped_reason == "empty_page"
        assert store.total_rows() == 0


def test_no_progress_guard_prevents_infinite_loop(tmp_path):
    """Toss 가 stale 커서로 같은 oldest 봉을 계속 반환 → oldest 비감소 감지 후 정지."""
    same = [_c("QQQ", 5)]
    prov = FakeProvider({
        ("QQQ", None): (same, "loop"),
        ("QQQ", "loop"): (same, "loop"),  # oldest 그대로
    })
    dbp = str(tmp_path / "market_history.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        res = backfill_symbol(store, prov, "QQQ", "1m", max_pages=1000, sleep=sleep)
        assert res.stopped_reason == "no_progress"
        assert res.pages_fetched <= 2  # max_pages 까지 폭주하지 않는다
        # 커서를 종료성으로 마킹 → 다음 실행이 같은 stale 커서를 무한 재fetch 하지 않음
        cur = store.get_cursor("QQQ", "1m")
        assert cur is not None and cur["exhausted"] is True
        # exhausted 커서라 소비되지 않으므로 다른 종료 경로와 동일하게 next_before=None
        assert cur["next_before"] is None


def test_resume_seeds_prev_oldest_detects_immediate_stall(tmp_path):
    """resume 시 커서 oldest 로 prev_oldest 시드 → 재개 첫 페이지에서 즉시 no_progress."""
    prov = FakeProvider({("QQQ", "loop"): ([_c("QQQ", 5)], "loop2")})  # oldest 00:05 (비감소)
    dbp = str(tmp_path / "market_history.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        # 미완 커서 사전 저장(oldest=00:05, next_before=loop)
        store.save_cursor("QQQ", "1m", oldest_ts_utc="2026-07-07T00:05:00+00:00",
                          next_before="loop", exhausted=False)
        store.commit()
        res = backfill_symbol(store, prov, "QQQ", "1m", max_pages=1000, sleep=sleep)
        assert res.stopped_reason == "no_progress"
        assert res.pages_fetched == 1  # 재개 첫 페이지에서 즉시 감지(1페이지 낭비 제거)


def test_ingest_log_attribution_and_unique_run_id(tmp_path):
    """감사 로그에 symbol/interval 귀속 + run_id 는 정적이 아님(실행 구분 가능)."""
    prov = FakeProvider({("QQQ", None): ([_c("QQQ", 0)], None)})
    dbp = str(tmp_path / "market_history.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        backfill_symbol(store, prov, "QQQ", "1m", max_pages=10, sleep=sleep)
        row = store._con.execute(
            "SELECT run_id, symbol, interval FROM ingest_log WHERE source='toss_backfill'"
        ).fetchone()
        assert row["symbol"] == "QQQ" and row["interval"] == "1m"
        assert row["run_id"] != "backfill-1m" and "1m" in row["run_id"]  # 정적 아님


def test_429_backoff_retry_then_success(tmp_path):
    """429 2회 → 지수 backoff sleep([base, base*2]) 후 3번째 성공."""
    prov = RateLimitedThenOk("QQQ", fail_times=2)
    dbp = str(tmp_path / "market_history.sqlite")
    sleeps, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        res = backfill_symbol(store, prov, "QQQ", "1m", max_pages=10,
                              max_retries=3, backoff_base=0.5, sleep=sleep)
        assert res.error is None
        assert store.total_rows() == 1
        assert prov.calls == 3  # 2 실패 + 1 성공
        assert sleeps == [0.5, 1.0]  # base*2^0, base*2^1


def test_symbol_isolation_on_persistent_429(tmp_path):
    """한 종목이 429 로 max_retries 초과해도 다른 종목 backfill 은 계속된다."""
    good = FakeProvider({("QQQ", None): ([_c("QQQ", 0)], None)})

    class _Router:
        def __init__(self):
            self.bad = AlwaysRateLimited()
        def fetch_page(self, symbol, interval, *, count=200, before=None):
            if symbol == "BAD":
                return self.bad.fetch_page(symbol, interval, count=count, before=before)
            return good.fetch_page(symbol, interval, count=count, before=before)

    dbp = str(tmp_path / "market_history.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        report = backfill_symbols(store, _Router(), ["BAD", "QQQ"], "1m",
                                  max_pages=10, max_retries=2, backoff_base=0.1, sleep=sleep)
        by_sym = {r.symbol: r for r in report.results}
        assert by_sym["BAD"].error is not None and "429" in by_sym["BAD"].error
        assert by_sym["QQQ"].error is None and by_sym["QQQ"].inserted == 1
        assert store.total_rows() == 1  # 정상 종목 데이터는 보존


def test_spacing_between_but_not_after_last_symbol(tmp_path):
    """spacing 은 종목 사이에만 — N종목이면 N-1회. 마지막 뒤 불필요 대기 없음."""
    prov = FakeProvider({
        ("A", None): ([_c("A", 0)], None),
        ("B", None): ([_c("B", 0)], None),
        ("C", None): ([_c("C", 0)], None),
    })
    dbp = str(tmp_path / "market_history.sqlite")
    sleeps, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        backfill_symbols(store, prov, ["A", "B", "C"], "1m",
                         max_pages=10, spacing=0.5, sleep=sleep)
        # 종목당 페이지 1개(nextBefore=None)라 페이지 간 sleep 은 없고, 종목 간 sleep 만 발생
        assert sleeps == [0.5, 0.5]  # A→B, B→C. C 뒤에는 없음


def test_non_retryable_error_isolated(tmp_path):
    class _Boom:
        def fetch_page(self, symbol, interval, *, count=200, before=None):
            if symbol == "X":
                raise ValueError("boom")
            return ([_c(symbol, 0)], None)

    dbp = str(tmp_path / "market_history.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        report = backfill_symbols(store, _Boom(), ["X", "QQQ"], "1m",
                                  max_pages=10, sleep=sleep)
        by_sym = {r.symbol: r for r in report.results}
        assert by_sym["X"].error is not None
        assert by_sym["QQQ"].inserted == 1
        assert store.total_rows() == 1


def test_no_order_or_broker_import():
    """자본 안전: backfill 드라이버·CLI 는 주문/broker/pipeline 을 import 하지 않는다."""
    banned = ("src.order", "src.broker", "src.pipeline", "src.execution",
              "place_order", "submit_order", "PaperBroker", "TossBroker")
    for path in ("src/ingest/backfill_history.py", "scripts/history_backfill.py"):
        src = Path(path).read_text(encoding="utf-8")
        for token in banned:
            assert token not in src, f"{path} 가 매매 경로({token})를 참조하면 안 됨"


# ---------------------------------------------------------------------------
# gap-skip (opt-in): Toss 1m API 의 UTC 일자 경계 절단 대응
#
# 실측(2026-07-18, SOXL/SPY)한 Toss 계약:
#   - before 기준 [before - count*1min, before] 구간만 반환
#   - **UTC 일자 경계를 넘지 않는다** → 00:01Z 까지 가면 nextBefore=null
#   - 경계 시각(00:00Z)으로 조회하면 빈 페이지 + nextBefore=null
#   그 결과 gap-skip 없이는 종목이 "하루치"에서 영구 정지한다.
# ---------------------------------------------------------------------------
from src.ingest.backfill_history import _shift_before  # noqa: E402


def _c_at(symbol: str, iso: str, close: float = 100.0) -> Candle:
    m = datetime.fromisoformat(iso)
    return Candle.from_moment(symbol=symbol, interval="1m", moment=m,
                              o=close, h=close + 1, low=close - 1, c=close, v=1000.0,
                              currency="USD", adjusted=True)


class UtcDayBoundaryProvider:
    """Toss 실계약 모사: 요청창을 UTC 일자 안으로 자르고, 하루 시작에 닿으면 nextBefore=None.

    데이터는 days 에 준 각 UTC 일자의 00:01~23:59 에 1분봉이 존재한다고 본다.
    """

    def __init__(self, days: set[str], window: int = 200):
        self.days = days
        self.window = window
        self.calls: list[str | None] = []

    def fetch_page(self, symbol, interval, *, count=200, before=None):
        self.calls.append(before)
        assert before is not None, "테스트는 항상 명시적 before 로 시작한다"
        end = datetime.fromisoformat(before.replace("Z", "+00:00"))
        day = end.date().isoformat()
        start_of_day = end.replace(hour=0, minute=1, second=0, microsecond=0)
        # 일자 경계 밖(00:00Z 정각 등) 또는 데이터 없는 날 → 빈 페이지
        if day not in self.days or end < start_of_day:
            return ([], None)
        win_start = max(start_of_day, end - timedelta(minutes=self.window - 1))
        bars = []
        t = win_start
        while t <= end:
            bars.append(_c_at(symbol, t.isoformat()))
            t += timedelta(minutes=1)
        if not bars:
            return ([], None)
        nxt = None if win_start <= start_of_day else (win_start - timedelta(minutes=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        return (bars, nxt)


def test_shift_before_normalizes_offset_to_utc():
    """Toss 는 +09:00 오프셋으로 nextBefore 를 준다 → UTC 정규화 후 이동."""
    assert _shift_before("2026-07-17T09:40:00.000+09:00", 1) == "2026-07-17T00:39:00Z"
    # 같은 일자 안에서는 요청한 만큼 이동한다.
    assert _shift_before("2026-07-17T12:00:00Z", 200) == "2026-07-17T08:40:00Z"


def test_shift_before_never_crosses_utc_day_boundary():
    """★ 유실 방지: 경계를 넘는 이동은 직전 일자 마지막 분에서 멈춘다.

    빈 응답은 `before` 가 속한 일자 안쪽만 확인해 준다. 경계를 넘어 건너뛰면 직전
    일자의 꼬리(예: 20:41~23:59Z)를 조회 없이 버린다 — 실제로 그렇게 유실됐었다.
    """
    # 00:00Z 는 이미 일자 시작 → 직전 일자 23:59Z
    assert _shift_before("2026-07-17T00:00:00Z", 200) == "2026-07-16T23:59:00Z"
    # 00:01Z 에서 1분 이동해도 00:00Z 가 아니라 직전 일자 23:59Z(00:00Z 는 항상 빈 응답)
    assert _shift_before("2026-07-17T00:01:00Z", 1) == "2026-07-16T23:59:00Z"
    # 200분 이동이 경계를 넘으면 클램프
    assert _shift_before("2026-07-17T02:00:00Z", 200) == "2026-07-16T23:59:00Z"
    # 경계에 정확히 걸쳐도 클램프
    assert _shift_before("2026-07-17T03:20:00Z", 200) == "2026-07-16T23:59:00Z"


def _seed_cursor(store, symbol, before):
    store.save_cursor(symbol, "1m", oldest_ts_utc=None, next_before=before, exhausted=False)
    store.commit()


def test_without_gap_skip_stops_at_utc_day_boundary(tmp_path):
    """회귀 고정: gap-skip 없으면 하루치에서 멈춘다(= 현재까지의 backfill 이 얕았던 원인)."""
    prov = UtcDayBoundaryProvider(days={"2026-07-17", "2026-07-16", "2026-07-15"})
    _, sleeper = _no_sleep()
    with HistoryStore(str(tmp_path / "h.sqlite")) as store:
        _seed_cursor(store, "SOXL", "2026-07-17T04:00:00Z")
        res = backfill_symbol(store, prov, "SOXL", "1m", max_pages=50,
                              sleep=sleeper, resume=True,
                              run_id="t", page_size=200)
    assert res.stopped_reason in {"nextbefore_null", "empty_page"}
    assert res.exhausted is True


def test_gap_skip_crosses_utc_day_boundary_and_collects_multiple_days(tmp_path):
    """gap-skip 켜면 nextBefore=null(경계)을 넘어 이전 UTC 일자까지 계속 수집한다."""
    prov = UtcDayBoundaryProvider(days={"2026-07-17", "2026-07-16", "2026-07-15"})
    _, sleeper = _no_sleep()
    db = str(tmp_path / "h.sqlite")
    with HistoryStore(db) as store:
        _seed_cursor(store, "SOXL", "2026-07-17T04:00:00Z")
        res = backfill_symbol(store, prov, "SOXL", "1m", max_pages=100,
                              sleep=sleeper, resume=True, run_id="t",
                              page_size=200, gap_skip_minutes=200,
                              max_consecutive_empty=10)
        rows = store._con.execute(
            "SELECT COUNT(*), COUNT(DISTINCT substr(ts_utc,1,10)) FROM candle_history "
            "WHERE symbol='SOXL'").fetchone()
    assert res.gap_skips >= 2, "일자 경계를 최소 2회(17→16→15) 넘어야 한다"
    assert rows[1] >= 3, f"3개 UTC 일자를 수집해야 한다(실제 {rows[1]})"
    assert rows[0] > 1000, "하루치(≈240봉)보다 훨씬 많아야 한다"


def test_gap_skip_steps_over_empty_window_without_losing_data(tmp_path):
    """휴장일(빈 창)은 건너뛰고 그 이전 거래일 데이터를 정상 수집한다."""
    # 07-16 은 휴장(데이터 없음) → 07-17 에서 07-15 로 건너뛰어야 한다
    prov = UtcDayBoundaryProvider(days={"2026-07-17", "2026-07-15"})
    _, sleeper = _no_sleep()
    db = str(tmp_path / "h.sqlite")
    with HistoryStore(db) as store:
        _seed_cursor(store, "SOXL", "2026-07-17T04:00:00Z")
        res = backfill_symbol(store, prov, "SOXL", "1m", max_pages=100,
                              sleep=sleeper, resume=True, run_id="t",
                              page_size=200, gap_skip_minutes=200,
                              max_consecutive_empty=20)
        days = [r[0] for r in store._con.execute(
            "SELECT DISTINCT substr(ts_utc,1,10) FROM candle_history WHERE symbol='SOXL' "
            "ORDER BY 1").fetchall()]
    assert days == ["2026-07-15", "2026-07-17"], f"휴장일 건너뛰고 양쪽 수집해야 한다: {days}"
    assert res.gap_skips >= 1


def test_gap_skip_budget_stops_on_true_exhaustion(tmp_path):
    """연속 빈 창이 예산을 넘으면 진짜 소진으로 판정하고 정지(무한 호출 방지)."""
    prov = UtcDayBoundaryProvider(days={"2026-07-17"})
    _, sleeper = _no_sleep()
    db = str(tmp_path / "h.sqlite")
    with HistoryStore(db) as store:
        _seed_cursor(store, "SOXL", "2026-07-17T04:00:00Z")
        res = backfill_symbol(store, prov, "SOXL", "1m", max_pages=200,
                              sleep=sleeper, resume=True, run_id="t",
                              page_size=200, gap_skip_minutes=200,
                              max_consecutive_empty=5)
    assert res.stopped_reason == "empty_budget_exhausted"
    assert res.exhausted is True
    assert res.gap_skips >= 5


def test_gap_skip_default_off_preserves_legacy_behavior(tmp_path):
    """기본값(0)에서는 기존 계약 그대로 — 기존 호출자 영향 0."""
    pages = {("AAA", None): ([_c("AAA", 5)], None)}
    prov = FakeProvider(pages)
    _, sleeper = _no_sleep()
    with HistoryStore(str(tmp_path / "h.sqlite")) as store:
        res = backfill_symbol(store, prov, "AAA", "1m", max_pages=10,
                              sleep=sleeper, resume=False, run_id="t")
    assert res.stopped_reason == "nextbefore_null" and res.exhausted is True
    assert res.gap_skips == 0



def _collected_minutes(store, symbol: str) -> set[str]:
    rows = store._con.execute(
        "SELECT ts_utc FROM candle_history WHERE symbol=? ORDER BY ts_utc",
        (symbol,)).fetchall()
    return {r[0] for r in rows}


def test_no_minute_is_lost_across_day_boundaries(tmp_path):
    """★ 회귀 고정: 데이터가 있는 날은 분봉이 **빠짐없이** 수집된다.

    기존 테스트는 일자 수/총 봉 수의 **하한**만 봐서 경계 유실을 통과시켰다.
    여기서는 존재해야 할 분봉 집합과 수집 집합을 직접 비교한다.
    """
    days = {"2026-07-17", "2026-07-16", "2026-07-15"}
    prov = UtcDayBoundaryProvider(days=days)
    _, sleeper = _no_sleep()
    db = str(tmp_path / "h.sqlite")
    with HistoryStore(db) as store:
        _seed_cursor(store, "SOXL", "2026-07-17T23:59:00Z")
        backfill_symbol(store, prov, "SOXL", "1m", max_pages=200,
                        sleep=sleeper, resume=True, run_id="t",
                        page_size=200, gap_skip_minutes=200,
                        max_consecutive_empty=20)
        got = _collected_minutes(store, "SOXL")

    # UtcDayBoundaryProvider 계약: 각 일자 00:01~23:59 에 1분봉이 존재
    expected = set()
    for day in days:
        base = datetime.fromisoformat(f"{day}T00:01:00+00:00")
        for i in range(1439):
            expected.add((base + timedelta(minutes=i)).isoformat())

    missing = expected - got
    assert not missing, (
        f"{len(missing)}개 분봉 유실. 예: {sorted(missing)[:3]} … "
        f"(기대 {len(expected)} / 수집 {len(got & expected)})")


def test_day_boundary_costs_no_wasted_00_00_call(tmp_path):
    """00:00Z 는 계약상 항상 빈 응답 → 조회하지 않는다(낭비 호출 제거)."""
    prov = UtcDayBoundaryProvider(days={"2026-07-17", "2026-07-16"})
    _, sleeper = _no_sleep()
    with HistoryStore(str(tmp_path / "h.sqlite")) as store:
        _seed_cursor(store, "SOXL", "2026-07-17T23:59:00Z")
        backfill_symbol(store, prov, "SOXL", "1m", max_pages=100,
                        sleep=sleeper, resume=True, run_id="t",
                        page_size=200, gap_skip_minutes=200,
                        max_consecutive_empty=20)
    midnight_calls = [b for b in prov.calls if b and b.endswith("T00:00:00Z")]
    assert not midnight_calls, f"00:00Z 를 조회했다: {midnight_calls}"


def test_empty_fetches_are_counted_separately_from_pages(tmp_path):
    """빈 호출은 pages 예산 밖이므로 호출자가 볼 수 있게 계수된다."""
    prov = UtcDayBoundaryProvider(days={"2026-07-17", "2026-07-15"})   # 07-16 휴장
    _, sleeper = _no_sleep()
    with HistoryStore(str(tmp_path / "h.sqlite")) as store:
        _seed_cursor(store, "SOXL", "2026-07-17T23:59:00Z")
        res = backfill_symbol(store, prov, "SOXL", "1m", max_pages=100,
                              sleep=sleeper, resume=True, run_id="t",
                              page_size=200, gap_skip_minutes=200,
                              max_consecutive_empty=30)
    assert res.empty_fetches > 0, "휴장일을 건너뛰었으면 빈 호출이 있어야 한다"
    assert len(prov.calls) == res.pages_fetched + res.empty_fetches, \
        "총 API 호출 = 데이터 페이지 + 빈 호출"
