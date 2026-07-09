"""quality_report 검증 — full-session/ missing minute/ gap/ 5m resample 판정."""
from datetime import date, datetime, timedelta, timezone

from src.history import HistoryStore, quality_report


def _seed(store: HistoryStore, sym, market, currency, day_local_date, start_hm, minutes, tz_off):
    """day 의 정규장 start_hm 부터 minutes 개의 연속 1분봉을 심는다."""
    run = "seed"
    base_local = datetime(day_local_date.year, day_local_date.month, day_local_date.day,
                          start_hm[0], start_hm[1], tzinfo=timezone(timedelta(hours=tz_off)))
    for i in range(minutes):
        loc = base_local + timedelta(minutes=i)
        utc = loc.astimezone(timezone.utc)
        r = dict(symbol=sym, market=market, interval="1m",
                 ts_utc=utc.isoformat(), ts_exchange=loc.isoformat(), session="REGULAR",
                 open=100.0, high=101.0, low=99.0, close=100.0 + i * 0.01, volume=10.0,
                 currency=currency, adjusted=1, source="seed")
        from src.history.store import ImportStats
        store._upsert_row(run, r, ImportStats(source="seed"))
    store._con.commit()


def test_full_session_and_resample(tmp_path):
    dbp = str(tmp_path / "market_history.sqlite")
    with HistoryStore(dbp) as store:
        # KR 종목, 09:00 부터 정규장 390분 전체(=full session), 연속(gap 0)
        _seed(store, "005930", "KR", "KRW", date(2026, 7, 7), (9, 0), 390, tz_off=9)
        rep = quality_report(dbp, today=date(2026, 7, 7))
        s = rep["symbols"][0]
        assert s["symbol"] == "005930" and s["market"] == "KR"
        assert s["total_bars"] == 390
        assert s["full_session_days"] == 1
        assert s["total_gaps"] == 0
        assert s["total_missing_intraday_min"] == 0
        assert s["resample_5m"] is True
        assert rep["markets"]["KR"]["full_session_days"] == 1


def test_partial_day_and_gap(tmp_path):
    dbp = str(tmp_path / "market_history.sqlite")
    with HistoryStore(dbp) as store:
        # 정규장 절반(195분)만 → full 아님, coverage 50%
        _seed(store, "SOXL", "US", "USD", date(2026, 7, 7), (9, 30), 195, tz_off=-4)
        rep = quality_report(dbp, today=date(2026, 7, 7), min_coverage_pct=90.0)
        s = rep["symbols"][0]
        assert s["market"] == "US"
        assert s["full_session_days"] == 0
        assert s["per_day"][0]["coverage_pct"] == 50.0
        assert s["total_missing_intraday_min"] == 195  # 390 - 195
        assert s["resample_5m"] is False  # full-session day 0


def test_missing_days_counted(tmp_path):
    dbp = str(tmp_path / "market_history.sqlite")
    with HistoryStore(dbp) as store:
        _seed(store, "000660", "KR", "KRW", date(2026, 7, 7), (9, 0), 390, tz_off=9)
        # 07-07 하루만 존재. as_of=07-10 이면 최근 3영업일(08,09,10) 중 2일 결측(08,09,10에 07은 미포함)
        rep = quality_report(dbp, today=date(2026, 7, 10), calendar_days=3)
        s = rep["symbols"][0]
        # 기대 영업일: 07-07,08,09,10 중 07-07만 present → 08,09,10 결측(3)
        assert s["missing_days_count"] == 3
        assert "2026-07-08" in s["missing_days"]
