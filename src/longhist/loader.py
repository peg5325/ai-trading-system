"""Guarded loader for the long-history daily data set.

- Every file read is verified against its SHA-256 in manifest.json; a mismatch or an unlisted file raises.
- purpose="performance" refuses any request whose range reaches on/after the swing blind-OOS start
  (2025-07-21, `src.history.oos_guard.blind_oos_window("swing")`), and an open-ended end. This is
  stricter than `assert_window_unlocked` (which would let ranges after 2026-07-21 through) on purpose:
  the next research stage seals everything from 2025-07-21 on.
- purpose="coverage" reads any range, for date-existence / schema / quality checks only (no returns).
"""
from __future__ import annotations

import hashlib
import io
import json
from datetime import date
from pathlib import Path

import pandas as pd

from src.history.oos_guard import TRACK_SWING, OOSLockError, blind_oos_window
from src.longhist import default_root

SEAL_START: date = blind_oos_window(TRACK_SWING)[0]
PURPOSES = ("performance", "coverage")

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
    """File content does not match manifest.json (or the file/manifest is not the pinned one)."""


def _as_date(x) -> date | None:
    if x is None:
        return None
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x))


def assert_performance_window(start, end) -> None:
    """Raise OOSLockError unless the inclusive range [start, end] ends strictly before SEAL_START."""
    s, e = _as_date(start), _as_date(end)
    if e is None or e >= SEAL_START or (s is not None and s >= SEAL_START):
        raise OOSLockError(
            f"longhist performance read refused: range {s}..{e} reaches the sealed period "
            f"(on/after {SEAL_START}). Use purpose='coverage' for date/schema checks only.")


def read_verified(rel: str, root: Path | None = None, expected_manifest_sha256: str | None = None) -> bytes:
    """Return the bytes of `rel` after checking them against manifest.json (and the manifest's own SHA)."""
    root = Path(root) if root else default_root()
    mbytes = (root / "manifest.json").read_bytes()
    if expected_manifest_sha256 and hashlib.sha256(mbytes).hexdigest() != expected_manifest_sha256:
        raise ManifestMismatch("manifest.json SHA-256 differs from the expected value")
    files = json.loads(mbytes)["files"]
    if rel not in files:
        raise ManifestMismatch(f"{rel} is not listed in manifest.json")
    data = (root / rel).read_bytes()
    if hashlib.sha256(data).hexdigest() != files[rel]["sha256"]:
        raise ManifestMismatch(f"{rel} SHA-256 differs from manifest.json — refusing to load")
    return data


def load_daily(symbol: str, start=None, end=None, purpose: str = "performance", *,
               root: Path | None = None, expected_manifest_sha256: str | None = None) -> pd.DataFrame:
    """Daily rows for `symbol` with start <= date <= end (inclusive ISO dates or date objects).

    purpose="performance" (default, fail-closed) requires an explicit `end` before 2025-07-21.
    """
    if purpose not in PURPOSES:
        raise ValueError(f"purpose must be one of {PURPOSES}, got {purpose!r}")
    if symbol not in SERIES:
        raise KeyError(f"unknown longhist series {symbol!r}; known: {sorted(SERIES)}")
    if purpose == "performance":
        assert_performance_window(start, end)
    data = read_verified(SERIES[symbol], root, expected_manifest_sha256)
    df = pd.read_csv(io.BytesIO(data), dtype={"date": str})
    s, e = _as_date(start), _as_date(end)
    if s is not None:
        df = df[df["date"] >= s.isoformat()]
    if e is not None:
        df = df[df["date"] <= e.isoformat()]
    if purpose == "performance" and (df["date"] >= SEAL_START.isoformat()).any():  # defensive
        raise OOSLockError("sealed rows leaked into a performance read")
    return df.reset_index(drop=True)


def load_aux(rel: str, purpose: str = "coverage", *, root: Path | None = None,
             expected_manifest_sha256: str | None = None) -> pd.DataFrame:
    """Dividend/split/calendar CSVs (event dates and amounts, no prices) — verified, any range."""
    if purpose != "coverage":
        raise ValueError("load_aux is coverage-only")
    return pd.read_csv(io.BytesIO(read_verified(rel, root, expected_manifest_sha256)), dtype={"date": str})
