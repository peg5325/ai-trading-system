"""Raw download (no computation). Writes files plus a SHA-256 manifest."""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import contract as C

UA = {"User-Agent": "Mozilla/5.0"}
MANIFEST = "manifest.json"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def yahoo_url(symbol: str) -> str:
    # Yahoo only: period2 is the day after DATA_END so those files hold no blind-OOS rows.
    # Cboe and French files do contain later rows; the loaders drop them right after parsing.
    nxt = C.DATA_END + timedelta(days=1)
    p2 = int(datetime(nxt.year, nxt.month, nxt.day, tzinfo=timezone.utc).timestamp())
    return (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
        f"?period1=0&period2={p2}&interval=1d&events=div%2Csplit&includeAdjustedClose=true"
    )


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_all(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = {f"yahoo_{s.replace('^', 'IDX_')}.json": yahoo_url(s) for s in C.YAHOO_SYMBOLS}
    targets["VIX_History.csv"] = C.CBOE_URL
    targets["ff_daily.zip"] = C.FRENCH_URL
    files = {}
    for name, url in targets.items():
        (out_dir / name).write_bytes(_get(url))
        files[name] = {"url": url, "sha256": sha256_file(out_dir / name),
                       "bytes": (out_dir / name).stat().st_size}
        time.sleep(1)
    manifest = {"fetched_utc": datetime.now(timezone.utc).isoformat(), "files": files}
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def raw_sha256(raw_dir: Path) -> dict[str, str]:
    """SHA-256 of every raw file present, recomputed (not trusted from the manifest)."""
    names = [f"yahoo_{s.replace('^', 'IDX_')}.json" for s in C.YAHOO_SYMBOLS]
    names += ["VIX_History.csv", "ff_daily.zip"]
    return {n: sha256_file(raw_dir / n) for n in names}
