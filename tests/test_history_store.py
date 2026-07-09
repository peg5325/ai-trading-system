"""HistoryStore 병합 로직 검증 — 멱등 upsert, conflict 정책/로그, ingest_log."""
import sqlite3

from src.history import HistoryStore


def _make_session_db(path, rows):
    """최소 candle 테이블을 가진 가짜 session DB 생성."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE candle (symbol TEXT, interval TEXT, ts_utc TEXT, ts_exchange TEXT, "
        "session TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, "
        "currency TEXT, adjusted INTEGER, PRIMARY KEY(symbol,interval,ts_utc))"
    )
    con.executemany(
        "INSERT INTO candle VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    con.commit()
    con.close()


def _row(sym="005930", ts="2026-07-07T00:00:00+00:00", tx="2026-07-07T09:00:00+09:00",
         close=70000.0, cur="KRW", adj=1, session="REGULAR"):
    return (sym, "1m", ts, tx, session, close, close + 10, close - 10, close, 100.0, cur, adj)


def test_schema_and_basic_import(tmp_path):
    src = str(tmp_path / "market_krx_s001.sqlite")
    _make_session_db(src, [_row(ts="2026-07-07T00:00:00+00:00"),
                           _row(ts="2026-07-07T00:01:00+00:00", close=70100.0)])
    with HistoryStore(str(tmp_path / "market_history.sqlite")) as store:
        # 스키마 테이블 존재
        sql = store.schema_sql()
        assert "candle_history" in sql and "ingest_log" in sql
        assert "import_conflict" in sql and "backfill_cursor" in sql
        s = store.import_session_db(src)
        assert s.rows_seen == 2 and s.inserted == 2 and s.duplicate == 0 and s.conflict == 0
        assert store.total_rows() == 2
        cov = store.symbol_coverage()
        assert cov[0]["symbol"] == "005930" and cov[0]["market"] == "KR" and cov[0]["n"] == 2


def test_idempotent_reimport(tmp_path):
    src = str(tmp_path / "market_us_s.sqlite")
    _make_session_db(src, [_row(sym="SOXL", cur="USD", ts="2026-07-07T13:30:00+00:00",
                                tx="2026-07-07T09:30:00-04:00", close=170.0)])
    dbp = str(tmp_path / "market_history.sqlite")
    with HistoryStore(dbp) as store:
        s1 = store.import_session_db(src)
        assert s1.inserted == 1
        # 재실행: 신규 insert 0, 전부 duplicate, 총 행수 불변
        s2 = store.import_session_db(src)
        assert s2.inserted == 0 and s2.duplicate == 1 and s2.conflict == 0
        assert store.total_rows() == 1
        cov = store.symbol_coverage()
        assert cov[0]["market"] == "US"


def test_conflict_logged_and_kept_by_priority(tmp_path):
    ts = "2026-07-07T00:00:00+00:00"
    src_a = str(tmp_path / "market_a.sqlite")
    src_b = str(tmp_path / "market_b.sqlite")
    _make_session_db(src_a, [_row(ts=ts, close=70000.0)])
    _make_session_db(src_b, [_row(ts=ts, close=71000.0)])  # 동일 PK, 다른 close → conflict
    dbp = str(tmp_path / "market_history.sqlite")
    with HistoryStore(dbp) as store:
        store.import_session_db(src_a)
        s = store.import_session_db(src_b)
        assert s.conflict == 1 and s.updated == 0  # 둘 다 import(동률) → 기존 유지
        assert store.total_rows() == 1
        con = sqlite3.connect(dbp)
        # 값은 기존(A=70000) 유지
        close = con.execute("SELECT close FROM candle_history").fetchone()[0]
        assert abs(close - 70000.0) < 1e-6
        # conflict 로그 기록됨
        nconf = con.execute("SELECT COUNT(*) FROM import_conflict WHERE field='close'").fetchone()[0]
        assert nconf == 1
        res = con.execute("SELECT resolution FROM import_conflict").fetchone()[0]
        assert res == "kept_existing"
        # ingest_log 두 건
        nlog = con.execute("SELECT COUNT(*) FROM ingest_log").fetchone()[0]
        assert nlog == 2
        con.close()


def test_backfill_source_wins_conflict(tmp_path):
    """toss_backfill(우선순위 높음)이 import 값을 덮어쓴다."""
    ts = "2026-07-07T00:00:00+00:00"
    src = str(tmp_path / "market_a.sqlite")
    _make_session_db(src, [_row(ts=ts, close=70000.0)])
    dbp = str(tmp_path / "market_history.sqlite")
    with HistoryStore(dbp) as store:
        store.import_session_db(src)
        # backfill 우선순위 소스로 직접 upsert(다른 close)
        r = dict(symbol="005930", market="KR", interval="1m", ts_utc=ts,
                 ts_exchange="2026-07-07T09:00:00+09:00", session="REGULAR",
                 open=71000.0, high=71010.0, low=70990.0, close=71000.0, volume=100.0,
                 currency="KRW", adjusted=1, source="toss_backfill")
        from src.history.store import ImportStats
        st = ImportStats(source="toss_backfill")
        store._upsert_row("run-x", r, st)
        store._con.commit()  # _upsert_row 는 커밋하지 않음(import_session_db 가 커밋) → 별도 커넥션 조회 전 커밋
        assert st.conflict == 1 and st.updated == 1
        con = sqlite3.connect(dbp)
        close = con.execute("SELECT close, source FROM candle_history").fetchone()
        assert abs(close[0] - 71000.0) < 1e-6 and close[1] == "toss_backfill"
        con.close()
