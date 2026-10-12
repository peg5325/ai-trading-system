"""Long-history data foundation: parsers, manifest SHA refusal, seal guard, early-close rules, events yaml."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest
import yaml

from src.history.oos_guard import OOSLockError
from src.longhist import fetch as F
from src.longhist import normalize as N
from src.longhist import quality as Q
from src.longhist.loader import (
    SEAL_START,
    ManifestMismatch,
    assert_performance_window,
    load_daily,
    read_verified,
)

REPO = Path(__file__).resolve().parents[1]
EVENTS = REPO / "config" / "longhist_events_v1.yaml"

# 2024-01-02 / 01-03 / 01-04 09:30 ET timestamps (EST, UTC-5)
TS = [1704205800, 1704292200, 1704378600]
YAHOO_FIXTURE = {
    "chart": {"result": [{
        "meta": {"symbol": "TEST", "exchangeTimezoneName": "America/New_York", "currency": "USD",
                 "instrumentType": "ETF"},
        "timestamp": TS,
        "events": {"dividends": {str(TS[1]): {"amount": 0.5, "date": TS[1]}},
                   "splits": {str(TS[2]): {"date": TS[2], "numerator": 1.0, "denominator": 5.0,
                                           "splitRatio": "1:5"}}},
        "indicators": {"quote": [{"open": [10.0, None, 12.0], "high": [11.0, None, 13.0],
                                  "low": [9.5, None, 11.5], "close": [10.5, None, 12.5],
                                  "volume": [100, None, 300]}],
                       "adjclose": [{"adjclose": [10.0, None, 12.4]}]},
    }], "error": None}}


def test_parse_yahoo_chart_bars_events_nulls():
    p = N.parse_yahoo_chart(json.dumps(YAHOO_FIXTURE).encode())
    assert [b["date"] for b in p["bars"]] == ["2024-01-02", "2024-01-04"]
    assert p["null_rows"] == ["2024-01-03"]
    assert p["bars"][0] == {"date": "2024-01-02", "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5,
                            "adjclose": 10.0, "volume": 100}
    assert p["dividends"] == [{"date": "2024-01-03", "amount": 0.5}]
    assert p["splits"] == [{"date": "2024-01-04", "numerator": 1.0, "denominator": 5.0, "ratio": "1:5"}]


def test_parse_cboe_ohlc_and_close_only():
    rows = N.parse_cboe("DATE,OPEN,HIGH,LOW,CLOSE\n01/03/2024,13.1,14.2,12.9,14.0\n01/02/2024,12.5,13.5,12.4,13.2\n")
    assert rows[0] == {"date": "2024-01-02", "open": 12.5, "high": 13.5, "low": 12.4, "close": 13.2}
    assert [r["date"] for r in rows] == ["2024-01-02", "2024-01-03"]
    rows = N.parse_cboe("DATE,VIX9D\n01/02/2024,11.0\n")
    assert rows == [{"date": "2024-01-02", "open": None, "high": None, "low": None, "close": 11.0}]
    with pytest.raises(ValueError):
        N.parse_cboe("DATE,OPEN,HIGH,LOW,CLOSE\n01/02/2024,1,1,1,1\n01/02/2024,1,1,1,1\n")


def test_parse_dix_and_fred():
    rows = N.parse_dix("date,price,dix,gex\n2011-05-02,1361.2,0.378,1897313862.6\n")
    assert rows == [{"date": "2011-05-02", "price": 1361.2, "dix": 0.378, "gex": 1897313862.6}]
    raw = json.dumps({"count": 2, "release_dates": [{"release_id": 10, "date": "2024-02-13"},
                                                     {"release_id": 10, "date": "2024-01-11"}]}).encode()
    assert N.parse_fred_release_dates(raw) == ["2024-01-11", "2024-02-13"]
    bad = json.dumps({"count": 3, "release_dates": [{"date": "2024-01-11"}]}).encode()
    with pytest.raises(ValueError):
        N.parse_fred_release_dates(bad)
    assert N.multi_date_months(["2024-02-09", "2024-02-13", "2024-03-12", "1990-02-01", "1990-02-20"],
                               "1993-01-29") == [{"month": "2024-02", "dates": ["2024-02-09", "2024-02-13"]}]


def test_parse_fed_historical_headings():
    page = "".join(f"<h5>{h}</h5>" for h in [
        "January 31-February 1 Meeting - 2017", "April/May 30-1 Meeting - 2017", "March 2 (unscheduled) Meeting - 2017",
        "March 17-18 (cancelled) Meeting - 2017", "March 19 (notation vote) - 2017", "January 13 Conference Call - 2017",
        "July 5-6 Meeting - 2017", "Something else - 2017"])
    items, un = N.parse_fed_historical(page, 2017, "u")
    got = [(i["date"], i["type"]) for i in items]
    assert got == [("2017-02-01", "FOMC"), ("2017-05-01", "FOMC"), ("2017-03-02", "FOMC_UNSCHEDULED"),
                   ("2017-03-18", "EXCLUDED_CANCELLED"), ("2017-03-19", "EXCLUDED_NOTATION"),
                   ("2017-01-13", "FOMC_UNSCHEDULED"), ("2017-07-06", "FOMC")]
    assert [u["heading"] for u in un] == ["Something else - 2017"]


def test_parse_fed_calendars_panel():
    row = ('<div class="fomc-meeting__month col-xs-5"><strong>{m}</strong></div>\n'
           '<div class="fomc-meeting__date col-xs-4">{d}</div>')
    page = ("<h4><a id='1'>2024 FOMC Meetings</a></h4>" + row.format(m="Apr/May", d="30-1")
            + row.format(m="June", d="11-12*") + row.format(m="August", d="22 (notation vote)")
            + "<h4><a id='2'>2023 FOMC Meetings</a></h4>" + row.format(m="Jan/Feb", d="31-1"))
    items, un = N.parse_fed_calendars(page, "u")
    assert [(i["date"], i["type"]) for i in items] == [
        ("2024-05-01", "FOMC"), ("2024-06-12", "FOMC"), ("2024-08-22", "EXCLUDED_NOTATION"), ("2023-02-01", "FOMC")]
    assert un == []


def test_early_close_rules():
    assert N.thanksgiving(2024) == date(2024, 11, 28)
    assert N.thanksgiving(2023) == date(2023, 11, 23)
    sessions = {"2023-07-03", "2023-11-24", "2024-12-24", "2024-11-29", "2019-07-03", "2022-12-23"}
    got = {e["date"]: e for e in N.early_closes(sessions, 2019, 2024)}
    assert set(got) == {"2019-07-03", "2023-07-03", "2023-11-24", "2024-11-29", "2024-12-24"}
    assert all(e["confidence"] == "J" for e in got.values())
    # 2020-07-03 (Fri, July 4 Sat) and 2021-12-24 (Fri, Dec 25 Sat) are holidays: not sessions -> excluded
    assert N.early_closes({"2020-07-02", "2021-12-23"}, 2020, 2021) == []
    # weekend candidates are never early closes even if (wrongly) present in the session set
    assert N.early_closes({"2022-12-24", "2022-07-03"}, 2022, 2022) == []


def _mini_root(tmp_path: Path) -> tuple[Path, str]:
    rows = [{"date": d, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "adjclose": 1.0, "volume": 1}
            for d in ("2025-07-17", "2025-07-18", "2025-07-21", "2025-07-22")]
    F.write_csv(tmp_path, "norm/yahoo/SPY_daily.csv", rows, ["date", "open", "high", "low", "close", "adjclose", "volume"])
    man = F.Manifest(tmp_path)
    man.add("norm/yahoo/SPY_daily.csv", kind="norm", source="test", source_url=None, fetched_utc="x",
            rows=4, first_date="2025-07-17", last_date="2025-07-22")
    return tmp_path, man.save()


def test_loader_seal_guard(tmp_path):
    root, sha = _mini_root(tmp_path)
    assert SEAL_START == date(2025, 7, 21)
    for start, end in [("2025-07-01", "2025-07-21"), ("2025-07-01", None), ("2025-08-01", "2025-09-01"),
                       (None, "2026-08-01")]:
        with pytest.raises(OOSLockError):
            load_daily("SPY", start, end, "performance", root=root)
    df = load_daily("SPY", "2025-07-01", "2025-07-20", "performance", root=root, expected_manifest_sha256=sha)
    assert list(df["date"]) == ["2025-07-17", "2025-07-18"]
    df = load_daily("SPY", purpose="coverage", root=root)
    assert list(df["date"])[-1] == "2025-07-22"
    with pytest.raises(ValueError):
        load_daily("SPY", purpose="backtest", root=root)
    assert_performance_window(date(1993, 1, 1), date(2025, 7, 20))


def test_manifest_sha_refusal(tmp_path):
    root, sha = _mini_root(tmp_path)
    assert read_verified("norm/yahoo/SPY_daily.csv", root, sha)
    with pytest.raises(ManifestMismatch):
        read_verified("norm/yahoo/SPY_daily.csv", root, "0" * 64)
    with pytest.raises(ManifestMismatch):
        read_verified("norm/yahoo/QQQ_daily.csv", root)
    p = root / "norm/yahoo/SPY_daily.csv"
    p.write_text(p.read_text().replace("2025-07-18,1.0", "2025-07-18,2.0"))
    with pytest.raises(ManifestMismatch):
        load_daily("SPY", "2025-07-01", "2025-07-18", "performance", root=root)
    with pytest.raises(ManifestMismatch):
        load_daily("SPY", purpose="coverage", root=root)


def test_raw_is_immutable_and_url_redacted(tmp_path):
    F.save_raw(tmp_path, "x", "a.json", b"1")
    with pytest.raises(FileExistsError):
        F.save_raw(tmp_path, "x", "a.json", b"2")
    assert F.redact("https://h/p?release_id=1&api_key=SECRET&file_type=json") == \
        "https://h/p?release_id=1&api_key=<redacted>&file_type=json"


def test_quality_helpers():
    cal = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]
    c = Q.coverage(["2023-12-29", "2024-01-02", "2024-01-04", "2024-01-05", "2024-01-06", "2024-01-08"], cal)
    assert (c["missing"], c["extra"], c["before_calendar"]) == (1, 1, 1)
    e = {"d1": (10.0, 10.3), "d2": (10.0, 9.9), "d3": (10.0, 10.0), "d4": (10.0, 10.6)}
    r = {"d1": (5.0, 5.05), "d2": (5.0, 4.983333333333333), "d3": (5.0, 5.0), "d4": (5.0, 5.1)}
    fid = Q.fidelity(e, r, 3)
    assert fid["n"] == 4 and fid["corr"] == pytest.approx(1.0) and fid["mean_abs_diff"] == pytest.approx(0.0, abs=1e-12)
    rd = Q.ratio_deviation([(100.0, 100.0), (101.0, 100.0)])
    assert rd["max_abs_dev"] == pytest.approx(0.01) and rd["n_over_0_1pct"] == 1


ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def test_events_yaml_schema():
    text = EVENTS.read_text()
    assert re.search(r"api_key=(?!<redacted>)", text) is None
    ev = yaml.safe_load(text)
    assert ev["version"] == "longhist-events-v1"
    assert set(ev["types"]) == {"CPI", "NFP", "PCE", "FOMC", "FOMC_UNSCHEDULED", "EARLY_CLOSE"}
    names = {r["type"]: (r["release_id"], r["name"]) for r in ev["fred_release_check"]}
    assert names["CPI"] == (10, "Consumer Price Index")
    assert names["NFP"] == (50, "Employment Situation")
    assert names["PCE"] == (54, "Personal Income and Outlays")
    assert names["FOMC_PRESS_RELEASE"] == (101, "FOMC Press Release")
    for typ, body in ev["types"].items():
        assert ISO.match(str(body["coverage"]["start"])) and ISO.match(str(body["coverage"]["end"]))
        dates = body.get("dates") or [e["date"] for e in body["events"]]
        assert dates and all(ISO.match(d) for d in dates) and dates == sorted(dates), typ
        assert len(dates) == len(set(dates)), typ
        if "dates" in body:
            assert body["source_url"].startswith("https://api.stlouisfed.org/") and len(body["raw_sha256"]) == 64
    for e in ev["types"]["FOMC"]["events"] + ev["types"]["FOMC_UNSCHEDULED"]["events"]:
        assert e["source"].startswith("https://www.federalreserve.gov/monetarypolicy/")
    assert all(e["confidence"] == "J" and e["rule"] for e in ev["types"]["EARLY_CLOSE"]["events"])
    for u in ev["unresolved"]:
        assert u["type"] in {"FOMC", "CPI", "NFP", "PCE"} and u["reason"] and u["source"]
    assert ev["trading_day_calendar"]["sessions"] > 8000
