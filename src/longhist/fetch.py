"""Network collection for the long-history data set: immutable raw files + normalized CSVs + manifest.

Raw responses are written once (`open(..., "xb")`) under raw/<source>/ with a UTC fetch stamp in the
file name. Normalized CSVs under norm/ are rebuilt from those raw bytes. `manifest.json` lists every
raw and normalized file with SHA-256, source URL (credentials redacted), fetch time UTC, row count and
first/last date. No returns or signal statistics are computed here.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from src.longhist import normalize as N

UA = {"User-Agent": "Mozilla/5.0 (longhist research data)"}
YAHOO_SYMBOLS = ("SPY", "QQQ", "^SOX", "SMH", "TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXS")
CBOE = {
    "VIX": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "VIX9D": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv",
    "VIX3M": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv",
}
DIX_URL = "https://squeezemetrics.com/monitor/static/DIX.csv"
FRED_RELEASES = {"CPI": 10, "NFP": 50, "PCE": 54, "FOMC_PRESS_RELEASE": 101}
FED_HIST = "https://www.federalreserve.gov/monetarypolicy/fomchistorical{year}.htm"
FED_CAL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FED_FIRST_YEAR = 1993
FRED_START = "1993-01-01"  # FRED release dates before this are not used
FED_HIST_LAST_YEAR = 2020  # later years are on fomccalendars.htm
MIN_SPACING_S = 1.0


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(Path(p).read_bytes())


def redact(url: str) -> str:
    return re.sub(r"(api_key=)[^&]+", r"\1<redacted>", url)


def safe_name(sym: str) -> str:
    return sym.replace("^", "IDX_")


class Http:
    """urllib GET with >= MIN_SPACING_S between requests."""

    def __init__(self, spacing: float = MIN_SPACING_S):
        self.spacing = spacing
        self._last = 0.0

    def get(self, url: str, timeout: float = 60.0) -> bytes:
        wait = self.spacing - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        finally:
            self._last = time.monotonic()


class Manifest:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "manifest.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        else:
            self.data = {"version": 1, "description": "longhist data manifest (raw + normalized)", "files": {}}

    def add(self, rel: str, **entry) -> None:
        full = self.root / rel
        entry["sha256"] = sha256_file(full)
        self.data["files"][rel] = dict(sorted(entry.items()))

    def save(self) -> str:
        self.data["files"] = dict(sorted(self.data["files"].items()))
        b = (json.dumps(self.data, indent=1, ensure_ascii=False) + "\n").encode()
        self.path.write_bytes(b)
        return sha256_bytes(b)


def save_raw(root: Path, source: str, name: str, data: bytes) -> str:
    rel = f"raw/{source}/{name}"
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "xb") as f:  # immutable: never overwrite a raw response
        f.write(data)
    return rel


def write_csv(root: Path, rel: str, rows: list[dict], cols: list[str]) -> str:
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    return rel


def _span(rows: list[dict]) -> dict:
    return {"rows": len(rows), "first_date": rows[0]["date"] if rows else None,
            "last_date": rows[-1]["date"] if rows else None}


def stamp(fetched_utc: str) -> str:
    return fetched_utc.replace("-", "").replace(":", "")


def fetch_yahoo(http: Http, root: Path, man: Manifest, sym: str, log: Callable[[str], None]) -> dict:
    now = int(time.time())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
           f"?period1=0&period2={now}&interval=1d&events=div%2Csplit&includeAdjustedClose=true")
    fetched = N.utc_now()
    raw = http.get(url)
    rel_raw = save_raw(root, "yahoo", f"{safe_name(sym)}_{stamp(fetched)}.json", raw)
    parsed = N.parse_yahoo_chart(raw)
    fetch_day = fetched[:10]
    # The fetch-day bar may be partial: keep only bars dated strictly before the UTC fetch date.
    bars = [b for b in parsed["bars"] if b["date"] < fetch_day]
    dropped = len(parsed["bars"]) - len(bars)
    man.add(rel_raw, kind="raw", source="yahoo_v8_chart (unofficial; do not commit/redistribute)",
            source_url=url, fetched_utc=fetched, symbol=sym, **_span(parsed["bars"]))
    base = f"norm/yahoo/{safe_name(sym)}"
    common = {"kind": "norm", "source": "yahoo_v8_chart", "source_url": url, "fetched_utc": fetched,
              "derived_from": rel_raw, "symbol": sym}
    write_csv(root, base + "_daily.csv", bars, ["date", "open", "high", "low", "close", "adjclose", "volume"])
    man.add(base + "_daily.csv", **common, null_rows_dropped=len(parsed["null_rows"]),
            fetch_day_rows_dropped=dropped, **_span(bars))
    write_csv(root, base + "_dividends.csv", parsed["dividends"], ["date", "amount"])
    man.add(base + "_dividends.csv", **common, **_span(parsed["dividends"]))
    write_csv(root, base + "_splits.csv", parsed["splits"], ["date", "numerator", "denominator", "ratio"])
    man.add(base + "_splits.csv", **common, **_span(parsed["splits"]))
    log(f"yahoo {sym}: {len(bars)} bars {bars[0]['date'] if bars else '-'}..{bars[-1]['date'] if bars else '-'}"
        f" div={len(parsed['dividends'])} split={len(parsed['splits'])} null={len(parsed['null_rows'])}")
    return {"symbol": sym, "rows": len(bars)}


def fetch_cboe(http: Http, root: Path, man: Manifest, name: str, url: str, log) -> None:
    fetched = N.utc_now()
    raw = http.get(url)
    rel_raw = save_raw(root, "cboe", f"{name}_History_{stamp(fetched)}.csv", raw)
    rows = N.parse_cboe(raw.decode("utf-8"))
    man.add(rel_raw, kind="raw", source="cboe_official", source_url=url, fetched_utc=fetched, **_span(rows))
    rel = write_csv(root, f"norm/cboe/{name}.csv", rows, ["date", "open", "high", "low", "close"])
    man.add(rel, kind="norm", source="cboe_official", source_url=url, fetched_utc=fetched,
            derived_from=rel_raw, **_span(rows))
    log(f"cboe {name}: {len(rows)} rows {rows[0]['date']}..{rows[-1]['date']}")


def fetch_dix(http: Http, root: Path, man: Manifest, log) -> None:
    fetched = N.utc_now()
    raw = http.get(DIX_URL)
    rel_raw = save_raw(root, "squeezemetrics", f"DIX_{stamp(fetched)}.csv", raw)
    rows = N.parse_dix(raw.decode("utf-8"))
    man.add(rel_raw, kind="raw", source="squeezemetrics", source_url=DIX_URL, fetched_utc=fetched, **_span(rows))
    rel = write_csv(root, "norm/squeezemetrics/DIX.csv", rows, ["date", "price", "dix", "gex"])
    man.add(rel, kind="norm", source="squeezemetrics", source_url=DIX_URL, fetched_utc=fetched,
            derived_from=rel_raw, **_span(rows))
    log(f"dix: {len(rows)} rows {rows[0]['date']}..{rows[-1]['date']}")


def fred_info(typ: str, rid: int, raw_meta: bytes, raw_dates: bytes, url: str, fetched: str) -> dict:
    """FRED release metadata + release dates -> info dict used by events_yaml."""
    meta = N.parse_fred_release(raw_meta)
    return {"type": typ, "release_id": rid, "name": meta["name"], "dates": N.parse_fred_release_dates(raw_dates),
            "raw_sha256": sha256_bytes(raw_dates), "source_url": redact(url), "fetched_utc": fetched}


def fetch_fred(http: Http, root: Path, man: Manifest, api_key: str, log) -> dict:
    """FRED release metadata + full release-date history per release id. Returns {type: info}."""
    out = {}
    for typ, rid in FRED_RELEASES.items():
        fetched = N.utc_now()
        u_rel = f"https://api.stlouisfed.org/fred/release?release_id={rid}&api_key={api_key}&file_type=json"
        raw_rel = http.get(u_rel)
        if api_key.encode() in raw_rel:
            raise RuntimeError("FRED response echoes the API key; refusing to store it")
        rel_meta = save_raw(root, "fred", f"release_{rid}_{stamp(fetched)}.json", raw_rel)
        man.add(rel_meta, kind="raw", source="fred_api", source_url=redact(u_rel), fetched_utc=fetched,
                rows=1, first_date=None, last_date=None)
        u_dates = (f"https://api.stlouisfed.org/fred/release/dates?release_id={rid}&api_key={api_key}"
                   "&file_type=json&include_release_dates_with_no_data=false&limit=10000&sort_order=asc")
        fetched2 = N.utc_now()
        raw_dates = http.get(u_dates)
        if api_key.encode() in raw_dates:
            raise RuntimeError("FRED response echoes the API key; refusing to store it")
        rel_dates = save_raw(root, "fred", f"release_dates_{rid}_{stamp(fetched2)}.json", raw_dates)
        info = fred_info(typ, rid, raw_rel, raw_dates, u_dates, fetched2)
        dates = info["dates"]
        man.add(rel_dates, kind="raw", source="fred_api", source_url=redact(u_dates), fetched_utc=fetched2,
                rows=len(dates), first_date=dates[0], last_date=dates[-1])
        out[typ] = info
        log(f"fred {typ} ({rid} {info['name']}): {len(dates)} dates {dates[0]}..{dates[-1]}")
    return out


def fomc_from_pages(hist: list[tuple[int, str, str]], cal_page: str, last_year: int,
                    unresolved: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Parse the per-year historical pages [(year, url, html)] and the calendars page."""
    items, unresolved = [], list(unresolved or [])
    for year, url, page in hist:
        it, un = N.parse_fed_historical(page, year, url)
        items += it
        unresolved += un
        if not any(i["type"] == "FOMC" for i in it):
            unresolved.append({"year": year, "heading": None, "source": url, "reason": "no scheduled meeting parsed"})
    it, un = N.parse_fed_calendars(cal_page, FED_CAL)
    it = [i for i in it if FED_HIST_LAST_YEAR < int(i["date"][:4]) <= last_year]
    items += it
    unresolved += un
    for year in range(FED_HIST_LAST_YEAR + 1, last_year + 1):
        if not any(i["type"] == "FOMC" and i["date"].startswith(str(year)) for i in it):
            unresolved.append({"year": year, "heading": None, "source": FED_CAL, "reason": "no scheduled meeting parsed"})
    return sorted(items, key=lambda r: (r["date"], r["type"])), unresolved


def fetch_fomc(http: Http, root: Path, man: Manifest, last_year: int, log) -> tuple[list[dict], list[dict]]:
    hist, failed = [], []
    for year in range(FED_FIRST_YEAR, FED_HIST_LAST_YEAR + 1):
        url = FED_HIST.format(year=year)
        fetched = N.utc_now()
        try:
            raw = http.get(url)
        except OSError as exc:
            failed.append({"year": year, "heading": None, "source": url, "reason": f"fetch failed: {exc}"})
            continue
        rel = save_raw(root, "fed", f"fomchistorical{year}_{stamp(fetched)}.htm", raw)
        page = raw.decode("utf-8", "replace")
        it, _ = N.parse_fed_historical(page, year, url)
        man.add(rel, kind="raw", source="federalreserve.gov", source_url=url, fetched_utc=fetched,
                **_span(sorted(it, key=lambda r: r["date"])))
        hist.append((year, url, page))
    fetched = N.utc_now()
    raw = http.get(FED_CAL)
    rel = save_raw(root, "fed", f"fomccalendars_{stamp(fetched)}.htm", raw)
    cal_page = raw.decode("utf-8", "replace")
    it, _ = N.parse_fed_calendars(cal_page, FED_CAL)
    it = [i for i in it if FED_HIST_LAST_YEAR < int(i["date"][:4]) <= last_year]
    man.add(rel, kind="raw", source="federalreserve.gov", source_url=FED_CAL, fetched_utc=fetched,
            **_span(sorted(it, key=lambda r: r["date"])))
    items, unresolved = fomc_from_pages(hist, cal_page, last_year, failed)
    log(f"fed: {len(items)} items, {len(unresolved)} unresolved")
    return items, unresolved


def _latest_raw(man: Manifest, pattern: str) -> str:
    rels = sorted(r for r in man.data["files"] if re.fullmatch(pattern, r))
    if not rels:
        raise FileNotFoundError(f"no raw file matching {pattern} in manifest")
    return rels[-1]  # names carry a UTC stamp, so the last is the newest


def _raw_verified(man: Manifest, rel: str) -> bytes:
    b = (man.root / rel).read_bytes()
    if sha256_bytes(b) != man.data["files"][rel]["sha256"]:
        raise ValueError(f"{rel} differs from manifest.json")
    return b


def events_from_raw(root: Path) -> dict:
    """Rebuild the events yaml from raw files already listed in manifest.json (no network, no data writes)."""
    man = Manifest(root)
    fred = {}
    for typ, rid in FRED_RELEASES.items():
        rel_meta = _latest_raw(man, rf"raw/fred/release_{rid}_\d{{8}}T\d{{6}}Z\.json")
        rel_dates = _latest_raw(man, rf"raw/fred/release_dates_{rid}_\d{{8}}T\d{{6}}Z\.json")
        entry = man.data["files"][rel_dates]
        fred[typ] = fred_info(typ, rid, _raw_verified(man, rel_meta), _raw_verified(man, rel_dates),
                              entry["source_url"], entry["fetched_utc"])
    fetched = man.data["last_run_utc"]
    hist = []
    for year in range(FED_FIRST_YEAR, FED_HIST_LAST_YEAR + 1):
        rel = _latest_raw(man, rf"raw/fed/fomchistorical{year}_\d{{8}}T\d{{6}}Z\.htm")
        hist.append((year, FED_HIST.format(year=year), _raw_verified(man, rel).decode("utf-8", "replace")))
    cal_rel = _latest_raw(man, r"raw/fed/fomccalendars_\d{8}T\d{6}Z\.htm")
    cal = _raw_verified(man, cal_rel).decode("utf-8", "replace")
    fomc, unresolved = fomc_from_pages(hist, cal, int(fetched[:4]))
    days = _raw_verified(man, "norm/calendar/nyse_sessions.csv").decode().split()[1:]
    ev = events_yaml(fred, fomc, unresolved, days, fetched)
    ev["fetch_failures"] = man.data.get("fetch_failures", [])
    return ev


def trading_days(root: Path) -> list[str]:
    """NYSE sessions = union of SPY and QQQ Yahoo bar dates (normalized CSVs)."""
    days: set[str] = set()
    for sym in ("SPY", "QQQ"):
        with open(Path(root) / f"norm/yahoo/{sym}_daily.csv") as f:
            days |= {r["date"] for r in csv.DictReader(f)}
    return sorted(days)


def build_calendar(root: Path, man: Manifest) -> list[str]:
    days = trading_days(root)
    rows = [{"date": d} for d in days]
    rel = write_csv(root, "norm/calendar/nyse_sessions.csv", rows, ["date"])
    man.add(rel, kind="norm", source="derived: union of Yahoo SPY and QQQ daily bar dates",
            source_url=None, fetched_utc=N.utc_now(),
            derived_from="norm/yahoo/SPY_daily.csv + norm/yahoo/QQQ_daily.csv", **_span(rows))
    return days


def events_yaml(fred: dict, fomc: list[dict], fomc_unresolved: list[dict], days: list[str],
                fetched_utc: str) -> dict:
    """Committed event calendar (public dates only)."""
    today = fetched_utc[:10]
    first_year, last_year = int(days[0][:4]), int(days[-1][:4])
    ec = N.early_closes(set(days), first_year, last_year)
    types: dict = {}
    ambiguous: dict[str, list[dict]] = {}
    for typ, defn in (("CPI", "BLS Consumer Price Index release day (FRED release dates)"),
                      ("NFP", "BLS Employment Situation release day (FRED release dates)"),
                      ("PCE", "BEA Personal Income and Outlays release day (FRED release dates)")):
        f = fred[typ]
        ambiguous[typ] = N.multi_date_months(f["dates"], FRED_START)
        amb_months = {m["month"] for m in ambiguous[typ]}
        kept = [d for d in f["dates"] if d >= FRED_START and d[:7] not in amb_months]
        types[typ] = {"definition": defn + (f"; dates before {FRED_START} are not used (low confidence); months "
                                            "where FRED lists more than one date are left out and listed under "
                                            "`unresolved`"),
                      "source": f"FRED release {f['release_id']} '{f['name']}'",
                      "source_url": f["source_url"], "raw_sha256": f["raw_sha256"],
                      "fetched_utc": f["fetched_utc"],
                      "coverage": {"start": FRED_START, "end": f["dates"][-1]},
                      "ambiguous_months_excluded": len(amb_months), "dates": kept}
    sched = [i for i in fomc if i["type"] == "FOMC" and i["date"] <= today]
    unsched = [i for i in fomc if i["type"] == "FOMC_UNSCHEDULED" and i["date"] <= today]
    excluded = [i for i in fomc if i["type"].startswith("EXCLUDED") and i["date"] <= today]
    fomc_cov = {"start": f"{FED_FIRST_YEAR}-01-01", "end": today}
    types["FOMC"] = {
        "definition": "decision day = last day of each SCHEDULED FOMC meeting (Federal Reserve pages). "
                      "Before 1994-02-04 the FOMC did not announce decisions on the meeting day. From 2000 "
                      "a meeting heading without a Statement on the Fed page is moved to `unresolved`.",
        "coverage": fomc_cov,
        "events": [{"date": i["date"], "label": i["label"], "source": i["source"]} for i in sched]}
    types["FOMC_UNSCHEDULED"] = {
        "definition": "unscheduled meetings and conference calls listed on the Fed pages (date = last day). "
                      "Not every item carried a policy change; not resolved per item. Some fall on weekends "
                      "or market holidays; a study that uses them maps such a date to the next session.",
        "coverage": fomc_cov,
        "events": [{"date": i["date"], "label": i["label"], "source": i["source"]} for i in unsched]}
    types["EARLY_CLOSE"] = {
        "definition": "NYSE 1:00 p.m. early close, RULE-DERIVED (confidence J): July 3, the day after "
                      "Thanksgiving and December 24, each only when a weekday that is a session in the "
                      "trading-day calendar. Non-standard early closes are not included.",
        "coverage": {"start": days[0], "end": days[-1]},
        "events": ec}
    fp = fred["FOMC_PRESS_RELEASE"]
    return {
        "version": "longhist-events-v1",
        "generated_by": "scripts/longhist_fetch.py",
        "fetched_utc": fetched_utc,
        "trading_day_calendar": {
            "definition": "NYSE sessions = union of Yahoo SPY and QQQ daily bar dates (SPY only before "
                          "1999-03-10). File norm/calendar/nyse_sessions.csv under the longhist data root "
                          "(not committed).",
            "first": days[0], "last": days[-1], "sessions": len(days)},
        "fred_release_check": [
            {"type": t, "release_id": fred[t]["release_id"], "name": fred[t]["name"], "dates": len(fred[t]["dates"])}
            for t in fred],
        "notes": [
            (f"FRED release {fp['release_id']} '{fp['name']}' lists {len(fp['dates'])} dates "
             f"({fp['dates'][0]}..{fp['dates'][-1]}), mostly daily data updates, so it is NOT used for "
             "FOMC decision days. FOMC dates come from federalreserve.gov pages."),
            ("FRED release dates are the dates FRED published the release, which also include "
             "revision/benchmark-only days. Only dates from 1993-01-01 are kept; months with more than one "
             "date are removed from `dates` and listed under `unresolved`. Only 2023-04..2026-01 is "
             "cross-checked against BLS/BEA (docs/research/DATA_LONGHIST_V1.md)."),
        ],
        "types": types,
        "excluded_fomc_items": [{"date": i["date"], "label": i["label"], "reason": i["type"], "source": i["source"]}
                                for i in excluded],
        "unresolved": [{"type": "FOMC", **u} for u in fomc_unresolved] + [
            {"type": t, "month": m["month"], "dates": m["dates"], "source": fred[t]["source_url"],
             "reason": "FRED lists more than one date in this month; the regular release is not resolved "
                       "(one date may be a revision/benchmark-only publication)"}
            for t in ("CPI", "NFP", "PCE") for m in ambiguous[t]],
    }

