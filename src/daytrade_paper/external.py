"""External prior-day inputs (Cboe VIX/VIX9D, SqueezeMetrics GEX) and the event calendar.

Parsing and prior-day selection are pure. `fetch_external` performs the only network I/O
(public CSV downloads; no broker API) and records failures as missing values.
"""
from __future__ import annotations

import hashlib
import urllib.request
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from src.daytrade_paper import rules as R

SOURCES = {
    "vix": ("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv", "VIX_History.csv"),
    "vix9d": ("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv", "VIX9D_History.csv"),
    "gex": ("https://squeezemetrics.com/monitor/static/DIX.csv", "DIX.csv"),
}


def parse_cboe(text: str) -> list[tuple[str, float]]:
    """Cboe daily CSV (DATE,OPEN,HIGH,LOW,CLOSE; DATE as MM/DD/YYYY) -> [(iso date, close)]."""
    rows = []
    for line in text.strip().splitlines()[1:]:
        parts = line.strip().split(",")
        if len(parts) < 5:
            continue
        try:
            m, dd, y = (int(x) for x in parts[0].split("/"))
            d = Date(y, m, dd).isoformat()
            rows.append((d, float(parts[4])))
        except ValueError:
            continue
    return sorted(rows)


def parse_dix(text: str) -> list[tuple[str, float]]:
    """SqueezeMetrics DIX.csv (date,price,dix,gex) -> [(iso date, gex)]."""
    lines = text.strip().splitlines()
    header = [h.strip().lower() for h in lines[0].split(",")]
    gi = header.index("gex")
    rows = []
    for line in lines[1:]:
        parts = line.strip().split(",")
        try:
            rows.append((Date.fromisoformat(parts[0]).isoformat(), float(parts[gi])))
        except (ValueError, IndexError):
            continue
    return sorted(rows)


def prior_value(rows: list[tuple[str, float]], day: str) -> dict:
    """Last value dated strictly before `day`; missing if none or older than the max age."""
    prev = [r for r in rows if r[0] < day]
    if not prev:
        return {"value": None, "date": None, "status": "missing: no row before session"}
    d, v = prev[-1]
    age = (Date.fromisoformat(day) - Date.fromisoformat(d)).days
    if age > R.EXTERNAL_MAX_AGE_DAYS:
        return {"value": None, "date": d, "status": f"missing: stale ({age} days old)"}
    return {"value": v, "date": d, "status": "ok"}


def _download(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (daytrade-paper research)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_external(day: str, out_dir: Path, download=_download) -> dict:
    """Download the three CSVs into out_dir/<day>/ and return prior-day values per source."""
    target = out_dir / day
    result = {}
    for key, (url, name) in SOURCES.items():
        entry = {"url": url, "fetched_at": datetime.now(timezone.utc).isoformat()}
        try:
            raw = download(url)
            target.mkdir(parents=True, exist_ok=True)
            (target / name).write_bytes(raw)
            text = raw.decode("utf-8", errors="replace")
            rows = parse_dix(text) if key == "gex" else parse_cboe(text)
            entry.update(prior_value(rows, day))
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            entry["file"] = str(target / name)
        except (OSError, ValueError, IndexError) as exc:  # network/parse failure -> missing
            entry.update({"value": None, "date": None, "status": f"missing: {type(exc).__name__}: {exc}"})
        result[key] = entry
    return result


INDICATOR_TYPES = {"CPI", "NFP", "PCE"}


def load_events(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def events_for(cal: dict, day: str) -> dict:
    """Event flags for `day`. known=False when the day is outside the calendar coverage."""
    cov = cal["coverage"]
    known = str(cov["start"]) <= day <= str(cov["end"])
    items = [e for e in cal.get("events", []) if str(e["date"]) == day]
    types = sorted({e["type"] for e in items})
    return {"known": known, "calendar_version": cal.get("version"), "types": types,
            "fomc": "FOMC" in types, "early_close": "EARLY_CLOSE" in types,
            "indicator": bool(INDICATOR_TYPES & set(types)),
            "items": [{k: str(v) for k, v in e.items()} for e in items]}


def nyse_holidays(cal: dict) -> set[str]:
    """NYSE full-day closures listed in the calendar (`holidays` key)."""
    return {str(h["date"]) for h in cal.get("holidays") or []}


def is_trading_day(cal: dict, day: str) -> bool:
    """NYSE trading day = weekday that is not a listed full-day holiday."""
    return Date.fromisoformat(day).weekday() < 5 and day not in nyse_holidays(cal)


def prev_trading_day(cal: dict, day: str) -> str:
    """The NYSE trading day immediately before `day`."""
    d = Date.fromisoformat(day) - timedelta(days=1)
    while not is_trading_day(cal, d.isoformat()):
        d -= timedelta(days=1)
    return d.isoformat()
