"""backfill report 범위 의미 구분 + 공유 collection run_id (이슈 #79 Scope B + run identity).

- fetched_earliest/latest = provider 가 돌려준 전체 candle 범위(cutoff/session 제외분 포함).
- accepted_earliest/latest = cutoff + SessionFilter 를 통과해 upsert 로 전달된 이 실행의 범위.
- 기존 earliest/latest 는 fetched 의 deprecated alias(legacy_fetch_range) 로 유지된다.
- 한 backfill_symbols 호출은 공유 run_id 를 전 종목·ingest_log 에 남기고 report 로 노출한다(#79).
- run_id 로 실제 수집 → manifest 교차검증까지 end-to-end VERIFIED(자본 경로 무접촉).
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from src.history import HistoryStore
from src.history import daily_manifest as dm
from src.history.ingest_provenance import parse_ingest_note
from src.history.session_filter import SessionFilter
from src.ingest.backfill_history import backfill_symbol, backfill_symbols
from src.models import ET, Candle

YAML = "config/universe_p0_us30.yaml"


def _c(symbol: str, moment: datetime, interval: str = "1m", close: float = 100.0) -> Candle:
    return Candle.from_moment(symbol=symbol, interval=interval, moment=moment,
                              o=close, h=close + 1, low=close - 1, c=close, v=1000.0,
                              currency="USD", adjusted=True)


def _et(y, mo, d, h, mi) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=ET)


class FakeProvider:
    def __init__(self, pages: dict):
        self.pages = pages

    def fetch_page(self, symbol, interval, *, count=200, before=None):
        return self.pages[(symbol, before)]


def _no_sleep(_s):
    pass


# ==========================================================================
# test #12: fetched 범위와 accepted 범위가 다른 fixture (cutoff 제외)
# ==========================================================================
def test_fetched_vs_accepted_differ_by_cutoff(tmp_path):
    # A(07-20) B(07-21=cutoff) 는 kept, C(07-22) 는 cutoff 초과로 제외.
    a = _c("QQQ", _et(2026, 7, 20, 10, 30))
    b = _c("QQQ", _et(2026, 7, 21, 10, 30))
    cc = _c("QQQ", _et(2026, 7, 22, 10, 30))
    prov = FakeProvider({("QQQ", None): ([cc, b, a], None)})
    dbp = str(tmp_path / "m.sqlite")
    with HistoryStore(dbp) as store:
        res = backfill_symbol(store, prov, "QQQ", "1m", max_pages=5, sleep=_no_sleep,
                              completed_through=date(2026, 7, 21))
    # fetched 는 C(07-22) 까지, accepted 는 B(07-21) 까지.
    assert res.earliest == a.ts_utc.isoformat()
    assert res.latest == cc.ts_utc.isoformat()          # fetched latest = 제외된 C
    assert res.accepted_earliest == a.ts_utc.isoformat()
    assert res.accepted_latest == b.ts_utc.isoformat()  # accepted latest = kept B
    assert res.latest != res.accepted_latest
    assert res.rows_excluded_after_cutoff == 1

    d = res.as_dict()
    assert d["fetched_latest"] == cc.ts_utc.isoformat()
    assert d["accepted_latest"] == b.ts_utc.isoformat()


# ==========================================================================
# test #13: 페이지 전체가 cutoff 로 제외 → fetched != None, accepted == None
# ==========================================================================
def test_whole_page_excluded_accepted_none(tmp_path):
    # 전 candle 이 cutoff(07-21) 초과 → 전량 제외.
    x = _c("QQQ", _et(2026, 7, 22, 10, 30))
    y = _c("QQQ", _et(2026, 7, 23, 10, 30))
    prov = FakeProvider({("QQQ", None): ([y, x], None)})
    dbp = str(tmp_path / "m.sqlite")
    with HistoryStore(dbp) as store:
        res = backfill_symbol(store, prov, "QQQ", "1m", max_pages=5, sleep=_no_sleep,
                              completed_through=date(2026, 7, 21))
        assert store.total_rows() == 0                  # 아무것도 저장 안 됨
    assert res.earliest is not None and res.latest is not None   # fetched 는 존재
    assert res.accepted_earliest is None and res.accepted_latest is None
    assert res.rows_stored == 0 and res.rows_excluded_after_cutoff == 2


# ==========================================================================
# test #14: SessionFilter 제외가 accepted 범위에 반영됨
# ==========================================================================
def test_session_filter_reflected_in_accepted(tmp_path):
    r1 = _c("QQQ", _et(2026, 7, 20, 10, 30))   # REGULAR
    r2 = _c("QQQ", _et(2026, 7, 20, 15, 0))    # REGULAR
    p1 = _c("QQQ", _et(2026, 7, 20, 17, 0))    # POST — 세션 필터로 제외
    assert (r1.session, r2.session, p1.session) == ("REGULAR", "REGULAR", "POST")
    prov = FakeProvider({("QQQ", None): ([p1, r2, r1], None)})
    sf = SessionFilter.from_spec("REGULAR")
    dbp = str(tmp_path / "m.sqlite")
    with HistoryStore(dbp) as store:
        res = backfill_symbol(store, prov, "QQQ", "1m", max_pages=5, sleep=_no_sleep,
                              session_filter=sf)
    # fetched latest = POST(21:00 UTC), accepted latest = REGULAR r2(19:00 UTC).
    assert res.latest == p1.ts_utc.isoformat()
    assert res.accepted_latest == r2.ts_utc.isoformat()
    assert res.accepted_earliest == r1.ts_utc.isoformat()
    assert res.excluded_by_session.get("POST") == 1


# ==========================================================================
# test #15: 기존 earliest/latest alias 의미 고정(legacy_fetch_range)
# ==========================================================================
def test_legacy_earliest_latest_is_fetched_alias(tmp_path):
    a = _c("QQQ", _et(2026, 7, 20, 10, 30))
    prov = FakeProvider({("QQQ", None): ([a], None)})
    dbp = str(tmp_path / "m.sqlite")
    with HistoryStore(dbp) as store:
        res = backfill_symbol(store, prov, "QQQ", "1m", max_pages=5, sleep=_no_sleep)
    d = res.as_dict()
    assert d["earliest"] == d["fetched_earliest"] == res.earliest
    assert d["latest"] == d["fetched_latest"] == res.latest
    assert "legacy_fetch_range" in d["range_semantics"]["earliest"]
    assert "accepted" in d["range_semantics"]


# ==========================================================================
# test #1: 공유 collection run_id 가 전 종목 report/ingest_log 에 남는다
# ==========================================================================
def test_shared_run_id_across_symbols(tmp_path):
    syms = dm.read_p0_universe(YAML)["symbols"]   # 30종목
    pages = {(s, None): ([_c(s, _et(2026, 7, 15, 10, 30), interval="1d")], None)
             for s in syms}
    prov = FakeProvider(pages)
    dbp = str(tmp_path / "m.sqlite")
    with HistoryStore(dbp) as store:
        report = backfill_symbols(store, prov, syms, "1d", max_pages=3, sleep=_no_sleep,
                                  run_id="RUN-SHARED", completed_through=date(2026, 7, 21))
    assert report.run_id == "RUN-SHARED"
    assert report.as_dict()["run_id"] == "RUN-SHARED"
    assert len(report.results) == 30
    # ingest_log: 30 행 모두 같은 run_id + completed_through 부기.
    con = __import__("sqlite3").connect(dbp)
    rows = con.execute(
        "SELECT symbol, run_id, note FROM ingest_log WHERE interval='1d'").fetchall()
    con.close()
    assert len(rows) == 30
    assert {r[1] for r in rows} == {"RUN-SHARED"}
    assert {r[0] for r in rows} == set(syms)
    for _sym, _rid, note in rows:
        assert parse_ingest_note(note)["completed_through"] == "2026-07-21"


# ==========================================================================
# end-to-end: 실 수집(run_id) → manifest 교차검증 VERIFIED (자본 경로 무접촉)
# ==========================================================================
def test_end_to_end_run_id_to_manifest_verified(tmp_path):
    syms = dm.read_p0_universe(YAML)["symbols"]
    pages = {(s, None): ([_c(s, _et(2026, 7, 15, 10, 30), interval="1d")], None)
             for s in syms}
    prov = FakeProvider(pages)
    dbp = str(tmp_path / "m.sqlite")
    with HistoryStore(dbp) as store:
        backfill_symbols(store, prov, syms, "1d", max_pages=3, sleep=_no_sleep,
                         run_id="RUN-E2E", completed_through=date(2026, 7, 21))
    man = dm.build_manifest(dbp, YAML, collection_params={}, collection_code_commit=None,
                            as_of="2026-07-21", collection_completed_through="2026-07-21",
                            collection_run_id="RUN-E2E")
    pv = man["provenance_verification"]
    assert pv["provenance_status"] == "VERIFIED"
    assert pv["verified_completed_through"] == "2026-07-21"
    assert pv["verified_symbol_count"] == 30


def test_resume_same_run_id_reuse(tmp_path):
    """중단→resume: 같은 run_id 로 이어 수집하면 manifest 가 하나의 collection 으로 검증한다."""
    syms = dm.read_p0_universe(YAML)["symbols"]
    # 1차: 앞 절반만 수집(같은 run_id).
    pages = {(s, None): ([_c(s, _et(2026, 7, 15, 10, 30), interval="1d")], None)
             for s in syms}
    prov = FakeProvider(pages)
    dbp = str(tmp_path / "m.sqlite")
    with HistoryStore(dbp) as store:
        backfill_symbols(store, prov, syms[:15], "1d", max_pages=3, sleep=_no_sleep,
                         run_id="RUN-R", completed_through=date(2026, 7, 21))
    # 2차: 나머지 절반을 **같은 run_id** 로 resume.
    with HistoryStore(dbp) as store:
        backfill_symbols(store, prov, syms[15:], "1d", max_pages=3, sleep=_no_sleep,
                         run_id="RUN-R", completed_through=date(2026, 7, 21))
    man = dm.build_manifest(dbp, YAML, collection_params={}, collection_code_commit=None,
                            as_of="2026-07-21", collection_run_id="RUN-R")
    assert man["provenance_verification"]["provenance_status"] == "VERIFIED"
