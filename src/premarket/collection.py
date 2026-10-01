"""Daily US 1-minute history collection for the full P0-US30 universe.

The post-close diagnostic refreshes only the FREEZE symbols plus SPY, so most of the
universe stopped receiving minute bars after the 2026-07-21 research snapshot. This
runner re-reads the newest pages for every universe symbol through the official
backfill path (completed-day cutoff, immutable raw snapshots, first-write-wins
upsert) under the shared market-history lock, then records per-symbol REGULAR
coverage for the collected day. It is read-only market data: no orders, no broker
state, and no change to the paper contract inputs.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.history import exchange_calendar as cal
from src.history.completed_session import SCOPE_POST, last_completed_trading_day
from src.history.daily_manifest import read_p0_universe
from src.premarket.operations import (
    MARKET_DB_LOCK_TIMEOUT_SECONDS,
    OPERATION_SCHEMA,
    CommandRunner,
    MarketDbLockTimeout,
    _incomplete,
    _load_operation,
    _payload_sha,
    market_db_lock,
    project_root,
    run_commands,
    subprocess_runner,
)
from src.premarket.report import json_bytes, write_immutable

OPERATION = "HISTORY_COLLECTION"
UNIVERSE_YAML = Path("config") / "universe_p0_us30.yaml"
# About 4.6 pages of 200 PRE/REGULAR/POST bars per calendar day were observed on
# 2026-10-01, so 40 pages re-read roughly eight calendar days: one missed run or a
# long weekend is caught up by the next run without widening the tail.
DEFAULT_MAX_PAGES = 40


def collection_paths(root: Path, trading_day: date) -> dict[str, Path]:
    key = trading_day.strftime("%Y%m%d")
    return {
        "operation": root / "reports" / "history-collection" / f"{key}.operation.json",
        "logs": root / "reports" / "history-collection-logs" / key,
        "market_db": root / "data" / "market_history.sqlite",
        "raw_snapshots": root / "data" / "raw_snapshots",
    }


def run_history_collection(
    *,
    root: Path | None = None,
    runner: CommandRunner | None = None,
    now: datetime | None = None,
    lock_timeout_seconds: float = MARKET_DB_LOCK_TIMEOUT_SECONDS,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, Any]:
    """Collect the last NYSE day whose POST session has closed, once per day.

    Only a successful collection writes the immutable receipt; failures and lock
    timeouts stay retryable by the paired launchd trigger.
    """
    root = (root or project_root()).resolve()
    actual_now = now or datetime.now(timezone.utc)
    if actual_now.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    trading_day = last_completed_trading_day(
        actual_now.astimezone(timezone.utc), session_scope=SCOPE_POST)
    paths = collection_paths(root, trading_day)
    if paths["operation"].exists():
        return _load_operation(paths["operation"])
    universe = read_p0_universe(str(root / UNIVERSE_YAML))
    if not universe["valid"]:
        return _incomplete("INVALID_UNIVERSE", trading_day, OPERATION, [])
    symbols = list(universe["symbols"])
    command = [
        sys.executable, str(root / "scripts" / "history_backfill_official.py"),
        "--db", str(paths["market_db"]),
        "--symbols", ",".join(symbols), "--interval", "1m",
        "--completed-through", trading_day.isoformat(), "--tail-repair",
        "--max-pages", str(max_pages), "--page-size", "200", "--spacing", "0.4",
        "--gap-skip-minutes", "200", "--max-consecutive-empty", "60",
        "--run-id", f"history-collection-{trading_day:%Y%m%d}",
        "--raw-snapshot-dir", str(paths["raw_snapshots"]), "--json",
    ]
    run = runner or subprocess_runner
    try:
        with market_db_lock(root, timeout_seconds=lock_timeout_seconds):
            steps = run_commands(
                [("history_tail_repair", None, command)], root, run, log_dir=paths["logs"])
    except MarketDbLockTimeout:
        return _incomplete("LOCK_TIMEOUT", trading_day, OPERATION, [])
    if not steps or steps[-1].get("returncode") != 0:
        return _incomplete("BACKFILL_FAILED", trading_day, OPERATION, steps)
    coverage = regular_coverage(paths["market_db"], symbols, trading_day)
    operation = {
        "schema": OPERATION_SCHEMA,
        "operation": OPERATION,
        "trading_day": trading_day.isoformat(),
        "status": "COMPLETE",
        "reason_codes": [],
        "max_pages": max_pages,
        "symbols": symbols,
        "expected_regular_minutes": cal.expected_regular_minutes(trading_day),
        "regular_coverage": coverage,
        "incomplete_symbols": sorted(
            symbol for symbol, minutes in coverage.items()
            if minutes < cal.expected_regular_minutes(trading_day)),
        "steps": steps,
    }
    operation["artifact_sha256"] = _payload_sha(operation)
    write_immutable(paths["operation"], json_bytes(operation))
    return operation


def regular_coverage(db_path: Path, symbols: list[str], trading_day: date) -> dict[str, int]:
    """REGULAR 1m bar count per symbol for one exchange-local day (read-only)."""
    counts = dict.fromkeys(symbols, 0)
    if not db_path.exists():
        return counts
    # The ts_utc bounds keep the scan on the (symbol, interval, ts_utc) primary key;
    # the regular session always falls inside [day 00:00Z, next day 00:00Z).
    start = f"{trading_day.isoformat()}T00:00"
    end = f"{date.fromordinal(trading_day.toordinal() + 1).isoformat()}T00:00"
    con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT symbol, COUNT(*) FROM candle_history "
            "WHERE interval='1m' AND session='REGULAR' AND ts_utc>=? AND ts_utc<? "
            "AND substr(ts_exchange,1,10)=? "
            f"AND symbol IN ({','.join('?' * len(symbols))}) GROUP BY symbol",
            (start, end, trading_day.isoformat(), *symbols),
        ).fetchall()
    finally:
        con.close()
    counts.update({symbol: int(n) for symbol, n in rows})
    return counts
