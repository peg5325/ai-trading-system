"""Guarded loader for the long-history daily data set.

- The manifest is pinned: `config/longhist_data_v1.json` holds the expected SHA-256 of manifest.json, and
  every call checks the manifest against it, then checks the requested file against its SHA-256 in the
  manifest. Any mismatch or an unlisted file raises `ManifestMismatch`. A different expected manifest SHA
  can only be passed explicitly (tests).
- purpose="performance" refuses any request whose range reaches on/after the swing blind-OOS start
  (2025-07-21, `src.history.oos_guard.blind_oos_window("swing")`), and an open-ended end. This is
  stricter than `assert_window_unlocked` (which would let ranges after 2026-07-21 through) on purpose:
  the next research stage seals everything from 2025-07-21 on.
- purpose="coverage" returns the `date` column only (no prices), for date-existence checks. Which files
  may use it is fixed by an AST test (tests/test_longhist.py).
"""
from __future__ import annotations

import hashlib
import io
import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from src.history.oos_guard import TRACK_SWING, OOSLockError, blind_oos_window
from src.longhist import default_root

SEAL_START: date = blind_oos_window(TRACK_SWING)[0]
PURPOSES = ("performance", "coverage")
PIN_FILE = Path(__file__).resolve().parents[2] / "config" / "longhist_data_v1.json"

# symbol -> normalized CSV path relative to the data root
SERIES = {
    **{s: f"norm/yahoo/{s.replace('^', 'IDX_')}_daily.csv"
       for s in ("SPY", "QQQ", "^SOX", "SMH", "TQQQ", "SQQQ", "SOXL", "SOXS", "SPXL", "SPXS")},
    "VIX": "norm/cboe/VIX.csv",
    "VIX9D": "norm/cboe/VIX9D.csv",
    "VIX3M": "norm/cboe/VIX3M.csv",
    "DIX": "norm/squeezemetrics/DIX.csv",
}


class ManifestMismatch(RuntimeError):
    """File content does not match manifest.json (or the manifest is not the pinned one)."""


def pinned_manifest_sha256() -> str:
    return json.loads(PIN_FILE.read_text())["manifest_sha256"]


def _as_date(x) -> date | None:
    if x is None:
        return None
    if isinstance(x, datetime):  # includes pandas.Timestamp
        return x.date()
    if isinstance(x, date):
        return x
    if isinstance(x, str):
        return date.fromisoformat(x)
    raise TypeError(f"unsupported date value {x!r}")


def assert_performance_window(start, end) -> None:
    """Raise OOSLockError unless the inclusive range [start, end] ends strictly before SEAL_START."""
    try:
        s, e = _as_date(start), _as_date(end)
    except (TypeError, ValueError) as exc:
        raise OOSLockError(f"longhist performance read refused: unreadable range {start!r}..{end!r}") from exc
    if e is None or e >= SEAL_START or (s is not None and s >= SEAL_START):
        raise OOSLockError(
            f"longhist performance read refused: range {s}..{e} reaches the sealed period "
            f"(on/after {SEAL_START}). Use purpose='coverage' for date checks only.")


def read_verified(rel: str, root: Path | None = None, expected_manifest_sha256: str | None = None) -> bytes:
    """Bytes of `rel` after checking the manifest against the pinned SHA and the file against the manifest."""
    root = Path(root) if root else default_root()
    expected = expected_manifest_sha256 or pinned_manifest_sha256()
    mbytes = (root / "manifest.json").read_bytes()
    if hashlib.sha256(mbytes).hexdigest() != expected:
        raise ManifestMismatch("manifest.json SHA-256 differs from the pinned value — refusing to load")
    files = json.loads(mbytes)["files"]
    if rel not in files:
        raise ManifestMismatch(f"{rel} is not listed in manifest.json")
    data = (root / rel).read_bytes()
    if hashlib.sha256(data).hexdigest() != files[rel]["sha256"]:
        raise ManifestMismatch(f"{rel} SHA-256 differs from manifest.json — refusing to load")
    return data


def load_daily(symbol: str, start=None, end=None, purpose: str = "performance", *,
               root: Path | None = None, expected_manifest_sha256: str | None = None) -> pd.DataFrame:
    """Daily rows for `symbol` with start <= date <= end (inclusive; ISO strings, date or datetime).

    purpose="performance" (default, fail-closed) requires an explicit `end` before 2025-07-21 and returns
    all columns. purpose="coverage" returns the `date` column only.
    """
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {PURPOSES}, got {purpose!r}")
    if symbol not in SERIES:
        raise KeyError(f"unknown longhist series {symbol!r}; known: {sorted(SERIES)}")
    if purpose == "performance":
        assert_performance_window(start, end)
    s, e = _as_date(start), _as_date(end)
    data = read_verified(SERIES[symbol], root, expected_manifest_sha256)
    df = pd.read_csv(io.BytesIO(data), dtype={"date": str})
    if s is not None:
        df = df[df["date"] >= s.isoformat()]
    if e is not None:
        df = df[df["date"] <= e.isoformat()]
    if purpose == "coverage":
        return df[["date"]].reset_index(drop=True)
    if (df["date"] >= SEAL_START.isoformat()).any():  # defensive
        raise OOSLockError("sealed rows leaked into a performance read")
    return df.reset_index(drop=True)


AUX_SUFFIXES = ("_dividends.csv", "_splits.csv")
AUX_FILES = ("norm/calendar/nyse_sessions.csv",)


def load_aux(rel: str, *, root: Path | None = None, expected_manifest_sha256: str | None = None) -> pd.DataFrame:
    """Session calendar and dividend/split event files (dates, amounts, ratios — no prices), verified."""
    if rel not in AUX_FILES and not (rel.startswith("norm/yahoo/") and rel.endswith(AUX_SUFFIXES)):
        raise ValueError(f"load_aux reads only the session calendar and dividend/split files, not {rel!r}")
    return pd.read_csv(io.BytesIO(read_verified(rel, root, expected_manifest_sha256)), dtype={"date": str})
