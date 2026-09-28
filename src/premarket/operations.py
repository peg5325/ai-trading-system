"""Deterministic local runners for premarket evidence and post-close diagnostics.

The module orchestrates existing command-line contracts only.  It has no order,
broker, borrow, or promotion authority.  Every stage either reaches its immutable
Daily Brief or reports an explicit incomplete state; it never turns missing data
into a tradeable decision.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from src.history.exchange_calendar import is_trading_day
from src.models import ET
from src.premarket.report import json_bytes, write_immutable
from src.premarket.schedule import build_premarket_schedule, iso_z

OPERATION_SCHEMA = "premarket-local-operation/v1"
STAGES = ("PRIMARY", "VERIFICATION", "FREEZE")

CommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def trading_day_from_clock(now: datetime | None = None) -> date:
    current = now or datetime.now(ET)
    if current.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return current.astimezone(ET).date()


def stage_paths(root: Path, trading_day: date, stage: str) -> dict[str, Path]:
    normalized_stage = _stage(stage)
    stem = f"{trading_day.strftime('%Y%m%d')}.{normalized_stage.lower()}"
    reports = root / "reports"
    return {
        "provider": reports / f"premarket-provider-bundle.{stem}.json",
        "market": reports / f"premarket-market-bundle.{stem}.json",
        "input": reports / f"premarket-input.{stem}.json",
        "brief": reports / f"premarket-daily-brief.{stem}.json",
        "brief_markdown": reports / f"premarket-daily-brief.{stem}.md",
        "operation": reports / f"premarket-operation.{stem}.json",
        "market_raw": root / "data" / "premarket" / "market" / "raw" / stem,
    }


def postclose_paths(root: Path, trading_day: date) -> dict[str, Path]:
    date_key = trading_day.strftime("%Y%m%d")
    reports = root / "reports"
    return {
        "freeze": stage_paths(root, trading_day, "FREEZE")["brief"],
        "evaluation": reports / f"premarket-postclose.{date_key}.json",
        "evaluation_markdown": reports / f"premarket-postclose.{date_key}.md",
        "ledger": reports / "premarket-postclose-ledger.json",
        "cumulative": reports / "premarket-postclose-cumulative.json",
        "cumulative_markdown": reports / "premarket-postclose-cumulative.md",
        "operation": reports / f"premarket-operation.{date_key}.postclose.json",
        "raw_snapshots": root / "data" / "raw_snapshots" / f"premarket-postclose-{date_key}",
        "market_db": root / "data" / "market_history.sqlite",
    }


def run_stage(
    *,
    trading_day: date,
    stage: str,
    root: Path | None = None,
    runner: CommandRunner | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one premarket stage, continuing through canonical fail-closed artifacts."""
    root = (root or project_root()).resolve()
    stage = _stage(stage)
    if not is_trading_day(trading_day):
        return _skipped("NON_TRADING_DAY", trading_day, stage)
    paths = stage_paths(root, trading_day, stage)
    if paths["operation"].exists():
        return _load_operation(paths["operation"])
    schedule = build_premarket_schedule(trading_day)
    actual_now = now or datetime.now(ET)
    if actual_now.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    start, end = _stage_window(schedule, stage)
    if not start <= actual_now.astimezone(start.tzinfo) < end:
        return _skipped("STAGE_WINDOW_NOT_REACHED", trading_day, stage)
    run = runner or _subprocess_runner
    python = str(Path(sys.executable).resolve())
    commands = [
        (
            "provider",
            paths["provider"],
            [
                python, str(root / "scripts" / "collect_premarket_evidence.py"),
                "--trading-day", trading_day.isoformat(), "--stage", stage,
                "--out-json", str(paths["provider"]),
            ],
        ),
        (
            "market",
            paths["market"],
            [
                python, str(root / "scripts" / "collect_premarket_market.py"),
                "--provider-bundle", str(paths["provider"]),
                "--raw-root", str(paths["market_raw"]),
                "--out-json", str(paths["market"]),
            ],
        ),
        (
            "normalize",
            paths["input"],
            [
                python, str(root / "scripts" / "normalize_premarket_input.py"),
                "--provider-bundle", str(paths["provider"]),
                "--market-bundle", str(paths["market"]),
                "--out-json", str(paths["input"]),
            ],
        ),
        (
            "brief",
            (paths["brief"], paths["brief_markdown"]),
            [
                python, str(root / "scripts" / "premarket_daily_brief.py"),
                "--input", str(paths["input"]),
                "--out-json", str(paths["brief"]),
                "--out-markdown", str(paths["brief_markdown"]),
            ],
        ),
    ]
    steps = _run_commands(
        commands,
        root,
        run,
        log_dir=root / "reports" / "premarket-operation-logs" / f"{trading_day:%Y%m%d}.{stage.lower()}",
    )
    if not paths["brief"].exists() or not paths["brief_markdown"].exists():
        return _persist_incomplete(
            paths["operation"],
            "MISSING_DAILY_BRIEF",
            trading_day,
            stage,
            steps,
        )
    operation = _stage_operation(trading_day, stage, paths, steps)
    write_immutable(paths["operation"], json_bytes(operation))
    return operation


def run_postclose(
    *,
    trading_day: date,
    root: Path | None = None,
    runner: CommandRunner | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Refresh only FREEZE symbols plus SPY, then run the read-only evaluator."""
    root = (root or project_root()).resolve()
    if not is_trading_day(trading_day):
        return _skipped("NON_TRADING_DAY", trading_day, "POSTCLOSE")
    paths = postclose_paths(root, trading_day)
    if paths["operation"].exists():
        return _load_operation(paths["operation"])
    schedule = build_premarket_schedule(trading_day)
    earliest = datetime.fromisoformat(schedule.market_close_utc.replace("Z", "+00:00")) + timedelta(minutes=20)
    actual_now = now or datetime.now(ET)
    if actual_now.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    if actual_now.astimezone(earliest.tzinfo) < earliest:
        return _skipped("POSTCLOSE_WINDOW_NOT_REACHED", trading_day, "POSTCLOSE")
    if not paths["freeze"].exists():
        return _persist_incomplete(
            paths["operation"],
            "MISSING_FREEZE_BRIEF",
            trading_day,
            "POSTCLOSE",
            [],
        )
    freeze = _load_json(paths["freeze"])
    symbols = _freeze_symbols(freeze)
    as_of = iso_z(earliest)
    run = runner or _subprocess_runner
    python = str(Path(sys.executable).resolve())
    date_key = trading_day.strftime("%Y%m%d")
    commands = [
        (
            "market_tail_repair",
            None,
            [
                python, str(root / "scripts" / "history_backfill_official.py"),
                "--db", str(paths["market_db"]),
                "--symbols", ",".join(symbols), "--interval", "1m",
                "--completed-through", trading_day.isoformat(), "--tail-repair",
                "--sessions", "REGULAR", "--max-pages", "8", "--page-size", "200",
                "--spacing", "0.4", "--run-id", f"premarket-postclose-{date_key}",
                "--raw-snapshot-dir", str(paths["raw_snapshots"]), "--json",
            ],
        ),
        (
            "evaluate",
            (paths["evaluation"], paths["evaluation_markdown"]),
            [
                python, str(root / "scripts" / "evaluate_premarket_postclose.py"),
                "--freeze-brief", str(paths["freeze"]), "--market-db", str(paths["market_db"]),
                "--evaluation-as-of", as_of, "--out-json", str(paths["evaluation"]),
                "--out-markdown", str(paths["evaluation_markdown"]), "--ledger-json", str(paths["ledger"]),
                "--cumulative-json", str(paths["cumulative"]),
                "--cumulative-markdown", str(paths["cumulative_markdown"]),
            ],
        ),
    ]
    steps = _run_commands(
        commands,
        root,
        run,
        log_dir=root / "reports" / "premarket-operation-logs" / f"{trading_day:%Y%m%d}.postclose",
    )
    if not paths["evaluation"].exists():
        return _persist_incomplete(
            paths["operation"],
            "MISSING_POSTCLOSE_EVALUATION",
            trading_day,
            "POSTCLOSE",
            steps,
        )
    operation = {
        "schema": OPERATION_SCHEMA,
        "operation": "POSTCLOSE",
        "trading_day": trading_day.isoformat(),
        "status": "COMPLETE",
        "reason_codes": [],
        "freeze_artifact_sha256": freeze.get("artifact_sha256"),
        "evaluation_file_sha256": _file_sha256(paths["evaluation"]),
        "steps": steps,
    }
    operation["artifact_sha256"] = _payload_sha(operation)
    write_immutable(paths["operation"], json_bytes(operation))
    return operation


def _run_commands(
    commands: list[tuple[str, Path | tuple[Path, ...] | None, list[str]]],
    root: Path,
    runner: CommandRunner,
    *,
    log_dir: Path,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for name, expected, command in commands:
        expected_paths = _as_paths(expected)
        if expected_paths and all(path.exists() for path in expected_paths):
            results.append({"name": name, "status": "EXISTING", "returncode": None})
            continue
        completed = runner(command, root)
        step = {
            "name": name,
            "status": "EXECUTED",
            "returncode": completed.returncode,
        }
        step.update(_write_command_log(log_dir, name, command, completed, root))
        results.append(step)
        if expected_paths and not all(path.exists() for path in expected_paths):
            break
    return results


def _as_paths(expected: Path | tuple[Path, ...] | None) -> tuple[Path, ...]:
    if expected is None:
        return ()
    if isinstance(expected, Path):
        return (expected,)
    return expected


def _stage_window(schedule: Any, stage: str) -> tuple[datetime, datetime]:
    starts = {
        "PRIMARY": schedule.primary_run_utc,
        "VERIFICATION": schedule.verification_utc,
        "FREEZE": schedule.freeze_utc,
    }
    ends = {
        "PRIMARY": schedule.verification_utc,
        "VERIFICATION": schedule.freeze_utc,
        "FREEZE": schedule.market_open_utc,
    }
    return (
        datetime.fromisoformat(starts[stage].replace("Z", "+00:00")),
        datetime.fromisoformat(ends[stage].replace("Z", "+00:00")),
    )


_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b(authorization|api[_-]?key|token|secret|client[_-]?id)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|token|secret|client_id)=)[^&\s]+"
)


def _write_command_log(
    log_dir: Path,
    name: str,
    command: list[str],
    completed: subprocess.CompletedProcess[str],
    root: Path,
) -> dict[str, Any]:
    """Persist bounded, redacted subprocess evidence outside Git artifacts."""
    stdout = _sanitize_log(completed.stdout or "", root)
    stderr = _sanitize_log(completed.stderr or "", root)
    command_text = _sanitize_log(json.dumps(command, ensure_ascii=False), root)
    content = f"command={command_text}\nreturncode={completed.returncode}\n[stdout]\n{stdout}\n[stderr]\n{stderr}\n"
    log_dir.mkdir(parents=True, exist_ok=True)
    target = log_dir / f"{name}.log"
    _write_atomic(target, content.encode("utf-8"))
    evidence: dict[str, Any] = {
        "log_path": str(target.relative_to(root)),
        "log_sha256": _file_sha256(target),
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
    }
    if completed.returncode != 0:
        evidence["failure_tail"] = (stderr or stdout)[-1000:]
    return evidence


def _sanitize_log(value: str, root: Path) -> str:
    redacted = _ASSIGNMENT_SECRET.sub(r"\1=<REDACTED>", value)
    redacted = _QUERY_SECRET.sub(r"\1<REDACTED>", redacted)
    return redacted.replace(str(root), "<PROJECT_ROOT>")


def _write_atomic(path: Path, content: bytes) -> None:
    partial = path.with_name(f".{path.name}.{os.getpid()}.partial")
    try:
        with open(partial, "xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, path)
    finally:
        if partial.exists():
            partial.unlink()


def _stage_operation(
    trading_day: date,
    stage: str,
    paths: Mapping[str, Path],
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    artifacts = {
        name: _file_sha256(path)
        for name, path in paths.items()
        if name in {"provider", "market", "input", "brief", "brief_markdown"}
    }
    brief = _load_json(paths["brief"])
    operation = {
        "schema": OPERATION_SCHEMA,
        "operation": "PREMARKET_STAGE",
        "trading_day": trading_day.isoformat(),
        "stage": stage,
        "status": "COMPLETE",
        "reason_codes": list(brief.get("brief", {}).get("reason_codes", [])),
        "brief_status": brief.get("brief", {}).get("status"),
        "artifact_file_sha256": artifacts,
        "steps": steps,
    }
    operation["artifact_sha256"] = _payload_sha(operation)
    return operation


def _freeze_symbols(freeze: Mapping[str, Any]) -> list[str]:
    brief = freeze.get("brief")
    if not isinstance(brief, Mapping):
        raise TypeError("FREEZE brief must contain a brief object")
    symbols = {"SPY"}
    for item in brief.get("watchlist", []):
        if not isinstance(item, Mapping) or not isinstance(item.get("symbol"), str):
            raise TypeError("FREEZE watchlist contains an invalid symbol")
        symbols.add(item["symbol"].upper())
    return sorted(symbols)


def _subprocess_runner(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)


def _skipped(reason: str, trading_day: date, operation: str) -> dict[str, Any]:
    return {
        "schema": OPERATION_SCHEMA,
        "operation": operation,
        "trading_day": trading_day.isoformat(),
        "status": "SKIPPED",
        "reason_codes": [reason],
        "steps": [],
    }


def _incomplete(
    reason: str,
    trading_day: date,
    operation: str,
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": OPERATION_SCHEMA,
        "operation": operation,
        "trading_day": trading_day.isoformat(),
        "status": "INCOMPLETE",
        "reason_codes": [reason],
        "steps": steps,
    }


def _persist_incomplete(
    path: Path,
    reason: str,
    trading_day: date,
    operation: str,
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Write an immutable audit receipt once a due operation has failed."""
    result = _incomplete(reason, trading_day, operation, steps)
    result["artifact_sha256"] = _payload_sha(result)
    write_immutable(path, json_bytes(result))
    return result


def _load_operation(path: Path) -> dict[str, Any]:
    value = _load_json(path)
    if value.get("schema") != OPERATION_SCHEMA:
        raise ValueError("operation receipt schema mismatch")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON root must be an object: {path}")
    return value


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _payload_sha(value: Mapping[str, Any]) -> str:
    payload = dict(value)
    payload.pop("artifact_sha256", None)
    from src.premarket.contract import sha256_payload

    return sha256_payload(payload)


def _stage(value: str) -> str:
    normalized = value.upper()
    if normalized not in STAGES:
        raise ValueError(f"unsupported stage: {value}")
    return normalized
