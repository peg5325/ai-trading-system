"""Run ledger (§7): append-only JSONL of reserve / complete / fail records that enforces an account_sim budget.

A unit is reserved before any computation starts and stays counted unless the same run later completes or fails
(both also count). A crash after a reservation therefore still uses the budget, so a restart cannot exceed it.
The budget comes from the authorization, never from the run spec. Writes take an exclusive file lock.
"""
from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

RESERVE, COMPLETE, FAIL = "reserve", "complete", "fail"


class BudgetExceeded(Exception):
    pass


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            yield f
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _records(f) -> list[dict]:
    return [json.loads(line) for line in f.read().splitlines() if line.strip()]


def used_units(records: list[dict], authorization_id: str) -> int:
    """Units reserved under `authorization_id` (each reservation counts once, whatever happened next)."""
    return sum(r["units"] for r in records if r["event"] == RESERVE and r["authorization_id"] == authorization_id)


def reserve(path: Path, authorization_id: str, budget: int, run_key: str, units: int, detail: dict) -> dict:
    if units <= 0:
        raise ValueError("units must be positive")
    with _locked(Path(path)) as f:
        recs = _records(f)
        if any(r["event"] == RESERVE and r["run_key"] == run_key for r in recs):
            raise BudgetExceeded(f"run {run_key} was already reserved; a rerun needs a new run key and budget")
        used = used_units(recs, authorization_id)
        if used + units > budget:
            raise BudgetExceeded(f"authorization {authorization_id}: {used} + {units} units > budget {budget}")
        rec = {"event": RESERVE, "time_utc": datetime.now(timezone.utc).isoformat(),
               "authorization_id": authorization_id, "run_key": run_key, "units": units,
               "used_after": used + units, "budget": budget, "detail": detail}
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        return rec


def finish(path: Path, run_key: str, ok: bool, detail: dict) -> dict:
    with _locked(Path(path)) as f:
        recs = _records(f)
        res = [r for r in recs if r["event"] == RESERVE and r["run_key"] == run_key]
        if not res:
            raise ValueError(f"run {run_key} has no reservation")
        if any(r["event"] in (COMPLETE, FAIL) and r["run_key"] == run_key for r in recs):
            raise ValueError(f"run {run_key} is already finished")
        rec = {"event": COMPLETE if ok else FAIL, "time_utc": datetime.now(timezone.utc).isoformat(),
               "authorization_id": res[0]["authorization_id"], "run_key": run_key, "units": 0, "detail": detail}
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        return rec


def read(path: Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with _locked(p) as f:
        return _records(f)
