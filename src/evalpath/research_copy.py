"""Research data copy (P3): an allowlisted, read-only SQLite extract of `candle_history` plus a manifest.

Two steps are kept apart. The preparation step (`build_copy`) is the only code that opens the source market DB, and
it reads only the allowlisted symbols, bar interval and ET date ranges; an allowlist that touches a sealed blind OOS
block is refused before the source is opened. The research step (`verify_copy`, `assert_covered`) gets the copy
path and its manifest only, checks the copy's SHA-256 against the manifest and refuses requests outside the
allowlist.

What this does not do: a hash proves identity, not isolation. A process running as the same user can still open
the source DB or chmod the copy; that boundary has to come from how the research process is launched.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from datetime import date, datetime, timezone
from pathlib import Path

from src.history.oos_guard import TRACK_INTRADAY, TRACK_SWING, overlaps_blind_oos
from src.history.store import SCHEMA
from src.models import ET

ALLOWLIST_SCHEMA = "research_allowlist/v1"
MANIFEST_SCHEMA = "research_copy_manifest/v1"
COLUMNS = ("symbol", "market", "interval", "ts_utc", "ts_exchange", "session", "open", "high", "low", "close",
           "volume", "currency", "adjusted", "source", "created_at", "updated_at")
TRACK_BY_INTERVAL = {"1m": TRACK_INTRADAY, "1d": TRACK_SWING}


class CopyRefused(Exception):
    """The allowlist, the copy or a request is outside what the copy may contain."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _et(d: str) -> datetime:
    x = date.fromisoformat(d)
    return datetime(x.year, x.month, x.day, tzinfo=ET)


def _utc_text(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def validate_allowlist(al: dict) -> dict:
    """Refuse a malformed allowlist or one whose ranges touch the sealed block of its interval's track."""
    if al.get("schema") != ALLOWLIST_SCHEMA:
        raise CopyRefused(f"allowlist schema {al.get('schema')!r} != {ALLOWLIST_SCHEMA}")
    for key in ("id", "interval", "symbols", "ranges", "approved_by", "approved_ref"):
        if not al.get(key):
            raise CopyRefused(f"allowlist field {key!r} is required")
    track = TRACK_BY_INTERVAL.get(al["interval"])
    if track is None:
        raise CopyRefused(f"interval {al['interval']!r} not in {sorted(TRACK_BY_INTERVAL)}")
    for r in al["ranges"]:
        lo, hi = _et(r["start"]), _et(r["end_exclusive"])
        if not lo < hi:
            raise CopyRefused(f"empty or reversed range {r}")
        for t in (TRACK_INTRADAY, TRACK_SWING) if track == TRACK_SWING else (track,):
            if overlaps_blind_oos(lo, hi, t):
                raise CopyRefused(f"range {r['start']}..{r['end_exclusive']} touches the sealed {t} block")
    return al


def other_track_overlaps(al: dict) -> list[dict]:
    """Ranges of a 1m allowlist that fall inside the swing/daily sealed block. Allowed for intraday research (the
    tracks are separate), recorded so the contact history is kept and no independence is claimed for it."""
    if al["interval"] != "1m":
        return []
    return [{"range": r, "track": TRACK_SWING,
             "note": "1m intraday bars on dates inside the swing/daily seal; not a daily-track performance read"}
            for r in al["ranges"] if overlaps_blind_oos(_et(r["start"]), _et(r["end_exclusive"]), TRACK_SWING)]


def allowlist_sha256(al: dict) -> str:
    return hashlib.sha256(json.dumps(al, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def build_copy(source_db: Path, allowlist: dict, out_db: Path) -> dict:
    """Write the allowlisted rows to a new DB, make it read-only and write `<out_db>.manifest.json` (write-once)."""
    al = validate_allowlist(allowlist)
    out_db = Path(out_db)
    man_path = manifest_path(out_db)
    for p in (out_db, man_path):
        if p.exists():
            raise CopyRefused(f"{p} already exists; copies are write-once")
    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    dst = sqlite3.connect(out_db)
    counts: dict[str, int] = {}
    hashes: dict[str, str] = {}
    try:
        dst.executescript(SCHEMA)
        cols = ",".join(COLUMNS)
        q = (f"SELECT {cols} FROM candle_history WHERE symbol=? AND interval=? AND ts_utc>=? AND ts_utc<? "
             "ORDER BY ts_utc")
        for sym in al["symbols"]:
            h = hashlib.sha256()
            n = 0
            for r in al["ranges"]:
                rows = src.execute(q, (sym, al["interval"], _utc_text(_et(r["start"])),
                                       _utc_text(_et(r["end_exclusive"])))).fetchall()
                for row in rows:
                    h.update(("|".join(repr(v) for v in row) + "\n").encode())
                n += len(rows)
                dst.executemany(f"INSERT INTO candle_history ({cols}) VALUES ({','.join('?' * len(COLUMNS))})",
                                rows)
            counts[sym], hashes[sym] = n, h.hexdigest()
        dst.commit()
    finally:
        src.close()
        dst.close()
    os.chmod(out_db, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    st = Path(source_db).stat()
    man = {"schema": MANIFEST_SCHEMA, "allowlist": al, "allowlist_sha256": allowlist_sha256(al),
           "copy_file": out_db.name, "copy_sha256": sha256_file(out_db), "row_counts": counts,
           "rows_sha256": hashes, "row_hash_columns": list(COLUMNS),
           "other_track_seal_overlaps": other_track_overlaps(al),
           "source": {"path": str(source_db), "size": st.st_size,
                      "mtime_utc": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                      "note": "source identified by path, size and mtime; not hashed"},
           "created_utc": datetime.now(timezone.utc).isoformat(),
           "isolation": "identity only: the hash does not stop a same-user process from opening the source DB"}
    with open(man_path, "x", encoding="utf-8") as f:
        f.write(json.dumps(man, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    return man


def manifest_path(copy_db: Path) -> Path:
    return Path(f"{copy_db}.manifest.json")


def verify_copy(copy_db: Path, expected_copy_sha256: str | None = None) -> dict:
    """Return the manifest after checking the copy against it (and against an expected SHA, if given)."""
    copy_db = Path(copy_db)
    mp = manifest_path(copy_db)
    if not mp.exists():
        raise CopyRefused(f"{copy_db} has no manifest {mp.name}; only research copies are accepted")
    man = json.loads(mp.read_text(encoding="utf-8"))
    if man.get("schema") != MANIFEST_SCHEMA:
        raise CopyRefused(f"manifest schema {man.get('schema')!r} != {MANIFEST_SCHEMA}")
    validate_allowlist(man["allowlist"])
    if allowlist_sha256(man["allowlist"]) != man["allowlist_sha256"]:
        raise CopyRefused("manifest allowlist does not match its recorded hash")
    sha = sha256_file(copy_db)
    if sha != man["copy_sha256"]:
        raise CopyRefused(f"copy SHA-256 {sha} != manifest {man['copy_sha256']}")
    if expected_copy_sha256 is not None and sha != expected_copy_sha256:
        raise CopyRefused(f"copy SHA-256 {sha} != expected {expected_copy_sha256}")
    return man


def assert_covered(man: dict, symbol: str, interval: str, start: datetime | None, end: datetime | None) -> None:
    """Refuse a [start, end) request the copy's allowlist does not cover (unbounded requests are refused)."""
    al = man["allowlist"]
    if symbol not in al["symbols"] or interval != al["interval"]:
        raise CopyRefused(f"{symbol} {interval} is not in allowlist {al['id']}")
    if start is None or end is None:
        raise CopyRefused("unbounded request; give both ends")
    for r in al["ranges"]:
        if _et(r["start"]) <= start and end <= _et(r["end_exclusive"]):
            return
    raise CopyRefused(f"[{start.isoformat()}, {end.isoformat()}) is outside allowlist {al['id']}")
