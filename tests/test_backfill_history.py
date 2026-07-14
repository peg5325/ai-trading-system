"""Toss 과거 캔들 backfill 드라이버(PR 3-A) 검증.

FakeProvider 로 실 Toss API·실 sleep 없이 계약 불변식을 강제한다:
- source="toss_backfill", ts_utc.isoformat() → import 경로와 PK 정합
- resume(next_before 커서 저장·소비), 재실행 idempotent(전부 duplicate)
- nextBefore=null·빈 페이지 → exhausted=1, oldest ts 비감소 → no_progress 정지(무한루프 차단)
- 429 → 지수 backoff 재시도, max_retries 초과 → 종목 격리(다른 종목 계속)
- API 조회는 주문과 완전 분리(order/broker import 0)
"""
from __future__ import annotations

from datetime import datetime
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
