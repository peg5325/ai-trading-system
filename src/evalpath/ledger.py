"""Run ledger (§7): append-only JSONL of reserve / complete / fail records that enforces an execution budget.

Budget unit (closeout 2026-10-19): ONE `run_account` call on real market data (`MARKET_UNIT`). A strategy run over
segments A and B is two units; every extra policy, cost model, stress or delay account is one more unit each.
Synthetic test calls are a separate resource (`SYNTHETIC_UNIT`) and never consume the market budget.

Enforcement:
- `expand_plan` turns runs x segments into account-call units; `preflight` refuses the whole plan before the first
  computation when the units exceed what is left of the authorization's budget, or reuse a reserved key.
- `AccountCallGuard` reserves each planned unit right before its `run_account` call and refuses an unplanned
  (hidden) call before it runs. Reservations are never released: failed, crashed or unfinished units stay counted,
  so a restart cannot regain budget, and a reserved key cannot be reused.
- The budget is read from the authorization, keyed by authorization id and resource; renaming a batch or a spec
  does not create budget. Reservations take an exclusive file lock (safe under concurrent callers);
  `single_run` refuses a second concurrent driver.

Standard library only: the repro compute step loads this file by path while it imports `src` from another tree.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

RESERVE, COMPLETE, FAIL = "reserve", "complete", "fail"
MARKET_UNIT = "market_data_run_account"
SYNTHETIC_UNIT = "synthetic_run_account"
LEGACY_UNIT = "legacy_run_arm"  # records written before 2026-10-19 carry no resource field


class BudgetExceeded(Exception):
    pass


class HiddenCall(Exception):
    """A run_account call that was not in the preflighted plan."""


class RunLocked(Exception):
    """Another driver holds the single-run lock."""


@contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            yield f
        finally:
            # flush before unlocking: otherwise a concurrent reader can take the lock before this write reaches the
            # file and reserve past the budget (found by the concurrency test, 2026-10-19)
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f, fcntl.LOCK_UN)


def _records(f) -> list[dict]:
    return [json.loads(line) for line in f.read().splitlines() if line.strip()]


def resource_of(rec: dict) -> str:
    return rec.get("resource", LEGACY_UNIT)


def used_units(records: list[dict], authorization_id: str, resource: str = MARKET_UNIT) -> int:
    """Units reserved under (`authorization_id`, `resource`); each reservation counts once, whatever happened next."""
    return sum(r["units"] for r in records if r["event"] == RESERVE and r["authorization_id"] == authorization_id
               and resource_of(r) == resource)


def reserved_keys(records: list[dict]) -> set[str]:
    return {r["run_key"] for r in records if r["event"] == RESERVE}


def reserve(path: Path, authorization_id: str, budget: int, run_key: str, units: int, detail: dict,
            resource: str = MARKET_UNIT) -> dict:
    if units <= 0:
        raise ValueError("units must be positive")
    with _locked(Path(path)) as f:
        recs = _records(f)
        if run_key in reserved_keys(recs):
            raise BudgetExceeded(f"run {run_key} was already reserved; a rerun needs a new run key and budget")
        used = used_units(recs, authorization_id, resource)
        if used + units > budget:
            raise BudgetExceeded(f"authorization {authorization_id} ({resource}): {used} + {units} > budget {budget}")
        rec = {"event": RESERVE, "time_utc": datetime.now(timezone.utc).isoformat(), "resource": resource,
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
               "resource": resource_of(res[0]), "authorization_id": res[0]["authorization_id"],
               "run_key": run_key, "units": 0, "detail": detail}
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        return rec


def read(path: Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with _locked(p) as f:
        return _records(f)


# ---------------------------------------------------------------- plan, preflight, per-call guard, single run


def unit_key(run_key: str, segment: str) -> str:
    return f"{run_key}/{segment}"


def expand_plan(runs: list[dict], segments: list[str]) -> list[dict]:
    """Smallest execution units: one entry per (run, segment account)."""
    return [{"unit_key": unit_key(r["run_key"], s), "run_key": r["run_key"], "segment": s,
             **{k: r[k] for k in ("policy", "cost_model_id", "delay") if k in r}}
            for r in runs for s in segments]


def preflight(path: Path, authorization_id: str, budget: int, units: list[dict],
              resource: str = MARKET_UNIT) -> dict:
    """Refuse the whole plan before any computation if it exceeds the remaining budget or reuses a key."""
    keys = [u["unit_key"] for u in units]
    if len(set(keys)) != len(keys):
        raise BudgetExceeded("plan repeats a unit key")
    recs = read(path)
    reused = sorted(set(keys) & reserved_keys(recs))
    if reused:
        raise BudgetExceeded(f"plan reuses reserved unit keys {reused}")
    used = used_units(recs, authorization_id, resource)
    if used + len(keys) > budget:
        raise BudgetExceeded(f"plan needs {len(keys)} {resource} units; {used} of {budget} already used "
                             f"under {authorization_id}; refused before any computation")
    return {"planned_units": len(keys), "used_before": used, "budget": budget, "resource": resource}


class AccountCallGuard:
    """Wrap each run_account call: refuse an unplanned unit, reserve the planned one right before the call, record
    complete/fail after. The reservation stays counted on failure (no budget comes back).

    By design `preflight` only reads the ledger and each unit is reserved separately right before its call: the
    budget is re-checked under the lock at every reservation, and concurrent drivers are excluded by `single_run`.
    A run cut off midway therefore leaves its earlier units spent and the later ones unreserved."""

    def __init__(self, path: Path, authorization_id: str, budget: int, planned_keys: list[str],
                 resource: str = MARKET_UNIT):
        self.path, self.authorization_id, self.budget = Path(path), authorization_id, budget
        self.planned = list(planned_keys)
        self.resource = resource
        self.done: list[str] = []

    def call(self, key: str, fn, *args, **kwargs):
        if key not in self.planned or key in self.done:
            raise HiddenCall(f"run_account {key!r} is not an open planned unit; refused before running")
        reserve(self.path, self.authorization_id, self.budget, key, 1, {"pid": os.getpid()}, self.resource)
        self.done.append(key)
        try:
            out = fn(*args, **kwargs)
        except BaseException as exc:
            try:
                finish(self.path, key, False, {"error": f"{type(exc).__name__}: {exc}"})
            except Exception as fin:  # noqa: BLE001 - keep the original error visible; the unit stays counted
                print(f"ledger finish failed for {key}: {fin!r}", file=sys.stderr)
            raise
        finish(self.path, key, True, {})
        return out


@contextmanager
def single_run(path: Path):
    """Non-blocking exclusive lock next to the ledger: a second concurrent driver is refused, not queued."""
    lock = Path(f"{path}.run.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+", encoding="utf-8") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunLocked(f"another run holds {lock}") from exc
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
