"""Config -> candidates and condition values (PREREG §4.2-§4.3): 29 main + 3 GEX conditions, calendar and event
flags, numbering. A condition value is True / False / <NA> (undefined: no trade that day, counted)."""
from __future__ import annotations

import bisect
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config" / "daily_screen_v1.json"
EVENTS_PATH = ROOT / "config" / "longhist_events_v1.yaml"
INDICES = ("SPY", "QQQ", "^SOX")
SIDES = ("long", "short")
FOMC_FIRST = "1994-02-04"  # decisions announced on the meeting day from here on (§4.2 C26)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_config(path: Path = CONFIG_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main_k(index: str, cond: int, side: str) -> int:
    return 58 * INDICES.index(index) + 2 * (cond - 1) + SIDES.index(side)


def gex_k(index: str, cond: int, side: str) -> int:
    return 6 * (cond - 1) + 2 * INDICES.index(index) + SIDES.index(side)


# ------------------------------------------------------------------ calendar flags (known in advance)


def third_friday(y: int, m: int) -> date:
    first = date(y, m, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def calendar_flags(calendar: list[str], unscheduled: list[str]) -> pd.DataFrame:
    """Per session: weekday (0 = Mon), month_edge (last or first 3 of month), holiday_eve, opex, quarter_end."""
    cal = sorted(calendar)
    unsched = set(unscheduled)
    df = pd.DataFrame({"date": cal})
    days = [date.fromisoformat(d) for d in cal]
    df["weekday"] = [d.weekday() for d in days]
    ym = df["date"].str[:7]
    rank = df.groupby(ym).cumcount()
    last = ym != ym.shift(-1)
    df["month_edge"] = last | (rank < 3)
    df["quarter_end"] = last & df["date"].str[5:7].isin(["03", "06", "09", "12"])
    eve = []
    for i, d in enumerate(days):
        if i + 1 == len(days):
            eve.append(False)
            continue
        between = (d + timedelta(days=j) for j in range(1, (days[i + 1] - d).days))
        eve.append(any(x.weekday() < 5 and x.isoformat() not in unsched for x in between))
    df["holiday_eve"] = eve
    opex = set()
    sessions = set(cal)
    for y, m in sorted({(d.year, d.month) for d in days}):
        tf = third_friday(y, m).isoformat()
        if tf in sessions:
            opex.add(tf)
        else:
            i = bisect.bisect_left(cal, tf)
            if i > 0:
                opex.add(cal[i - 1])
    df["opex"] = df["date"].isin(opex)
    return df.set_index("date")


# ------------------------------------------------------------------ events (resolved yaml dates only)


def load_events(path: Path = EVENTS_PATH) -> dict:
    """FOMC decision dates (>= 1994-02-04), unknown FOMC dates, CPI/NFP dates and unknown months."""
    ev = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    out = {"FOMC": sorted(e["date"] for e in ev["types"]["FOMC"]["events"] if e["date"] >= FOMC_FIRST),
           "FOMC_unknown": sorted(u["date"] for u in ev["unresolved"] if u["type"] == "FOMC")}
    for t in ("CPI", "NFP"):
        out[t] = sorted(ev["types"][t]["dates"])
        out[f"{t}_unknown_months"] = sorted(u["month"] for u in ev["unresolved"] if u["type"] == t)
    return out


def event_flags(calendar: list[str], events: dict) -> pd.DataFrame:
    """Per session: fomc, fomc_eve, cpi, nfp as True/False/<NA> (<NA> = unknown, excluded from that candidate)."""
    cal = sorted(calendar)
    idx = {d: i for i, d in enumerate(cal)}
    df = pd.DataFrame(index=pd.Index(cal, name="date"))
    fomc = set(events["FOMC"]) & set(cal)
    fomc_unknown = set(events["FOMC_unknown"]) & set(cal)
    df["fomc"] = pd.array([True if d in fomc else pd.NA if d in fomc_unknown else False for d in cal],
                          dtype="boolean")
    eve = {cal[idx[d] - 1] for d in fomc if idx[d] > 0}
    eve_unknown = {cal[idx[d] - 1] for d in fomc_unknown if idx[d] > 0}
    df["fomc_eve"] = pd.array([True if d in eve else pd.NA if d in eve_unknown else False for d in cal],
                              dtype="boolean")
    for t in ("CPI", "NFP"):
        dates, unknown = set(events[t]), set(events[f"{t}_unknown_months"])
        df[t.lower()] = pd.array([pd.NA if d[:7] in unknown else d in dates for d in cal], dtype="boolean")
    return df


# ------------------------------------------------------------------ condition values


def _cmp(mask, undefined) -> pd.Series:
    s = pd.Series(mask, dtype="boolean")
    s[pd.Series(undefined, index=s.index).fillna(True).astype(bool)] = pd.NA
    return s


def main_condition(cond: int, t: pd.DataFrame, thr: dict, cal: pd.DataFrame, ev: pd.DataFrame) -> pd.Series:
    """Value of condition C{cond:02d} on each row of a session table `t` (index aligned with `t`).

    thr: {"vix_t1", "vix_t2", "mg", "qo", "prng_q20", "prng_q80"} for this index. Only d's open (in g) and
    values dated p(d) or earlier are used."""
    g, poc, prng, vix, v9, v3 = (t[c] for c in ("g", "poc", "prng", "vix_p", "v9_p", "v3_p"))
    d = t["date"]
    if cond == 1:
        return _cmp(pd.Series(True, index=t.index), pd.Series(False, index=t.index))
    if cond in (2, 3, 4):
        lo, hi = thr["vix_t1"], thr["vix_t2"]
        m = {2: vix <= lo, 3: (vix > lo) & (vix <= hi), 4: vix > hi}[cond]
        return _cmp(m, vix.isna())
    if cond == 5:
        return _cmp(v9 / vix > 1, v9.isna() | vix.isna())
    if cond == 6:
        return _cmp(vix / v3 > 1, vix.isna() | v3.isna())
    if cond in (7, 8, 9, 10):
        big = g.abs() >= thr["mg"]
        m = {7: (g > 0) & big, 8: (g > 0) & ~big, 9: (g < 0) & ~big, 10: (g < 0) & big}[cond]
        return _cmp(m, g.isna())
    if cond in (11, 12, 13, 14):
        m = {11: poc > 0, 12: poc < 0, 13: poc >= thr["qo"], 14: poc <= -thr["qo"]}[cond]
        return _cmp(m, poc.isna())
    if cond in (15, 16):
        m = prng >= thr["prng_q80"] if cond == 15 else prng <= thr["prng_q20"]
        return _cmp(m, prng.isna())
    if 17 <= cond <= 21:
        return _cmp(d.map(cal["weekday"]) == cond - 17, ~d.isin(cal.index))
    col = {22: "month_edge", 23: "holiday_eve", 24: "opex", 25: "quarter_end"}.get(cond)
    if col is not None:
        return _cmp(d.map(cal[col]).fillna(False).astype(bool), ~d.isin(cal.index))
    col = {26: "fomc", 27: "fomc_eve", 28: "cpi", 29: "nfp"}.get(cond)
    if col is not None:
        s = pd.Series(ev[col].reindex(d.to_numpy()).to_numpy(), index=t.index, dtype="boolean")
        return s
    raise ValueError(f"unknown main condition {cond}")


def gex_condition(cond: int, t: pd.DataFrame, gex_q20: float | None) -> pd.Series:
    """G01 GEX_p < 0, G02 GEX_p >= 0, G03 GEX_p <= GEX DEV 20% quantile (fixed by the GEX precount)."""
    x = t["gex_p"]
    if cond == 1:
        return _cmp(x < 0, x.isna())
    if cond == 2:
        return _cmp(x >= 0, x.isna())
    if cond == 3:
        if gex_q20 is None:
            raise ValueError("G03 needs the GEX DEV 20% quantile")
        return _cmp(x <= gex_q20, x.isna())
    raise ValueError(f"unknown GEX condition {cond}")
