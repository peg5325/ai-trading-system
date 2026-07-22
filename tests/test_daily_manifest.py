"""P0-30 일봉 manifest 도구 검증 — read-only·결정론·정렬 불변·기준완화 금지.

포함: 기존 quality_report 가 1분봉 의미에 결합돼 일봉 검증에 부적합함을 증명하는 테스트.
"""
from datetime import date, timedelta

import pytest

from src.history import HistoryStore, quality_report
from src.history.daily_manifest import (
    build_manifest,
    content_digest,
    daily_verdict,
    invariant_snapshot,
    read_p0_universe,
    split_continuity_check,
    symbol_fingerprint,
)


# ---------------------------------------------------------------------------
# helpers — tmp DB 에 합성 1d 봉/ingest_log/cursor 삽입
# ---------------------------------------------------------------------------
def _con(store: HistoryStore):
    return store._con


def _insert_1d(store, symbol, start_date, n_days, *, market="US", currency="USD",
               source="toss_backfill", adjusted=1, close_fn=None, vol_fn=None,
               local_hm="09:30:00"):
    con = _con(store)
    for i in range(n_days):
        d = start_date + timedelta(days=i)
        ds = d.isoformat()
        close = close_fn(i) if close_fn else 100.0 + i
        vol = vol_fn(i) if vol_fn else 1000.0 + i
        con.execute(
            "INSERT OR REPLACE INTO candle_history (symbol,market,interval,ts_utc,ts_exchange,"
            "session,open,high,low,close,volume,currency,adjusted,source,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (symbol, market, "1d", f"{ds}T13:30:00+00:00", f"{ds}T{local_hm}",
             "REGULAR", close, close + 1, close - 1, close, vol, currency, adjusted, source,
             "t", "t"),
        )
    con.commit()


def _ingest(store, symbol, *, inserted, note, run_id="r1", interval="1d"):
    con = _con(store)
    con.execute(
        "INSERT INTO ingest_log (run_id,ts,source,symbol,interval,rows_seen,rows_inserted,"
        "rows_updated,rows_duplicate,rows_conflict,earliest,latest,note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "2026-07-22T00:00:00", "toss_backfill", symbol, interval,
         inserted, inserted, 0, 0, 0, None, None, note),
    )
    con.commit()


def _cursor(store, symbol, *, exhausted, interval="1d"):
    _con(store).execute(
        "INSERT OR REPLACE INTO backfill_cursor (symbol,interval,oldest_ts_utc,next_before,"
        "exhausted,updated_at) VALUES (?,?,?,?,?,?)",
        (symbol, interval, None, None, 1 if exhausted else 0, "t"),
    )
    _con(store).commit()


# ---------------------------------------------------------------------------
# 1) P0 YAML 결정론적 읽기 + 계약 검증 (실제 정본 파일)
# ---------------------------------------------------------------------------
def test_read_real_p0_universe_valid():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    uni = read_p0_universe(os.path.join(root, "config", "universe_p0_us30.yaml"))
    assert uni["valid"] is True and uni["errors"] == []
    assert uni["actual_count"] == 30 and uni["unique_count"] == 30
    assert uni["market"] == "US" and uni["currency"] == "USD"
    assert uni["min_years"] == 5
    # 순서 보존(결정론) — YAML 기재 순서
    assert uni["symbols"][0] == "SPY" and uni["symbols"][-1] == "SOXS"


def test_read_p0_universe_detects_violations(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "version: 1\ncount: 30\nmarket: KR\ncurrency: KRW\n"
        "requirements:\n  swing_daily:\n    min_years: 5\n"
        "symbols:\n  - {symbol: AAA}\n  - {symbol: AAA}\n",
        encoding="utf-8",
    )
    uni = read_p0_universe(str(bad))
    assert uni["valid"] is False
    assert any("count" in e for e in uni["errors"])
    assert any("duplicate" in e for e in uni["errors"])
    assert any("market" in e for e in uni["errors"])
    assert any("currency" in e for e in uni["errors"])


# ---------------------------------------------------------------------------
# 2) fingerprint 결정론 + 삽입순서 불변
# ---------------------------------------------------------------------------
def test_symbol_fingerprint_deterministic_and_insertorder_invariant(tmp_path):
    with HistoryStore(str(tmp_path / "a.sqlite")) as s1:
        _insert_1d(s1, "SPY", date(2020, 1, 1), 5)
        fp_a = symbol_fingerprint(_con(s1), "SPY")
        fp_a2 = symbol_fingerprint(_con(s1), "SPY")
    # 다른 DB 에 역순 삽입 → 정렬 후 동일 내용이면 같은 해시
    with HistoryStore(str(tmp_path / "b.sqlite")) as s2:
        con = _con(s2)
        for i in reversed(range(5)):
            d = (date(2020, 1, 1) + timedelta(days=i)).isoformat()
            con.execute(
                "INSERT INTO candle_history (symbol,market,interval,ts_utc,ts_exchange,session,"
                "open,high,low,close,volume,currency,adjusted,source,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("SPY", "US", "1d", f"{d}T13:30:00+00:00", f"{d}T09:30:00", "REGULAR",
                 100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i, 1000.0 + i, "USD", 1,
                 "toss_backfill", "t", "t"),
            )
        con.commit()
        fp_b = symbol_fingerprint(con, "SPY")
    assert fp_a["content_sha256"] == fp_a2["content_sha256"] == fp_b["content_sha256"]
    assert fp_a["row_count"] == 5 and fp_a["trading_days"] == 5


# ---------------------------------------------------------------------------
# 3) 일봉 판정 — 기준 완화 금지, listing 구분
# ---------------------------------------------------------------------------
def test_daily_verdict_thresholds():
    ok = daily_verdict(span_yrs=10.3, min_years=5, stopped_reason="max_pages", row_count=2600)
    assert ok["status"] == "OK" and ok["meets_min_years"] is True
    assert ok["depth_source"] == "page_budget_capped" and ok["listing_limited"] is False

    listing = daily_verdict(span_yrs=5.8, min_years=5, stopped_reason="nextbefore_null",
                            row_count=1458)
    assert listing["status"] == "OK" and listing["listing_limited"] is True
    assert listing["depth_source"] == "listing_full"

    # span < min_years → 절대 완화하지 않고 INCONCLUSIVE_DATA
    short = daily_verdict(span_yrs=3.0, min_years=5, stopped_reason="nextbefore_null",
                          row_count=700)
    assert short["status"] == "INCONCLUSIVE_DATA" and short["meets_min_years"] is False

    empty = daily_verdict(span_yrs=None, min_years=5, stopped_reason=None, row_count=0)
    assert empty["status"] == "INCONCLUSIVE_DATA"


def test_daily_verdict_nonfloor_stops_are_indeterminate_not_listing_full():
    """empty_budget_exhausted/no_progress 는 '상장이력 전체'로 단정하지 않는다(over-claim 방지)."""
    for reason in ("empty_budget_exhausted", "no_progress", "error"):
        v = daily_verdict(span_yrs=10.3, min_years=5, stopped_reason=reason, row_count=2600)
        assert v["depth_source"] == "indeterminate", reason
        assert v["listing_limited"] is False, reason
        assert v["status"] == "OK"  # 판정은 span 으로만(라벨과 무관)
    # empty_page 는 원천 바닥으로 인정
    v2 = daily_verdict(span_yrs=6.0, min_years=5, stopped_reason="empty_page", row_count=1500)
    assert v2["depth_source"] == "listing_full" and v2["listing_limited"] is True


# ---------------------------------------------------------------------------
# 4) content_digest 결정론 + 순서 불변 + 변경 민감
# ---------------------------------------------------------------------------
def test_content_digest_order_invariant_and_sensitive():
    e1 = {"symbol": "AAA", "market": "US", "interval": "1d", "earliest": "x", "latest": "y",
          "row_count": 3, "content_sha256": "h1", "sources": ["toss_backfill"],
          "adjusted": [1], "trading_days": 3}
    e2 = dict(e1, symbol="BBB", content_sha256="h2")
    d_order1 = content_digest([e1, e2])
    d_order2 = content_digest([e2, e1])
    assert d_order1 == d_order2  # 입력 순서 불변(정렬)
    # 데이터 필드 하나만 바꿔도 달라짐
    e2b = dict(e2, row_count=4)
    assert content_digest([e1, e2b]) != d_order1


def test_build_manifest_guards_missing_min_years(tmp_path):
    """min_years 누락(계약 위반) YAML → 애매한 TypeError 대신 명시적 ValueError."""
    y = tmp_path / "nomy.yaml"
    y.write_text(
        "version: 1\ncount: 1\nmarket: US\ncurrency: USD\n"
        "symbols:\n  - {symbol: SPY}\n",  # requirements 없음 → min_years None
        encoding="utf-8",
    )
    dbp = str(tmp_path / "m.sqlite")
    with HistoryStore(dbp) as store:
        _insert_1d(store, "SPY", date(2016, 3, 18), 10)
    with pytest.raises(ValueError, match="min_years"):
        build_manifest(dbp, str(y), collection_params={}, collection_code_commit=None,
                       generated_at_utc="2026-07-22T00:00:00Z")


# ---------------------------------------------------------------------------
# 5) manifest 결정론 + secrets/raw 미포함
# ---------------------------------------------------------------------------
def test_build_manifest_deterministic_and_clean(tmp_path):
    yaml_path = tmp_path / "u.yaml"
    yaml_path.write_text(
        "version: 1\nas_of: '2026-07-21'\ncount: 2\nmarket: US\ncurrency: USD\n"
        "requirements:\n  swing_daily:\n    min_years: 5\n"
        "symbols:\n  - {symbol: SPY}\n  - {symbol: QQQ}\n",
        encoding="utf-8",
    )
    dbp = str(tmp_path / "secret_dir" / "market_history.sqlite")
    import os
    os.makedirs(os.path.dirname(dbp))
    with HistoryStore(dbp) as store:
        _insert_1d(store, "SPY", date(2016, 3, 18), 2600)
        _insert_1d(store, "QQQ", date(2016, 3, 18), 2600)
        _ingest(store, "SPY", inserted=2600, note="max_pages")
        _ingest(store, "QQQ", inserted=2600, note="max_pages")

    params = {"max_pages": 13, "page_size": 200, "spacing": 0.5, "resume": True,
              "gap_skip_minutes": 0, "interval": "1d"}
    m1 = build_manifest(dbp, str(yaml_path), collection_params=params,
                        collection_code_commit="8a58fdc", generated_at_utc="2026-07-22T00:00:00Z")
    m2 = build_manifest(dbp, str(yaml_path), collection_params=params,
                        collection_code_commit="8a58fdc", generated_at_utc="2999-01-01T00:00:00Z")
    # 생성 시각이 달라도 content hash 동일(결정론)
    assert m1["manifest_content_sha256"] == m2["manifest_content_sha256"]
    assert m1["summary"]["symbols_total"] == 2
    assert all(e["verdict"]["status"] == "OK" for e in m1["symbols"])
    # secrets/절대경로 미포함: db_basename 만, 전체 절대경로 문자열 없음
    blob = __import__("json").dumps(m1, ensure_ascii=False)
    assert "secret_dir" not in blob
    assert m1["collection"]["db_basename"] == "market_history.sqlite"
    # 원시 캔들 배열 미포함 — 심볼 엔트리에 open/high/low/close 리스트가 없다
    for e in m1["symbols"]:
        assert "candles" not in e and "open" not in e
        assert e["content_sha256"] and e["row_count"] == 2600


# ---------------------------------------------------------------------------
# 6) R2 split 연속성 — 조정/미조정 구분, 애매하면 inconclusive
# ---------------------------------------------------------------------------
def test_split_continuity_adjusted_vs_unadjusted(tmp_path):
    split = "2024-06-10"
    # 조정된 계열: 경계에서 close 연속(불연속 없음) → adjusted
    with HistoryStore(str(tmp_path / "adj.sqlite")) as s:
        _insert_1d(s, "NVDA", date(2024, 6, 3), 10, close_fn=lambda i: 120.0,
                   vol_fn=lambda i: 1000.0)
        r = split_continuity_check(_con(s), "NVDA", split, ratio=10)
    assert r["price_adjusted"] == "adjusted"

    # 미조정 계열: split 전 가격이 10배(1200), 후 120 → close_ratio≈10 → unadjusted
    with HistoryStore(str(tmp_path / "unadj.sqlite")) as s:
        _insert_1d(s, "NVDA", date(2024, 6, 3), 5, close_fn=lambda i: 1200.0)
        _insert_1d(s, "NVDA", date(2024, 6, 10), 5, close_fn=lambda i: 120.0)
        r = split_continuity_check(_con(s), "NVDA", split, ratio=10)
    assert r["price_adjusted"] == "unadjusted"
    assert r["close_ratio"] == pytest.approx(10.0, rel=0.05)


# ---------------------------------------------------------------------------
# 7) invariant snapshot — 무변경 증명 해시
# ---------------------------------------------------------------------------
def test_invariant_snapshot_changes_only_on_content_change(tmp_path):
    dbp = str(tmp_path / "inv.sqlite")
    with HistoryStore(dbp) as store:
        _insert_1d(store, "AMD", date(2020, 1, 1), 3, market="US", currency="USD")
        snap1 = invariant_snapshot(_con(store), "1d")
        # 다른 interval(1m) 추가는 1d 스냅샷을 바꾸지 않는다
        _insert_1d(store, "AMD", date(2020, 1, 1), 3)  # 동일 1d → idempotent(REPLACE)
        snap2 = invariant_snapshot(_con(store), "1d")
        assert snap1["combined_sha256"] == snap2["combined_sha256"]
        # 실제 1d 내용 변경 시 해시 달라짐
        _insert_1d(store, "AMD", date(2020, 1, 4), 1)
        snap3 = invariant_snapshot(_con(store), "1d")
    assert snap3["combined_sha256"] != snap1["combined_sha256"]
    assert snap1["total_rows"] == 3 and snap3["total_rows"] == 4


# ---------------------------------------------------------------------------
# 8) ★증명: quality_report 는 1분봉 의미에 결합 → 일봉 검증에 부적합
# ---------------------------------------------------------------------------
def test_quality_report_is_1m_coupled_and_unfit_for_daily(tmp_path):
    """일봉(하루 1봉)을 quality_report(interval='1d')에 넣으면 REG_WINDOW(390분) 기준
    coverage_pct 가 0.26%(=1/390) 로 나오고 full_session_days=0, resample_5m=False 가 된다.

    즉 정상적인 10년치 일봉인데도 '완전 세션 0일·리샘플 불가' 로 오판한다 → 일봉 부적합.
    이 결함이 daily 전용 검증(daily_manifest)을 추가한 근거다.
    """
    dbp = str(tmp_path / "d.sqlite")
    with HistoryStore(dbp) as store:
        # 정규장 시각(09:30)으로 5거래일치 일봉 — 완전한 정상 일봉
        _insert_1d(store, "SPY", date(2026, 7, 6), 5, local_hm="09:30:00")
    rep = quality_report(dbp, interval="1d", today=date(2026, 7, 10))
    s = rep["symbols"][0]
    # 하루 1봉 → 정규창 390분 대비 coverage 0.26% (완전 오판)
    assert s["per_day"][0]["coverage_pct"] < 1.0
    assert s["per_day"][0]["missing_minutes"] == 389  # 390 - 1
    # 멀쩡한 일봉인데 '완전 세션 0일'·'리샘플 불가' 로 판정
    assert s["full_session_days"] == 0
    assert s["resample_5m"] is False
    # 대조: daily_manifest 는 같은 데이터를 정상(범위·행수)으로 요약
    import sqlite3
    ro = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    fp = symbol_fingerprint(ro, "SPY")
    ro.close()
    assert fp["row_count"] == 5 and fp["trading_days"] == 5
