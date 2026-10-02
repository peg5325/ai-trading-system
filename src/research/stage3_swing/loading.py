"""Raw-file parsing and panel construction (PREREG section 2).

Reads saved raw files only. Every parser drops rows dated after DATA_END immediately
after parsing and asserts none remain, so blind-OOS rows never reach computation.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import contract as C

_END = pd.Timestamp(C.DATA_END)
UNKNOWN_TD = 99  # calendar attribute of the final (possibly incomplete) month


def cut_end(obj):
    """Drop every row dated after DATA_END and assert none remain."""
    out = obj[obj.index <= _END]
    assert len(out) == 0 or out.index.max() <= _END, "post-DATA_END row survived"
    return out


def parse_yahoo(raw: bytes) -> pd.DataFrame:
    """Yahoo v8 chart JSON -> DataFrame[close, div] indexed by exchange date.

    `close` is Yahoo's raw (split-adjusted, not dividend-adjusted) close; dividends are
    ex-date cash amounts on the same basis. Adjusted close is never used.
    """
    result = json.loads(raw)["chart"]["result"][0]
    stamps = result["timestamp"]
    close = result["indicators"]["quote"][0]["close"]
    idx = pd.DatetimeIndex(
        [datetime.fromtimestamp(t, timezone.utc).date() for t in stamps]
    )
    df = pd.DataFrame({"close": close}, index=idx, dtype=float)
    df = df[~df.index.duplicated(keep="last")]
    divs = (result.get("events") or {}).get("dividends") or {}
    div = pd.Series(
        {
            pd.Timestamp(datetime.fromtimestamp(int(k), timezone.utc).date()): float(v["amount"])
            for k, v in divs.items()
        },
        dtype=float,
    )
    df["div"] = div.reindex(df.index).fillna(0.0) if len(div) else 0.0
    df = df.dropna(subset=["close"]).sort_index()
    return cut_end(df)


def parse_cboe_vix(raw: bytes) -> pd.Series:
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    header = [h.strip().upper() for h in rows[0]]
    di, ci = header.index("DATE"), header.index("CLOSE")
    data = {
        pd.to_datetime(r[di].strip(), format="%m/%d/%Y"): float(r[ci])
        for r in rows[1:]
        if len(r) > ci and r[di].strip() and r[ci].strip()
    }
    return cut_end(pd.Series(data, dtype=float).sort_index())


def parse_french_rf(raw: bytes) -> pd.Series:
    """Ken French daily F-F factors zip -> RF as a decimal daily return."""
    zf = zipfile.ZipFile(io.BytesIO(raw))
    lines = zf.read(zf.namelist()[0]).decode("latin-1").splitlines()
    data = {}
    for line in lines:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5 and len(parts[0]) == 8 and parts[0].isdigit():
            data[pd.to_datetime(parts[0], format="%Y%m%d")] = float(parts[4]) / 100.0
    return cut_end(pd.Series(data, dtype=float).sort_index())


def tr_level(df: pd.DataFrame) -> pd.Series:
    """Total-return level from raw close + dividends: (close_t + div_t) / close_{t-1}."""
    ret = (df["close"] + df["div"]) / df["close"].shift(1) - 1.0
    return (1.0 + ret.fillna(0.0)).cumprod().where(df["close"].notna())


@dataclass
class RawData:
    yahoo: dict[str, pd.DataFrame]
    cboe_vix: pd.Series
    rf: pd.Series


def load_raw_dir(raw_dir: Path) -> RawData:
    yahoo = {}
    for sym in C.YAHOO_SYMBOLS:
        path = raw_dir / f"yahoo_{sym.replace('^', 'IDX_')}.json"
        yahoo[sym] = parse_yahoo(path.read_bytes())
    return RawData(
        yahoo=yahoo,
        cboe_vix=parse_cboe_vix((raw_dir / "VIX_History.csv").read_bytes()),
        rf=parse_french_rf((raw_dir / "ff_daily.zip").read_bytes()),
    )


def calendar_flags(dates: pd.DatetimeIndex):
    """Calendar attributes known in advance: month-end flag, trading-day position."""
    ym = dates.year * 100 + dates.month
    n = len(dates)
    ym_arr = np.asarray(ym)
    td_of_month = np.zeros(n, dtype=np.int64)
    td_from_end = np.zeros(n, dtype=np.int64)
    start = 0
    for i in range(1, n + 1):
        if i == n or ym_arr[i] != ym_arr[start]:
            length = i - start
            td_of_month[start:i] = np.arange(1, length + 1)
            td_from_end[start:i] = np.arange(length - 1, -1, -1)
            start = i
    is_me = td_from_end == 0
    # the final month may be cut by DATA_END: its end-of-month attributes are unknown
    last = ym_arr == ym_arr[-1]
    td_from_end[last] = UNKNOWN_TD
    is_me[last] = False
    return is_me, td_of_month, td_from_end, np.asarray(dates.month)


@dataclass
class Panel:
    """Aligned daily arrays on the SPY calendar from PREP_START to DATA_END."""

    dates: pd.DatetimeIndex
    pr: dict[str, np.ndarray]  # signal-index price return
    ru: dict[str, np.ndarray]  # total-return r_u used for products
    rf: np.ndarray
    vix: np.ndarray
    is_me: np.ndarray
    td_of_month: np.ndarray
    td_from_end: np.ndarray
    month: np.ndarray
    etf_tr: dict[str, np.ndarray] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


def build_panel(raw: RawData) -> Panel:
    cal = raw.yahoo["SPY"].index
    cal = cal[(cal <= _END)]
    meta: dict = {"calendar": "SPY trading days", "pre_spy_index_dates_unused": True}
    missing: dict[str, int] = {}

    def level(series: pd.Series, name: str) -> pd.Series:
        series = series.dropna()
        first = series.index.min()
        own = cal[cal >= first]
        missing[name] = int((~own.isin(series.index)).sum())
        return series.reindex(cal, method="ffill")

    gspc = level(raw.yahoo["^GSPC"]["close"], "^GSPC").pct_change()
    ndx_pr = level(raw.yahoo["^NDX"]["close"], "^NDX").pct_change()
    sp_ru = level(raw.yahoo["^SP500TR"]["close"], "^SP500TR").pct_change()
    qqq_tr = level(tr_level(raw.yahoo["QQQ"]), "QQQ").pct_change()
    qqq_ok = qqq_tr.notna() & (qqq_tr.index >= pd.Timestamp(C.QQQ_START))
    ndx_ru = ndx_pr.where(~qqq_ok, qqq_tr + C.NDX_QQQ_ER / 252.0)

    rf = raw.rf.reindex(cal, method="ffill")
    vix_cboe = raw.cboe_vix.reindex(cal)
    vix_yahoo = raw.yahoo["^VIX"]["close"].reindex(cal)
    meta["vix_filled_from_yahoo"] = int((vix_cboe.isna() & vix_yahoo.notna()).sum())
    vix = vix_cboe.fillna(vix_yahoo)

    etf = {}
    for sym, _, _ in C.CALIBRATION_PAIRS:
        etf[sym] = level(tr_level(raw.yahoo[sym]), sym).pct_change()

    keep = cal >= pd.Timestamp(C.PREP_START)
    dates = cal[keep]
    assert len(dates) and dates.max() <= _END, "post-DATA_END row in panel"
    arrays = {
        "gspc": gspc[keep], "ndx_pr": ndx_pr[keep], "sp_ru": sp_ru[keep],
        "ndx_ru": ndx_ru[keep], "rf": rf[keep], "vix": vix[keep],
    }
    for name, ser in arrays.items():
        if ser.isna().any():
            raise ValueError(f"NaN in panel series {name} at {ser.index[ser.isna()][0].date()}")
    meta["index_days_missing_vs_calendar"] = missing
    is_me, td_of_month, td_from_end, month = calendar_flags(dates)
    return Panel(
        dates=dates,
        pr={"sp": arrays["gspc"].to_numpy(), "ndx": arrays["ndx_pr"].to_numpy()},
        ru={"sp": arrays["sp_ru"].to_numpy(), "ndx": arrays["ndx_ru"].to_numpy()},
        rf=arrays["rf"].to_numpy(),
        vix=arrays["vix"].to_numpy(),
        is_me=is_me,
        td_of_month=td_of_month,
        td_from_end=td_from_end,
        month=month,
        etf_tr={k: v[keep].to_numpy() for k, v in etf.items()},
        meta=meta,
    )
