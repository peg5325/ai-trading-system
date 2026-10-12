"""Pure parsers and rule derivations for the long-history data set (no network, no returns).

- Yahoo v8 chart JSON -> daily bars (raw split-adjusted OHLC + adjclose), dividend and split events.
- Cboe daily index CSV, SqueezeMetrics DIX.csv, FRED release/dates JSON.
- Federal Reserve FOMC pages (historical per-year pages and the current calendars page).
- NYSE 1:00 p.m. early closes derived from the standard rules against a given trading-day set.
"""
from __future__ import annotations

import csv
import html
import io
import json
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- Yahoo


def _num(v):
    return None if v is None else float(v)


def parse_yahoo_chart(raw: bytes) -> dict:
    """Yahoo v8 chart JSON -> {meta, bars, dividends, splits, null_rows}.

    Dates are the exchange-local calendar date of each bar timestamp (meta.exchangeTimezoneName).
    Rows whose open/high/low/close are all null are dropped and listed in `null_rows`.
    `close` is Yahoo's raw close (already split-adjusted, NOT dividend-adjusted); `adjclose` is
    Yahoo's split+dividend adjusted close as of the fetch time.
    """
    res = json.loads(raw)["chart"]["result"][0]
    meta = res["meta"]
    tz = ZoneInfo(meta.get("exchangeTimezoneName") or "America/New_York")

    def d(ts: int) -> str:
        return datetime.fromtimestamp(ts, tz).date().isoformat()

    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or [None] * len(ts)
    bars, nulls = [], []
    for i, t in enumerate(ts):
        o, h, lo, c = (q[k][i] for k in ("open", "high", "low", "close"))
        if o is None and h is None and lo is None and c is None:
            nulls.append(d(t))
            continue
        bars.append({"date": d(t), "open": _num(o), "high": _num(h), "low": _num(lo), "close": _num(c),
                     "adjclose": _num(adj[i]), "volume": q["volume"][i]})
    ev = res.get("events") or {}
    divs = sorted(({"date": d(e["date"]), "amount": float(e["amount"])}
                   for e in (ev.get("dividends") or {}).values()), key=lambda r: r["date"])
    splits = sorted(({"date": d(e["date"]), "numerator": float(e["numerator"]),
                      "denominator": float(e["denominator"]), "ratio": e.get("splitRatio", "")}
                     for e in (ev.get("splits") or {}).values()), key=lambda r: r["date"])
    dup = len(bars) - len({b["date"] for b in bars})
    if dup:
        raise ValueError(f"Yahoo chart has {dup} duplicate bar dates")
    return {"meta": {"symbol": meta.get("symbol"), "timezone": str(tz), "currency": meta.get("currency"),
                     "instrumentType": meta.get("instrumentType")},
            "bars": bars, "dividends": divs, "splits": splits, "null_rows": nulls}


# ---------------------------------------------------------------- Cboe / SqueezeMetrics


def parse_cboe(text: str) -> list[dict]:
    """Cboe daily CSV -> [{date, open, high, low, close}] (open/high/low None for close-only files).

    Accepts `DATE,OPEN,HIGH,LOW,CLOSE` and two-column `DATE,<NAME>` layouts; DATE is MM/DD/YYYY.
    """
    reader = csv.reader(io.StringIO(text.strip()))
    header = [h.strip().upper() for h in next(reader)]
    if header[0] != "DATE":
        raise ValueError(f"unexpected Cboe header {header}")
    rows = []
    for parts in reader:
        if not parts or not parts[0].strip():
            continue
        m, dd, y = (int(x) for x in parts[0].strip().split("/"))
        vals = [float(x) if x.strip() not in ("", ".") else None for x in parts[1:]]
        if len(header) >= 5:
            o, h, lo, c = vals[:4]
        else:
            o = h = lo = None
            c = vals[0]
        rows.append({"date": date(y, m, dd).isoformat(), "open": o, "high": h, "low": lo, "close": c})
    rows.sort(key=lambda r: r["date"])
    if len(rows) != len({r["date"] for r in rows}):
        raise ValueError("Cboe CSV has duplicate dates")
    return rows


def parse_dix(text: str) -> list[dict]:
    """SqueezeMetrics DIX.csv (date,price,dix,gex) -> [{date, price, dix, gex}]."""
    reader = csv.reader(io.StringIO(text.strip()))
    header = [h.strip().lower() for h in next(reader)]
    idx = {k: header.index(k) for k in ("date", "price", "dix", "gex")}
    rows = []
    for parts in reader:
        if not parts:
            continue
        rows.append({"date": date.fromisoformat(parts[idx["date"]].strip()).isoformat(),
                     **{k: float(parts[idx[k]]) for k in ("price", "dix", "gex")}})
    rows.sort(key=lambda r: r["date"])
    if len(rows) != len({r["date"] for r in rows}):
        raise ValueError("DIX.csv has duplicate dates")
    return rows


# ---------------------------------------------------------------- FRED


def parse_fred_release(raw: bytes) -> dict:
    rel = json.loads(raw)["releases"][0]
    return {"id": int(rel["id"]), "name": rel["name"], "link": rel.get("link", "")}


def parse_fred_release_dates(raw: bytes) -> list[str]:
    j = json.loads(raw)
    if int(j.get("count", 0)) != len(j["release_dates"]):
        raise ValueError("FRED release_dates count mismatch (pagination needed)")
    return sorted({r["date"] for r in j["release_dates"]})


def multi_date_months(dates: list[str], since: str) -> list[dict]:
    """Calendar months (on/after `since`) in which a monthly release lists more than one date.

    FRED release dates also carry revision/benchmark-only publication days; which date is the
    regular monthly release is not resolved here, so such months are reported, not guessed.
    """
    by: dict[str, list[str]] = {}
    for d in dates:
        if d >= since:
            by.setdefault(d[:7], []).append(d)
    return [{"month": m, "dates": sorted(v)} for m, v in sorted(by.items()) if len(v) > 1]


# ---------------------------------------------------------------- Federal Reserve FOMC pages

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"], start=1)}
_MON_ABBR = {k[:3]: v for k, v in _MONTHS.items()}


def _month(name: str) -> int:
    name = name.strip().rstrip(".")
    if name in _MONTHS:
        return _MONTHS[name]
    if name[:3] in _MON_ABBR:
        return _MON_ABBR[name[:3]]
    raise ValueError(f"unknown month {name!r}")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def classify_fomc(kind: str, tag: str) -> str:
    """FOMC (scheduled meeting decision day) | FOMC_UNSCHEDULED | EXCLUDED_NOTATION | EXCLUDED_CANCELLED."""
    t = tag.lower()
    if "cancel" in t:
        return "EXCLUDED_CANCELLED"
    if "notation" in t:
        return "EXCLUDED_NOTATION"
    if "unscheduled" in t or kind.lower() == "conference call":
        return "FOMC_UNSCHEDULED"
    if kind.lower() == "meeting" and not t.strip():
        return "FOMC"
    raise ValueError(f"unclassified FOMC item kind={kind!r} tag={tag!r}")


_HIST_RE = re.compile(
    r"^(?P<m1>[A-Z][a-z]+)(?:/(?P<m1b>[A-Z][a-z]+))?\.? (?P<d1>\d{1,2})(?:\s*-\s*(?:(?P<m2>[A-Z][a-z]+)\.? )?(?P<d2>\d{1,2}))?"
    r"(?: \((?P<tag>[^)]*)\))?(?: (?P<kind>Meeting|Conference Call))? - (?P<y>\d{4})$")


def parse_fed_historical(page: str, year: int, url: str) -> tuple[list[dict], list[dict]]:
    """fomchistoricalYYYY.htm -> (items, unresolved). Item date = last day of the meeting/call."""
    items, unresolved = [], []
    for raw in re.findall(r"<h5[^>]*>(.*?)</h5>", page, re.DOTALL):
        head = _clean(raw)
        m = _HIST_RE.match(head)
        if not m or int(m["y"]) != year:
            unresolved.append({"year": year, "heading": head, "source": url, "reason": "heading not parsed"})
            continue
        try:
            mon = _month(m["m2"] or m["m1b"] or m["m1"])
            day = int(m["d2"] or m["d1"])
            typ = classify_fomc(m["kind"] or "", m["tag"] or "")
            items.append({"date": date(year, mon, day).isoformat(), "type": typ, "label": head, "source": url})
        except ValueError as exc:
            unresolved.append({"year": year, "heading": head, "source": url, "reason": str(exc)})
    return items, unresolved


_CAL_ROW = re.compile(
    r'fomc-meeting__month[^"]*">(?P<month>.*?)</div>\s*<div class="fomc-meeting__date[^"]*">(?P<date>.*?)</div>',
    re.DOTALL)
_CAL_DATE = re.compile(r"^(?P<d1>\d{1,2})(?:\s*-\s*(?P<d2>\d{1,2}))?\*?\s*(?:\((?P<tag>[^)]*)\))?\*?$")


def parse_fed_calendars(page: str, url: str) -> tuple[list[dict], list[dict]]:
    """fomccalendars.htm -> (items, unresolved) for every '<YYYY> FOMC Meetings' panel on the page."""
    items, unresolved = [], []
    heads = list(re.finditer(r"(\d{4}) FOMC Meetings", page))
    for i, h in enumerate(heads):
        year = int(h.group(1))
        seg = page[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(page)]
        for row in _CAL_ROW.finditer(seg):
            mon_txt, date_txt = _clean(row["month"]), _clean(row["date"])
            label = f"{mon_txt} {date_txt} - {year}"
            m = _CAL_DATE.match(date_txt)
            try:
                if not m:
                    raise ValueError("date cell not parsed")
                months = [p for p in re.split(r"[/-]", mon_txt) if p.strip()]
                mon = _month(months[-1])
                day = int(m["d2"] or m["d1"])
                tag = m["tag"] or ""
                typ = classify_fomc("Meeting", tag)
                items.append({"date": date(year, mon, day).isoformat(), "type": typ, "label": label, "source": url})
            except (ValueError, IndexError) as exc:
                unresolved.append({"year": year, "heading": label, "source": url, "reason": str(exc)})
    return items, unresolved


# ---------------------------------------------------------------- NYSE early closes (rule-derived, J)


def thanksgiving(year: int) -> date:
    d = date(year, 11, 1)
    first_thu = d + timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + timedelta(weeks=3)


def early_close_candidates(year: int) -> list[tuple[date, str]]:
    """Standard NYSE 1:00 p.m. early-close rule days for `year` (before the trading-day filter)."""
    return [
        (date(year, 7, 3), "day before Independence Day"),
        (thanksgiving(year) + timedelta(days=1), "day after Thanksgiving"),
        (date(year, 12, 24), "Christmas Eve"),
    ]


def early_closes(trading_days: set[str], first_year: int, last_year: int) -> list[dict]:
    """Rule-derived early closes: candidate day must be a weekday AND a trading day in `trading_days`.

    July 3 on a Friday (July 4 Saturday) and Dec 24 on a Friday (Dec 25 Saturday) are observed full
    holidays, so the trading-day filter drops them; weekends are dropped by the weekday check.
    """
    out = []
    for y in range(first_year, last_year + 1):
        for d, rule in early_close_candidates(y):
            if d.weekday() < 5 and d.isoformat() in trading_days:
                out.append({"date": d.isoformat(), "rule": rule, "confidence": "J"})
    return out


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
