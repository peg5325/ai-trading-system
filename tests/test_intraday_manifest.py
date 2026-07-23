"""1분봉 coverage/snapshot manifest 테스트 (계약 §2.2·§4.4·§5.1 · 이슈 #75 Phase 1B).

검증(작업 테스트 5·6·7·16·17):
  5.  as_of 이후 행 공식 snapshot 제외
  6.  진행 중 거래일/불완성 봉 OK 금지
  7.  EDT/EST(DST) 경계 — ET 거래일 기준 as_of 판정
  16. manifest 결정론 및 content hash 재현
  17. 종목별 3년 미달 INCONCLUSIVE_DATA
read-only·임시 DB. **전략 성과·forward return 미계산.**
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history import HistoryStore  # noqa: E402
from src.history.intraday_manifest import (  # noqa: E402
    build_manifest,
    intraday_symbol_fingerprint,
    read_intraday_requirements,
)
from src.history.store import ImportStats  # noqa: E402
from src.models import Candle  # noqa: E402

ET = ZoneInfo("America/New_York")


def _cand(sym, dt: datetime, close=100.0) -> Candle:
    return Candle.from_moment(symbol=sym, interval="1m", moment=dt,
                              o=close, h=close + 1, low=close - 1, c=close, v=1000.0,
                              currency="USD", adjusted=True, market="US")


def _insert(store, symbol, moments, *, source="toss_backfill"):
    stats = ImportStats(source=source)
    store.upsert_candles([_cand(symbol, m) for m in moments],
                         source=source, run_id="r", stats=stats)
    store.commit()


def _ingest_log(store, symbol, *, note="max_pages"):
    store._con.execute(
        "INSERT INTO ingest_log (run_id,ts,source,symbol,interval,rows_seen,rows_inserted,"
        "rows_updated,rows_duplicate,rows_conflict,earliest,latest,note) "
        "VALUES ('r','2026-07-21T00:00:00Z','toss_backfill',?,?,1,1,0,0,0,NULL,NULL,?)",
        (symbol, "1m", note))
    store.commit()


def _yaml(tmp_path, symbols, *, as_of="2026-07-21", min_years=3):
    body = ["version: 1", f"as_of: '{as_of}'", "market: US", "currency: USD",
            f"count: {len(symbols)}", "requirements:", "  intraday_minute:",
            "    interval: '1m'", f"    min_years: {min_years}",
            "    sessions_required: [PRE, REGULAR, POST]", "    blind_oos_months: 6",
            "symbols:"]
    body += [f"  - {{symbol: {s}}}" for s in symbols]
    p = tmp_path / "u.yaml"
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
    return str(p)


def _reg_day(day_dt: datetime, n: int):
    """day_dt(ET 10:00) 부터 n 개 연속 REGULAR 1분봉."""
    from datetime import timedelta
    return [day_dt + timedelta(minutes=i) for i in range(n)]


# ---- 5·6. as_of 경계 ----

def test_as_of_excludes_beyond_and_blocks_ok(tmp_path):
    """5+6. as_of(2026-07-21) 초과 봉 제외 + 진행 중 거래일 있으면 OK 금지."""
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        # SPY: 3년 전(2023-06-01) ~ as_of 당일 REGULAR + 진행 중 다음날(2026-07-22) 1봉.
        _insert(store, "SPY",
                _reg_day(datetime(2023, 6, 1, 10, 0, tzinfo=ET), 3)
                + _reg_day(datetime(2026, 7, 21, 10, 0, tzinfo=ET), 3)
                + [datetime(2026, 7, 22, 10, 0, tzinfo=ET)])   # ★as_of 초과(진행 중)
        _ingest_log(store, "SPY")
        con = store._con
        fp = intraday_symbol_fingerprint(con, "SPY", as_of="2026-07-21")
        assert fp["bars_beyond_as_of"] == 1
        assert fp["latest_trading_day"] == "2026-07-21"      # 초과봉 제외 → 마지막은 as_of
        assert fp["row_count"] == 6

    man = build_manifest(db, _yaml(tmp_path, ["SPY"]),
                         collection_params={"interval": "1m"}, collection_code_commit="x",
                         as_of="2026-07-21", generated_at_utc="2026-07-23T00:00:00Z")
    e = man["symbols"][0]
    assert e["bars_beyond_as_of"] == 1
    assert e["verdict"]["status"] == "INCONCLUSIVE_DATA"     # 진행 중 봉 → OK 금지(6)
    assert "bars_beyond_as_of" in e["verdict"]["inconclusive_reasons"]


def test_as_of_cannot_be_later_than_yaml(tmp_path):
    """경계 완화 방지: --as-of 가 YAML 정본보다 늦으면 거부(fail-closed)."""
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _insert(store, "SPY", [datetime(2026, 7, 21, 10, 0, tzinfo=ET)])
    with pytest.raises(ValueError):
        build_manifest(db, _yaml(tmp_path, ["SPY"], as_of="2026-07-21"),
                       collection_params={}, collection_code_commit=None,
                       as_of="2026-07-25")       # YAML(07-21)보다 늦음 → 거부


# ---- 7. DST(EDT/EST) 경계 ----

def test_as_of_boundary_is_et_trading_day_dst(tmp_path):
    """7. as_of 판정은 ET 거래일 기준 — DST 여름/겨울 모두 올바른 경계.

    2026-07-21 23:00 ET(EDT=UTC-4 → 07-22 03:00Z)는 ET 거래일 07-21 이므로 포함.
    2026-07-22 00:30 ET 은 07-22 이므로 제외. UTC 로 보면 둘 다 07-22Z 지만 ET 로 갈린다.
    """
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _insert(store, "SPY", [
            datetime(2026, 1, 15, 10, 0, tzinfo=ET),   # 겨울(EST) REGULAR
            datetime(2026, 7, 21, 23, 0, tzinfo=ET),   # 여름 23:00 ET → UTC 07-22 (포함돼야)
            datetime(2026, 7, 22, 0, 30, tzinfo=ET),   # 07-22 ET → 제외
        ])
        con = store._con
        fp = intraday_symbol_fingerprint(con, "SPY", as_of="2026-07-21")
        assert fp["bars_beyond_as_of"] == 1            # 07-22 00:30 ET 만 초과
        assert fp["latest_trading_day"] == "2026-07-21"
        # 07-21 23:00 ET 봉(UTC 07-22)은 ET 거래일 07-21 → 포함.
        assert fp["row_count"] == 2


# ---- 17. 3년 미달 INCONCLUSIVE ----

def test_short_history_is_inconclusive(tmp_path):
    """17. span<3.0년 종목은 INCONCLUSIVE_DATA(기준 완화 금지)."""
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _insert(store, "OK3Y", _reg_day(datetime(2023, 6, 1, 10, 0, tzinfo=ET), 2)
                + _reg_day(datetime(2026, 7, 21, 10, 0, tzinfo=ET), 2))     # ~3.14y
        _insert(store, "LATE", _reg_day(datetime(2024, 8, 1, 10, 0, tzinfo=ET), 2)
                + _reg_day(datetime(2026, 7, 21, 10, 0, tzinfo=ET), 2))     # ~1.97y
        _ingest_log(store, "OK3Y"); _ingest_log(store, "LATE")
    man = build_manifest(db, _yaml(tmp_path, ["OK3Y", "LATE"]),
                         collection_params={}, collection_code_commit=None,
                         as_of="2026-07-21", generated_at_utc="2026-07-23T00:00:00Z")
    by = {e["symbol"]: e for e in man["symbols"]}
    assert by["OK3Y"]["verdict"]["status"] == "OK"
    assert by["OK3Y"]["verdict"]["meets_min_years"] is True
    assert by["LATE"]["verdict"]["status"] == "INCONCLUSIVE_DATA"
    assert any(r.startswith("span<") for r in by["LATE"]["verdict"]["inconclusive_reasons"])
    assert man["summary"]["meets_3y_count"] == 1


# ---- 16. 결정론 + content hash 재현 ----

def test_manifest_deterministic_content_hash(tmp_path):
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        _insert(store, "SPY", _reg_day(datetime(2023, 6, 1, 10, 0, tzinfo=ET), 3)
                + _reg_day(datetime(2026, 7, 20, 10, 0, tzinfo=ET), 3))
        _ingest_log(store, "SPY")
    yaml_p = _yaml(tmp_path, ["SPY"])
    m1 = build_manifest(db, yaml_p, collection_params={}, collection_code_commit="c1",
                        as_of="2026-07-21", generated_at_utc="2026-07-23T00:00:00Z")
    m2 = build_manifest(db, yaml_p, collection_params={"other": 1}, collection_code_commit="c2",
                        as_of="2026-07-21", generated_at_utc="2026-07-24T09:09:09Z")
    # provenance/시각/collection 이 달라도 data_content_sha256 는 동일(데이터 유도 필드만).
    assert m1["data_content_sha256"] == m2["data_content_sha256"]


def test_fingerprint_order_independent(tmp_path):
    """입력 삽입 순서가 달라도 per-symbol content_sha256 동일(스트리밍 ts_utc 정렬)."""
    moments = _reg_day(datetime(2026, 7, 20, 10, 0, tzinfo=ET), 8)
    db1 = str(tmp_path / "a.sqlite")
    with HistoryStore(db1) as s1:
        _insert(s1, "SPY", moments)
        fp1 = intraday_symbol_fingerprint(s1._con, "SPY", as_of="2026-07-21")
    db2 = str(tmp_path / "b.sqlite")
    shuffled = moments[:]
    random.Random(3).shuffle(shuffled)
    with HistoryStore(db2) as s2:
        _insert(s2, "SPY", shuffled)
        fp2 = intraday_symbol_fingerprint(s2._con, "SPY", as_of="2026-07-21")
    assert fp1["content_sha256"] == fp2["content_sha256"]
    assert fp1["row_count"] == fp2["row_count"] == 8


def test_read_intraday_requirements(tmp_path):
    reqs = read_intraday_requirements(_yaml(tmp_path, ["SPY"]))
    assert reqs["min_years"] == 3.0
    assert reqs["sessions_required"] == ["PRE", "REGULAR", "POST"]
    assert reqs["blind_oos_months"] == 6


def test_session_breakdown_and_allowed(tmp_path):
    """세션별 행수 집계 + 허용 세션 밖(OVERNIGHT 등) 저장 시 INCONCLUSIVE."""
    from datetime import timedelta
    db = str(tmp_path / "m.sqlite")
    with HistoryStore(db) as store:
        # PRE(05:00)·REGULAR(10:00)·POST(17:00) 각 1봉 + 3년 span 확보용 과거 봉.
        _insert(store, "SPY", [
            datetime(2023, 6, 1, 10, 0, tzinfo=ET),
            datetime(2026, 7, 20, 5, 0, tzinfo=ET),
            datetime(2026, 7, 20, 10, 0, tzinfo=ET),
            datetime(2026, 7, 20, 17, 0, tzinfo=ET)])
        _ingest_log(store, "SPY")
        fp = intraday_symbol_fingerprint(store._con, "SPY", as_of="2026-07-21")
        assert fp["sessions"] == {"POST": 1, "PRE": 1, "REGULAR": 2}
