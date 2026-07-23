"""backfill 파이프라인의 완료-거래일 cutoff 배선(이슈 #72).

강제 불변식:
- cursor(oldest/newest/next_before)는 **필터 전 전체 페이지** 기준으로 전진(cutoff 가 탐색 깊이 불변).
- cutoff 는 SessionFilter **전에** 적용(raw→parse→cutoff→session→upsert 순서).
- cutoff 로 제외된 봉도 raw 스냅샷에는 원본 그대로 존재.
- resolved completed_through·제외 행수가 결과/ingest_log 에 기록.
- 1d·1m 동일 경계. resume 재실행 멱등.

FakeProvider 로 실 API·실 sleep 없이 강제한다.
"""
from __future__ import annotations

from datetime import date, datetime

from src.history import HistoryStore
from src.history.raw_snapshot import RawSnapshotStore
from src.history.session_filter import SessionFilter
from src.ingest.backfill_history import backfill_symbol, backfill_symbols
from src.models import Candle


def _c(symbol: str, iso_et: str, interval: str = "1m", close: float = 100.0) -> Candle:
    m = datetime.fromisoformat(iso_et)
    return Candle.from_moment(symbol=symbol, interval=interval, moment=m,
                              o=close, h=close + 1, low=close - 1, c=close, v=1000.0,
                              currency="USD", adjusted=True)


class FakeProvider:
    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[tuple[str, str | None]] = []

    def fetch_page(self, symbol, interval, *, count=200, before=None):
        self.calls.append((symbol, before))
        return self.pages[(symbol, before)]


class RawEmittingProvider:
    """raw_sink 를 지원하는 fake — parse 전 payload(제외 봉 포함)를 emit 한다."""

    def __init__(self, pages: dict, raw_payloads: dict):
        self.pages = pages
        self.raw_payloads = raw_payloads
        self.calls: list[tuple[str, str | None]] = []

    def fetch_page(self, symbol, interval, *, count=200, before=None, raw_sink=None):
        self.calls.append((symbol, before))
        if raw_sink is not None:
            raw_sink(symbol=symbol, interval=interval, before=before,
                     requested_at="2026-01-01T00:00:00Z",
                     received_at="2026-01-01T00:00:01Z", source="fake",
                     payload=self.raw_payloads[(symbol, before)])
        return self.pages[(symbol, before)]


def _no_sleep():
    calls: list[float] = []
    return calls, (lambda s: calls.append(s))


# --------------------------------------------------------------------------
# 5) cutoff 당일 포함, 다음 거래일 제외 — 1m
# --------------------------------------------------------------------------
def test_cutoff_excludes_future_day_1m(tmp_path):
    keep = _c("QQQ", "2025-07-15T15:30:00-04:00")   # REGULAR 07-15
    drop = _c("QQQ", "2025-07-16T08:00:00-04:00")   # PRE 07-16(미완성 진행일)
    prov = FakeProvider({("QQQ", None): ([drop, keep], None)})
    dbp = str(tmp_path / "m.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        res = backfill_symbol(store, prov, "QQQ", "1m", max_pages=5, sleep=sleep,
                              completed_through=date(2025, 7, 15))
        assert res.rows_seen == 2          # 전체 페이지 관측
        assert res.rows_stored == 1        # cutoff 후 저장
        assert res.rows_excluded_after_cutoff == 1
        assert res.completed_through == "2025-07-15"
        assert store.total_rows() == 1
        days = [r[0] for r in store._con.execute(
            "SELECT substr(ts_exchange,1,10) FROM candle_history").fetchall()]
        assert days == ["2025-07-15"]


# --------------------------------------------------------------------------
# 6) 1d 도 동일 경계
# --------------------------------------------------------------------------
def test_cutoff_excludes_future_day_1d(tmp_path):
    keep = _c("SPY", "2025-07-15T00:00:00-04:00", interval="1d")
    drop = _c("SPY", "2025-07-16T00:00:00-04:00", interval="1d")
    prov = FakeProvider({("SPY", None): ([drop, keep], None)})
    dbp = str(tmp_path / "m.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        res = backfill_symbol(store, prov, "SPY", "1d", max_pages=5, sleep=sleep,
                              completed_through=date(2025, 7, 15))
        assert res.rows_stored == 1 and res.rows_excluded_after_cutoff == 1
        assert store.total_rows() == 1
        # 1d note 도 cutoff provenance 를 담되 daily 파서가 stopped_reason 을 정상 추출.
        note = store._con.execute(
            "SELECT note FROM ingest_log WHERE symbol='SPY'").fetchone()[0]
        assert "completed_through=2025-07-15" in note
        assert "reason=nextbefore_null" in note


# --------------------------------------------------------------------------
# 7) cursor 는 필터 전 페이지 기준으로 전진(전부 제외돼도 다음 페이지로)
# --------------------------------------------------------------------------
def test_cursor_advances_when_page_fully_excluded(tmp_path):
    # 1페이지: 전부 cutoff 이후(제외), nextBefore=cur1 → 2페이지: cutoff 이하(저장).
    d1 = _c("QQQ", "2025-07-17T10:00:00-04:00")  # 제외
    d2 = _c("QQQ", "2025-07-16T10:00:00-04:00")  # 제외
    keep = _c("QQQ", "2025-07-15T10:00:00-04:00")  # 저장
    prov = FakeProvider({
        ("QQQ", None): ([d1, d2], "cur1"),
        ("QQQ", "cur1"): ([keep], None),
    })
    dbp = str(tmp_path / "m.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        res = backfill_symbol(store, prov, "QQQ", "1m", max_pages=5, sleep=sleep,
                              completed_through=date(2025, 7, 15))
        assert res.pages_fetched == 2               # 첫 페이지 전부 제외돼도 계속 페이징
        assert res.rows_excluded_after_cutoff == 2
        assert res.rows_stored == 1
        assert store.total_rows() == 1
    # cursor 가 필터 전 첫 페이지 oldest(cur1) 를 소비했음을 호출 순서로 확인.
    assert ("QQQ", "cur1") in prov.calls


# --------------------------------------------------------------------------
# 8) raw 스냅샷은 cutoff 제외 봉도 원본 그대로 보존
# --------------------------------------------------------------------------
def test_raw_snapshot_retains_cutoff_excluded(tmp_path):
    keep = _c("QQQ", "2025-07-15T15:30:00-04:00")
    drop = _c("QQQ", "2025-07-16T08:00:00-04:00")
    raw_payload = {"candles": [{"timestamp": "2025-07-16T08:00:00-04:00"},
                               {"timestamp": "2025-07-15T15:30:00-04:00"}],
                   "nextBefore": None}
    prov = RawEmittingProvider(
        pages={("QQQ", None): ([drop, keep], None)},
        raw_payloads={("QQQ", None): raw_payload})
    dbp = str(tmp_path / "m.sqlite")
    raw_root = str(tmp_path / "raw")
    raw = RawSnapshotStore(raw_root)
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        res = backfill_symbol(store, prov, "QQQ", "1m", max_pages=5, sleep=sleep,
                              completed_through=date(2025, 7, 15), raw_sink=raw.emit)
        assert res.rows_stored == 1 and res.rows_excluded_after_cutoff == 1
        assert store.total_rows() == 1
    # DB 에는 제외 봉 없음
    stored_days = [r[0] for r in HistoryStore(dbp)._con.execute(
        "SELECT substr(ts_exchange,1,10) FROM candle_history").fetchall()]
    assert stored_days == ["2025-07-15"]
    # raw 스냅샷에는 제외 봉(07-16)이 원본 그대로 존재
    files = raw.list_content_files()
    assert len(files) == 1
    payload = raw.read_payload(files[0])
    ts = [c["timestamp"] for c in payload["candles"]]
    assert "2025-07-16T08:00:00-04:00" in ts


# --------------------------------------------------------------------------
# 9) resolved cutoff·제외 행수가 ingest_log 에 기록(1m 구조화 노트)
# --------------------------------------------------------------------------
def test_cutoff_recorded_in_ingest_log_1m(tmp_path):
    keep = _c("QQQ", "2025-07-15T15:30:00-04:00")
    drop = _c("QQQ", "2025-07-16T08:00:00-04:00")
    prov = FakeProvider({("QQQ", None): ([drop, keep], None)})
    dbp = str(tmp_path / "m.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        backfill_symbol(store, prov, "QQQ", "1m", max_pages=5, sleep=sleep,
                        completed_through=date(2025, 7, 15),
                        session_filter=SessionFilter.default())
        note = store._con.execute(
            "SELECT note FROM ingest_log WHERE symbol='QQQ'").fetchone()[0]
    assert "completed_through=2025-07-15" in note
    assert "excluded_after_cutoff=1" in note
    assert "reason=" in note              # 구조화 노트(파서 호환)


# --------------------------------------------------------------------------
# 12) cutoff 는 SessionFilter 전에 적용(순서) + unknown session fail-closed 불변
# --------------------------------------------------------------------------
def test_cutoff_before_session_filter_order(tmp_path):
    # 07-15 REGULAR(저장), 07-15 OVERNIGHT(세션 제외), 07-16 REGULAR(cutoff 제외).
    reg = _c("QQQ", "2025-07-15T15:30:00-04:00")            # REGULAR
    overnight = _c("QQQ", "2025-07-15T02:00:00-04:00")      # OVERNIGHT(같은 일자)
    future = _c("QQQ", "2025-07-16T15:30:00-04:00")         # REGULAR, cutoff 이후
    prov = FakeProvider({("QQQ", None): ([reg, overnight, future], None)})
    dbp = str(tmp_path / "m.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        res = backfill_symbol(store, prov, "QQQ", "1m", max_pages=5, sleep=sleep,
                              completed_through=date(2025, 7, 15),
                              session_filter=SessionFilter.default())
        assert res.rows_excluded_after_cutoff == 1          # future(07-16)
        assert res.excluded_by_session.get("OVERNIGHT") == 1  # cutoff 통과 후 세션 제외
        assert res.rows_stored == 1                          # reg 만 저장
        assert store.total_rows() == 1


# --------------------------------------------------------------------------
# 11) resume 재실행 멱등(cutoff 유지)
# --------------------------------------------------------------------------
def test_cutoff_idempotent_resume(tmp_path):
    keep = _c("QQQ", "2025-07-15T15:30:00-04:00")
    drop = _c("QQQ", "2025-07-16T08:00:00-04:00")
    pages = {("QQQ", None): ([drop, keep], None)}
    dbp = str(tmp_path / "m.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        backfill_symbol(store, FakeProvider(pages), "QQQ", "1m", max_pages=5, sleep=sleep,
                        completed_through=date(2025, 7, 15))
        res2 = backfill_symbol(store, FakeProvider(pages), "QQQ", "1m", max_pages=5,
                               sleep=sleep, completed_through=date(2025, 7, 15))
        assert store.total_rows() == 1          # 중복 저장 없음
        assert res2.rows_excluded_after_cutoff == 1
        assert res2.duplicate == 1              # 재수집분은 전부 duplicate


# --------------------------------------------------------------------------
# backfill_symbols(다종목)도 cutoff 전달
# --------------------------------------------------------------------------
def test_cutoff_threaded_through_backfill_symbols(tmp_path):
    keep = _c("QQQ", "2025-07-15T15:30:00-04:00")
    drop = _c("QQQ", "2025-07-16T08:00:00-04:00")
    prov = FakeProvider({("QQQ", None): ([drop, keep], None)})
    dbp = str(tmp_path / "m.sqlite")
    _, sleep = _no_sleep()
    with HistoryStore(dbp) as store:
        report = backfill_symbols(store, prov, ["QQQ"], "1m", max_pages=5, sleep=sleep,
                                  completed_through=date(2025, 7, 15))
        r = report.results[0]
        assert r.rows_excluded_after_cutoff == 1 and r.rows_stored == 1
        d = r.as_dict()
        assert d["completed_through"] == "2025-07-15"
        assert d["rows_excluded_after_cutoff"] == 1
        assert d["excluded_after_cutoff_day_range"] == "2025-07-16"
