"""Post-close runner for one scheduled FREEZE Paper session (issue #165 PR 2).

Order of checks (fail closed, never skips a scheduled session):

1. NYSE non-trading day -> SKIPPED (nothing written)
2. activation: a valid first-session receipt that binds the current contract JSON/MD
   and Daily Brief config, and lists this day -> otherwise NOT_ACTIVATED
3. regular close + 20 min (same guard as the post-close evaluator) -> otherwise SKIPPED
4. strict order: the previous scheduled session artifact must exist and verify
   (signature, day, index, receipt); an already recorded, verified session returns
   COMPLETE before any backfill or network call
5. FREEZE gates bound to the pinned activation config, official 1m tail-repair backfill
   (subprocess) and OOS-guarded bar load under the shared market-history lock
6. engine minute by minute, reconciliation with a mid-session restart check, immutable
   session artifact, verified ledger fold

Only the operation receipt records wall-clock time and the git commit, and a COMPLETE
receipt is never replaced. The session artifact is a pure function of its inputs;
``recompute`` compares it with the current DB and writes drift to a separate file.
"""
from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.freeze_paper.binding import ContractBinding, load_binding
from src.freeze_paper.costs import derived_cost_drags
from src.freeze_paper.engine import (
    COMPLETE,
    finish_session,
    new_account_state,
    start_session,
    state_bytes,
    step_bar,
)
from src.freeze_paper.gates import NO_TRADE as NO_TRADE_GATE
from src.freeze_paper.gates import TRADE, GateResult, evaluate_gates, freeze_paths
from src.freeze_paper.receipt import (
    CONFIG_RELATIVE,
    CONTRACT_JSON_RELATIVE,
    GitRunner,
    ReceiptError,
    code_commit_defect,
    file_sha256,
    load_receipt,
)
from src.freeze_paper.reconcile import reconcile_state
from src.history.exchange_calendar import is_trading_day
from src.history.oos_guard import TRACK_INTRADAY, research_load
from src.models import ET
from src.premarket.contract import load_config, sha256_payload
from src.premarket.operations import (
    MARKET_DB_LOCK_TIMEOUT_SECONDS,
    OPERATION_SCHEMA,
    MarketDbLockTimeout,
    market_db_lock,
    postclose_earliest,
    project_root,
    run_commands,
    subprocess_runner,
    write_atomic,
)
from src.premarket.report import json_bytes, write_immutable, write_immutable_pair
from src.premarket.schedule import build_premarket_schedule, iso_z, parse_iso
from src.runtime.isolation.identity import producer_commit

SESSION_SCHEMA = "premarket-freeze-paper/session/v2"   # v2: end_state.session.equity_curve
LEDGER_SCHEMA = "premarket-freeze-paper/ledger/v1"
DRIFT_SCHEMA = "premarket-freeze-paper/drift/v1"
ALERT_SCHEMA = "premarket-freeze-paper/alert/v1"
SYMBOL_PATTERN = re.compile(r"^[A-Z.]{1,10}$")
OPERATION = "FREEZE_PAPER"

# runner statuses
COMPLETE_STATUS = "COMPLETE"
SKIPPED = "SKIPPED"
NOT_ACTIVATED = "NOT_ACTIVATED"
INCOMPLETE = "INCOMPLETE"
DATA_DRIFT = "DATA_DRIFT"
DRY_RUN = "DRY_RUN"
INVALID = "INVALID"          # ledger row: artifact failed verification

CommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]
BarLoader = Callable[[Path, str, datetime, datetime], list]


def paper_paths(root: Path, trading_day: date) -> dict[str, Path]:
    key = trading_day.strftime("%Y%m%d")
    reports = root / "reports"
    return {
        "session": reports / f"premarket-freeze-paper.{key}.json",
        "session_markdown": reports / f"premarket-freeze-paper.{key}.md",
        "drift": reports / f"premarket-freeze-paper.{key}.drift.json",
        "alert": reports / f"premarket-freeze-paper.{key}.alert.json",
        "ledger": reports / "premarket-freeze-paper-ledger.json",
        "operation": reports / f"premarket-operation.{key}.paper.json",
        "market_db": root / "data" / "market_history.sqlite",
        "raw_snapshots": root / "data" / "raw_snapshots" / f"premarket-freeze-paper-{key}",
        "logs": reports / "premarket-operation-logs" / f"{key}.paper",
    }


def oos_guarded_bars(db: Path, symbol: str, start: datetime, end: datetime) -> list:
    """REGULAR 1m bars in [start, end) through the blind-OOS guarded loader."""
    return research_load(str(db), symbol, "1m", start=start, end=end, purpose="performance",
                         track=TRACK_INTRADAY, market="US")


def run_paper_session(
    *,
    trading_day: date,
    root: Path | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    recompute: bool = False,
    runner: CommandRunner | None = None,
    bar_loader: BarLoader | None = None,
    git_runner: GitRunner | None = None,
    lock_timeout_seconds: float = MARKET_DB_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run or verify one scheduled session. ``recompute`` re-derives an existing session
    from the current DB without backfill and reports drift in a separate file only."""
    root = root or project_root()
    paths = paper_paths(root, trading_day)
    if not is_trading_day(trading_day):
        return _result(SKIPPED, trading_day, ["NON_TRADING_DAY"])
    writes_receipt = not dry_run and not recompute

    def finish(status: str, codes: list[str], **extra: Any) -> dict[str, Any]:
        result = _result(status, trading_day, codes, **extra)
        if not writes_receipt:
            return result
        existing = _read_json(paths["operation"])
        if existing is not None and existing.get("status") == COMPLETE_STATUS:
            result["operation_receipt_kept"] = True     # a COMPLETE receipt is never replaced
            return result
        result["run_at"] = (now or datetime.now(ET)).isoformat()
        result["git_commit"] = producer_commit(cwd=str(root))
        result["artifact_sha256"] = sha256_payload(
            {k: v for k, v in result.items() if k != "artifact_sha256"})
        paths["operation"].parent.mkdir(parents=True, exist_ok=True)
        write_atomic(paths["operation"], json_bytes(result))
        return result

    try:
        receipt = load_receipt(root)
    except ReceiptError as exc:
        return finish(NOT_ACTIVATED, [str(exc)])
    commit_defect = code_commit_defect(receipt, root, git_runner)
    if commit_defect is not None:
        return finish(NOT_ACTIVATED, [commit_defect])
    sessions = [date.fromisoformat(d) for d in receipt["scheduled_sessions"]]
    if trading_day not in sessions:
        return finish(NOT_ACTIVATED, ["NOT_A_SCHEDULED_SESSION"])

    schedule = build_premarket_schedule(trading_day)
    actual_now = now or datetime.now(ET)
    if actual_now.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    if actual_now < postclose_earliest(schedule):
        return _result(SKIPPED, trading_day, ["PAPER_WINDOW_NOT_REACHED"])
    try:
        return _run_scheduled(
            trading_day=trading_day, root=root, paths=paths, receipt=receipt, sessions=sessions,
            schedule=schedule, finish=finish, dry_run=dry_run, recompute=recompute,
            runner=runner or subprocess_runner, loader=bar_loader or oos_guarded_bars,
            lock_timeout_seconds=lock_timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - any failure is a retryable INCOMPLETE; type name only
        return finish(INCOMPLETE, [f"EXCEPTION:{type(exc).__name__}"])


def _run_scheduled(*, trading_day: date, root: Path, paths: Mapping[str, Path],
                   receipt: Mapping, sessions: list[date], schedule: Any,
                   finish: Callable[..., dict[str, Any]], dry_run: bool, recompute: bool,
                   runner: CommandRunner, loader: BarLoader,
                   lock_timeout_seconds: float) -> dict[str, Any]:
    binding = load_binding(root / CONTRACT_JSON_RELATIVE)
    receipt_sha = receipt["artifact_sha256"]
    index = sessions.index(trading_day)
    prev_artifact = None
    if index > 0:
        prev_day = sessions[index - 1]
        prev_artifact, defect = _load_verified(root, prev_day, binding, receipt_sha, index - 1)
        if defect is not None:
            return finish(INCOMPLETE, [defect], previous_session=prev_day.isoformat())
    prev_sha = prev_artifact["artifact_sha256"] if prev_artifact else None

    if paths["session"].exists() and not recompute:
        existing, defect = _load_verified(root, trading_day, binding, receipt_sha, index, prev_sha)
        if defect is not None:
            codes = [f"EXISTING_{defect}"]
            if not dry_run:
                # The (possibly COMPLETE) receipt is kept; the alert makes the break visible.
                _write_alert(paths, trading_day, codes)
            return finish(INCOMPLETE, codes)
        if dry_run:
            return _result(DRY_RUN, trading_day, ["SESSION_ALREADY_RECORDED"], session=existing)
        codes = ["SESSION_ALREADY_RECORDED"]
        markdown = render_markdown(existing).encode("utf-8")
        if not paths["session_markdown"].exists():      # derived view, rebuilt deterministically
            write_immutable(paths["session_markdown"], markdown)
        elif paths["session_markdown"].read_bytes() != markdown:
            codes.append("EXISTING_MARKDOWN_DIFFERS")    # never overwritten; alert only
            _write_alert(paths, trading_day, ["EXISTING_MARKDOWN_DIFFERS"])
        write_atomic(paths["ledger"], json_bytes(
            build_ledger(root, sessions, through=trading_day, receipt_sha256=receipt_sha,
                         contract_sha256=binding.sha256)))
        return finish(COMPLETE_STATUS, codes, session_status=existing["status"],
                      session_artifact_sha256=existing["artifact_sha256"])
    if recompute and not paths["session"].exists():
        return _result(INCOMPLETE, trading_day, ["NOTHING_TO_RECOMPUTE"])

    start_state = prev_artifact["end_state"] if prev_artifact else new_account_state(binding)
    gate = bind_policy_config(evaluate_gates(trading_day, root / "reports", binding), root)
    symbols = sorted(set(_gate_symbols(gate, start_state)) | set(start_state["positions"]))
    if any(not SYMBOL_PATTERN.match(sym) for sym in symbols):
        return finish(INCOMPLETE, ["INVALID_SYMBOL"])
    open_utc = parse_iso(schedule.market_open_utc)
    close_utc = parse_iso(schedule.market_close_utc)
    steps: list[dict[str, Any]] = []
    bars_by_symbol: dict[str, list] = {}
    spy_step: dict[str, Any] | None = None
    capture_spy = not dry_run and not recompute
    if symbols or capture_spy:
        try:
            with _maybe_lock(root, not dry_run, lock_timeout_seconds):
                if capture_spy:
                    # Separate, non-fatal SPY close capture for the evaluator's benchmark. It
                    # never feeds the engine, so the session artifact does not depend on it.
                    spy_steps = _backfill(root, paths, trading_day, ["SPY"], runner,
                                          step="spy_tail_repair", run_suffix="spy")
                    ok = bool(spy_steps) and spy_steps[-1].get("returncode") == 0
                    spy_step = {"status": "OK" if ok else "SPY_BACKFILL_FAILED",
                                "steps": spy_steps}
                if symbols and capture_spy:
                    steps = _backfill(root, paths, trading_day, symbols, runner)
                    if not steps or steps[-1].get("returncode") != 0:
                        return finish(INCOMPLETE, ["BACKFILL_FAILED"], steps=steps,
                                      spy_step=spy_step)
                bars_by_symbol = {sym: _regular_bars(loader(paths["market_db"], sym, open_utc,
                                                            close_utc), open_utc, close_utc)
                                  for sym in symbols}
        except MarketDbLockTimeout:
            return finish(INCOMPLETE, ["LOCK_TIMEOUT"])
    artifact = build_session_artifact(
        trading_day=trading_day, index=index, binding=binding, receipt=receipt, gate=gate,
        start_state=start_state, bars_by_symbol=bars_by_symbol, prev_sha=prev_sha, root=root)
    if dry_run:
        return _result(DRY_RUN, trading_day, [], session=artifact)
    if recompute:
        return _report_drift(paths, trading_day, artifact)

    content = json_bytes(artifact)
    markdown = render_markdown(artifact).encode("utf-8")
    for path in (paths["session"], paths["session_markdown"]):
        partial = Path(str(path) + ".partial")
        if partial.exists():
            return finish(INCOMPLETE, ["STALE_PARTIAL"], stale_partial=partial.name, steps=steps)
    write_immutable_pair((paths["session"], content), (paths["session_markdown"], markdown))
    write_atomic(paths["ledger"], json_bytes(
        build_ledger(root, sessions, through=trading_day, receipt_sha256=receipt_sha,
                     contract_sha256=binding.sha256)))
    return finish(COMPLETE_STATUS, [], session_status=artifact["status"],
                  session_artifact_sha256=artifact["artifact_sha256"],
                  reconciliation_ok=artifact["reconciliation"]["ok"], steps=steps,
                  spy_step=spy_step)


def _write_alert(paths: Mapping[str, Path], trading_day: date, codes: list[str]) -> None:
    alert = {"schema": ALERT_SCHEMA, "trading_day": trading_day.isoformat(),
             "reason_codes": codes}
    alert["artifact_sha256"] = sha256_payload(alert)
    write_atomic(paths["alert"], json_bytes(alert))


def _regular_bars(bars: list, open_utc: datetime, close_utc: datetime) -> list:
    """Candle.ts_utc is the bar START; the session keeps bars with open <= start < close,
    so the 09:35 ET entry bar is ts 13:35Z in EDT and the bar starting at the close is dropped."""
    return [bar for bar in bars if open_utc <= bar.ts_utc < close_utc]


@contextmanager
def _maybe_lock(root: Path, enabled: bool, timeout_seconds: float) -> Iterator[None]:
    if not enabled:
        yield
        return
    with market_db_lock(root, timeout_seconds=timeout_seconds):
        yield


def bind_policy_config(gate: GateResult, root: Path) -> GateResult:
    """NO_TRADE unless the FREEZE snapshot was built with the activation config the
    receipt pins (compared as sha256_payload of PremarketConfig.to_dict())."""
    brief_path, _ = freeze_paths(date.fromisoformat(gate.trading_day), root / "reports")
    brief = _read_json(brief_path)
    if brief is None:
        return gate
    try:
        snapshot_policy = brief["snapshot"]["payload"]["policy_config"]
    except (KeyError, TypeError):
        snapshot_policy = None
    expected = sha256_payload(load_config(root / CONFIG_RELATIVE).to_dict())
    if snapshot_policy is not None and sha256_payload(snapshot_policy) == expected:
        return gate
    return dataclasses.replace(
        gate, decision=NO_TRADE_GATE, leg=None, candidates=(),
        reason_codes=tuple(sorted(set(gate.reason_codes) | {"POLICY_CONFIG_MISMATCH"})))


def _report_drift(paths: Mapping[str, Path], trading_day: date,
                  artifact: Mapping) -> dict[str, Any]:
    existing = _read_json(paths["session"]) or {}
    if json_bytes(artifact) == paths["session"].read_bytes():
        return _result(COMPLETE_STATUS, trading_day, ["RECOMPUTE_IDENTICAL"])
    drift = {
        "schema": DRIFT_SCHEMA,
        "trading_day": trading_day.isoformat(),
        "existing_artifact_sha256": existing.get("artifact_sha256"),
        "recomputed_artifact_sha256": artifact["artifact_sha256"],
        "differing_keys": sorted(k for k in set(existing) | set(artifact)
                                 if k != "artifact_sha256" and existing.get(k) != artifact.get(k)),
        "recomputed_inputs": artifact["inputs"],
    }
    drift["artifact_sha256"] = sha256_payload(drift)
    write_atomic(paths["drift"], json_bytes(drift))
    return _result(DATA_DRIFT, trading_day, ["SESSION_ARTIFACT_DIFFERS"],
                   drift_file=paths["drift"].name)


def build_session_artifact(*, trading_day: date, index: int, binding: ContractBinding,
                           receipt: Mapping, gate: GateResult, start_state: dict,
                           bars_by_symbol: Mapping[str, list], prev_sha: str | None,
                           root: Path) -> dict[str, Any]:
    """Pure: identical inputs give an identical artifact (no wall clock)."""
    bars_by_minute: dict[str, dict[str, Any]] = {}
    bar_inputs: dict[str, dict[str, Any]] = {}
    for sym, bars in sorted(bars_by_symbol.items()):
        rows = [[iso_z(b.ts_utc), b.open, b.high, b.low, b.close, b.volume] for b in bars]
        bar_inputs[sym] = {"count": len(rows), "sha256": sha256_payload(rows)}
        for bar in bars:
            bars_by_minute.setdefault(iso_z(bar.ts_utc), {})[sym] = bar
    minutes = sorted(bars_by_minute)
    state = start_session(start_state, gate, binding)
    for minute in minutes:
        state = step_bar(state, minute, bars_by_minute[minute], binding)
    state = finish_session(state, binding)
    # Restart check: persist and reload the state at a deterministic midpoint, then resume.
    rebuilt = start_session(start_state, gate, binding)
    midpoint = len(minutes) // 2
    for position, minute in enumerate(minutes):
        if position == midpoint:
            rebuilt = _checkpoint(rebuilt)
        rebuilt = step_bar(rebuilt, minute, bars_by_minute[minute], binding)
    if not minutes:
        rebuilt = _checkpoint(rebuilt)
    rebuilt = finish_session(rebuilt, binding)
    reconciliation = reconcile_state(state, rebuilt_state=rebuilt).to_dict()
    reconciliation["restart_checkpoint_bar"] = minutes[midpoint] if minutes else None

    day = trading_day.isoformat()
    prior_order_ids = {o["client_order_id"] for o in start_state["orders"]}
    orders = [o for o in state["orders"] if o["client_order_id"] not in prior_order_ids]
    fills = [f for f in state["fills"] if f["trading_day"] == day]
    session = state["session"]
    brief_path, receipt_path = freeze_paths(trading_day, root / "reports")
    artifact = {
        "schema": SESSION_SCHEMA,
        "trading_day": day,
        "session_index": index,
        "strategy_template_id": binding.strategy_template_id,
        "contract_sha256": binding.sha256,
        "contract_version": binding.contract["version"],
        "first_session_receipt_sha256": receipt["artifact_sha256"],
        "prev_session_artifact_sha256": prev_sha,
        "inputs": {
            "freeze_brief_file_sha256": file_sha256(brief_path) if brief_path.is_file() else None,
            "freeze_receipt_file_sha256": (file_sha256(receipt_path)
                                           if receipt_path.is_file() else None),
            "contract_sha256": binding.sha256,
            "activation_config_sha256": receipt["activation_config_sha256"],
            "bars": bar_inputs,
        },
        "gate": gate.to_dict(),
        "status": session_status(state, fills),
        "engine_status": session["status"],
        "events": session["events"],
        "orders": orders,
        "fills": fills,
        "marks": state["marks"],
        "halted": state["halted"],
        "equity": {"start": session["day_start_equity"], "end": session["end_equity"],
                   "peak": state["peak_equity"]},
        "reconciliation": reconciliation,
        "derived_costs": {
            "total": derived_cost_drags(fills, binding),
            "by_leg": {leg: derived_cost_drags([f for f in fills if f["leg"] == leg], binding)
                       for leg in sorted({f["leg"] for f in fills})},
        },
        "legs": leg_summary(fills),
        "end_state": state,
    }
    artifact["artifact_sha256"] = sha256_payload(artifact)
    return artifact


def session_status(state: Mapping, fills: list[Mapping]) -> str:
    engine_status = state["session"]["status"]
    if engine_status == COMPLETE:
        return "TRADED" if fills else "NO_TRADE"
    return engine_status          # NO_TRADE | HALTED | UNRESOLVED_EXIT


def leg_summary(fills: list[Mapping]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for fill in fills:
        row = out.setdefault(fill["leg"], {"fills": 0, "buy_notional": 0.0, "sell_notional": 0.0,
                                           "fees": 0.0, "net_cash_flow": 0.0})
        notional = fill["quantity"] * fill["price"]
        row["fills"] += 1
        row["buy_notional" if fill["side"] == "BUY" else "sell_notional"] += notional
        row["fees"] += fill["fee"]
        row["net_cash_flow"] += (-notional if fill["side"] == "BUY" else notional) - fill["fee"]
    return {leg: out[leg] for leg in sorted(out)}


def build_ledger(root: Path, sessions: list[date], *, through: date,
                 receipt_sha256: str | None = None,
                 contract_sha256: str | None = None) -> dict[str, Any]:
    """Pure fold over the immutable session artifacts (regenerable at any time). Every
    artifact is verified (schema, signature, day, index, chain, receipt); a failing one
    becomes an INVALID row and is never folded."""
    rows = []
    prev_sha: str | None = None
    for index, day in enumerate(sessions):
        if day > through:
            break
        path = paper_paths(root, day)["session"]
        if not path.is_file():
            rows.append({"trading_day": day.isoformat(), "status": INCOMPLETE,
                         "artifact_sha256": None})
            prev_sha = None
            continue
        artifact = _read_json(path)
        defect = verify_session_artifact(
            artifact, day=day, index=index, receipt_sha256=receipt_sha256,
            contract_sha256=contract_sha256, prev_sha=prev_sha, check_prev=index > 0)
        if defect is not None:
            rows.append({"trading_day": day.isoformat(), "status": INVALID, "reason": defect,
                         "artifact_sha256": None})
            prev_sha = None
            continue
        prev_sha = artifact["artifact_sha256"]
        rows.append({
            "trading_day": artifact["trading_day"],
            "status": artifact["status"],
            "artifact_sha256": artifact["artifact_sha256"],
            "prev_session_artifact_sha256": artifact["prev_session_artifact_sha256"],
            "equity_start": artifact["equity"]["start"],
            "equity_end": artifact["equity"]["end"],
            "legs": artifact["legs"],
            "reconciliation_ok": artifact["reconciliation"]["ok"],
            "halted": artifact["halted"],
        })
    ledger = {"schema": LEDGER_SCHEMA, "scheduled_sessions": [d.isoformat() for d in sessions],
              "rows": rows}
    ledger["artifact_sha256"] = sha256_payload(ledger)
    return ledger


def render_markdown(artifact: Mapping) -> str:
    lines = [
        f"# FREEZE Paper 세션 {artifact['trading_day']}",
        "",
        f"- status: **{artifact['status']}** (engine `{artifact['engine_status']}`)",
        f"- gate: `{artifact['gate']['decision']}` {artifact['gate']['reason_codes']}",
        f"- equity: {artifact['equity']['start']:.6f} → {artifact['equity']['end']:.6f}",
        f"- reconciliation ok: `{artifact['reconciliation']['ok']}`",
        f"- artifact SHA-256: `{artifact['artifact_sha256']}`",
        "",
        "| 시각(UTC) | 종목 | 매매 | 종류 | 수량 | 체결가 | 수수료 | 다리 |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for f in artifact["fills"]:
        lines.append(f"| {f['timestamp']} | {f['symbol']} | {f['side']} | {f['kind']} | "
                     f"{f['quantity']:.6f} | {f['price']:.6f} | {f['fee']:.6f} | {f['leg']} |")
    if not artifact["fills"]:
        lines.append("| - | - | - | - | - | - | - | - |")
    lines += ["", "Paper 전용 기록이다. 실주문·실계좌 권한이 없다.", ""]
    return "\n".join(lines)


def _gate_symbols(gate: GateResult, state: Mapping) -> list[str]:
    if gate.decision != TRADE or state["halted"] is not None:
        return []
    return [c.traded_symbol for c in gate.candidates]


def _backfill(root: Path, paths: Mapping[str, Path], trading_day: date, symbols: list[str],
              runner: CommandRunner, *, step: str = "market_tail_repair",
              run_suffix: str = "") -> list[dict[str, Any]]:
    run_id = f"premarket-freeze-paper-{trading_day:%Y%m%d}" + (f"-{run_suffix}" if run_suffix
                                                                 else "")
    command = [
        sys.executable, str(root / "scripts" / "history_backfill_official.py"),
        "--db", str(paths["market_db"]), "--symbols", ",".join(symbols), "--interval", "1m",
        "--completed-through", trading_day.isoformat(), "--tail-repair",
        "--sessions", "REGULAR", "--max-pages", "8", "--page-size", "200",
        "--spacing", "0.4", "--run-id", run_id,
        "--raw-snapshot-dir", str(paths["raw_snapshots"]), "--json",
    ]
    return run_commands([(step, None, command)], root, runner, log_dir=paths["logs"])


def verify_session_artifact(artifact: Any, *, day: date, index: int,
                            receipt_sha256: str | None, contract_sha256: str | None,
                            prev_sha: str | None, check_prev: bool = True) -> str | None:
    """Fixed defect code, or None when the artifact is signed and chained as expected."""
    if not isinstance(artifact, dict):
        return "SESSION_UNREADABLE"
    unsigned = {k: v for k, v in artifact.items() if k != "artifact_sha256"}
    if artifact.get("schema") != SESSION_SCHEMA:
        return "SESSION_SCHEMA_MISMATCH"
    if artifact.get("artifact_sha256") != sha256_payload(unsigned):
        return "SESSION_SIGNATURE_INVALID"
    if artifact.get("trading_day") != day.isoformat() or artifact.get("session_index") != index:
        return "SESSION_POSITION_INVALID"
    if contract_sha256 is not None and artifact.get("contract_sha256") != contract_sha256:
        return "SESSION_CONTRACT_MISMATCH"
    if receipt_sha256 is not None and artifact.get("first_session_receipt_sha256") != (
            receipt_sha256):
        return "SESSION_RECEIPT_MISMATCH"
    if check_prev and artifact.get("prev_session_artifact_sha256") != prev_sha:
        return "SESSION_CHAIN_BROKEN"
    return None


def _load_verified(root: Path, day: date, binding: ContractBinding, receipt_sha256: str,
                   index: int, prev_sha: str | None = None) -> tuple[dict | None, str | None]:
    """Previous session (prev_sha unknown: chain checked when it was written) or the
    existing current session (prev_sha given)."""
    path = paper_paths(root, day)["session"]
    if not path.is_file():
        return None, "MISSING_PREVIOUS_SESSION"
    artifact = _read_json(path)
    defect = verify_session_artifact(
        artifact, day=day, index=index, receipt_sha256=receipt_sha256,
        contract_sha256=binding.sha256, prev_sha=prev_sha, check_prev=prev_sha is not None
        or index == 0)
    if defect is not None:
        return None, defect
    return artifact, None


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _checkpoint(state: dict) -> dict:
    """Persist-and-reload round trip used by the restart check."""
    return json.loads(state_bytes(state))


def _result(status: str, trading_day: date, codes: list[str], **extra: Any) -> dict[str, Any]:
    return {"schema": OPERATION_SCHEMA, "operation": OPERATION,
            "trading_day": trading_day.isoformat(), "status": status,
            "reason_codes": codes, **extra}
