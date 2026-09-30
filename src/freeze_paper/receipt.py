"""First-session receipt: the only activation switch of the FREEZE Paper runner.

The receipt is a tracked, reviewed JSON file written before the first future
session.  It binds the exact contract bytes, activation config, code commit,
the 10 scheduled NYSE sessions, benchmark sources, operating costs and the
verified failure paths.  ``validate_receipt`` rejects anything missing or
inconsistent; this module never creates a receipt.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from src.freeze_paper.binding import CONTRACT_PATH
from src.history.exchange_calendar import CalendarRangeError, is_trading_day
from src.premarket.contract import load_config, sha256_payload
from src.universe.screener import leverage_for

SCHEMA = "premarket-freeze-long-paper/first-session-receipt/v1"
ROOT = CONTRACT_PATH.parents[2]
RECEIPT_RELATIVE = Path("docs/research/PREREG_PREMARKET_FREEZE_LONG_PAPER_V1_FIRST_SESSION_RECEIPT.json")
CONTRACT_MD_RELATIVE = Path("docs/research/PREREG_PREMARKET_FREEZE_LONG_PAPER_V1.md")
CONTRACT_JSON_RELATIVE = Path("docs/research/PREREG_PREMARKET_FREEZE_LONG_PAPER_V1.json")
CONFIG_RELATIVE = Path("config/premarket_daily_brief.yaml")
VERIFIED_PATH_KEYS = ("restart", "eod", "reconciliation", "duplicate")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_NODE = re.compile(r"^(tests/[\w/]+\.py)::(test_\w+)")

REQUIRED_FIELDS = (
    "schema", "contract_json_sha256", "contract_md_sha256", "contract_version", "code_commit",
    "activation_config_sha256", "first_session", "scheduled_sessions", "window_start_close_date",
    "intraday_bar_provider", "spy_price_source", "spy_dividend_source",
    "spy_ex_dividend_dates_in_window", "operating_costs", "account_event_fee",
    "allowed_symbols_check", "verified_paths", "test_run", "artifact_sha256",
)


class ReceiptError(ValueError):
    """The first-session receipt is missing, malformed or inconsistent."""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def scheduled_sessions(first_session: date, count: int) -> list[date]:
    out: list[date] = []
    day = first_session
    while len(out) < count:
        if is_trading_day(day):
            out.append(day)
        day += timedelta(days=1)
    return out


def previous_trading_day(day: date) -> date:
    prev = day - timedelta(days=1)
    while not is_trading_day(prev):
        prev -= timedelta(days=1)
    return prev


def load_receipt(root: Path = ROOT, *, path: Path | None = None) -> dict[str, Any]:
    receipt_path = path or root / RECEIPT_RELATIVE
    if not receipt_path.is_file():
        raise ReceiptError("MISSING_FIRST_SESSION_RECEIPT")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReceiptError("UNREADABLE_FIRST_SESSION_RECEIPT") from exc
    return validate_receipt(receipt, root=root)


def validate_receipt(receipt: Any, *, root: Path = ROOT) -> dict[str, Any]:
    """Return the receipt if every field is present and consistent; raise ReceiptError."""
    if not isinstance(receipt, Mapping):
        raise ReceiptError("RECEIPT_NOT_OBJECT")
    missing = [name for name in REQUIRED_FIELDS if name not in receipt]
    if missing:
        raise ReceiptError(f"RECEIPT_MISSING_FIELDS:{','.join(missing)}")
    if receipt["schema"] != SCHEMA:
        raise ReceiptError("RECEIPT_SCHEMA_MISMATCH")
    unsigned = {k: v for k, v in receipt.items() if k != "artifact_sha256"}
    if receipt["artifact_sha256"] != sha256_payload(unsigned):
        raise ReceiptError("RECEIPT_SHA256_MISMATCH")

    contract_path = root / CONTRACT_JSON_RELATIVE
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    _expect(receipt["contract_json_sha256"] == file_sha256(contract_path), "CONTRACT_JSON_SHA256")
    _expect(receipt["contract_md_sha256"] == file_sha256(root / CONTRACT_MD_RELATIVE),
            "CONTRACT_MD_SHA256")
    _expect(receipt["contract_version"] == contract["version"], "CONTRACT_VERSION")
    _expect(isinstance(receipt["code_commit"], str) and bool(_HEX40.match(receipt["code_commit"])),
            "CODE_COMMIT")
    config_path = root / CONFIG_RELATIVE
    _expect(receipt["activation_config_sha256"] == file_sha256(config_path),
            "ACTIVATION_CONFIG_SHA256")
    _check_config(config_path, contract, receipt["allowed_symbols_check"])

    sessions = _check_sessions(receipt, contract)
    for name in ("intraday_bar_provider", "spy_price_source", "spy_dividend_source"):
        _expect(_nonempty_str(receipt[name]), name.upper())
    ex_dates = receipt["spy_ex_dividend_dates_in_window"]
    _expect(isinstance(ex_dates, list), "SPY_EX_DIVIDEND_DATES")
    window_start = date.fromisoformat(receipt["window_start_close_date"])
    for value in ex_dates:
        _expect(isinstance(value, str) and window_start < _date(value) <= sessions[-1],
                "SPY_EX_DIVIDEND_DATES")
    _check_operating_costs(receipt["operating_costs"])
    fee = receipt["account_event_fee"]
    _expect(isinstance(fee, Mapping) and "observed_commission_bps_per_side" in fee
            and (fee["observed_commission_bps_per_side"] is None
                 or _number(fee["observed_commission_bps_per_side"]))
            and _nonempty_str(fee.get("note")), "ACCOUNT_EVENT_FEE")
    _check_verified_paths(receipt["verified_paths"], root)
    run = receipt["test_run"]
    _expect(isinstance(run, Mapping) and _nonempty_str(run.get("command"))
            and run.get("passed") is True, "TEST_RUN")
    return dict(receipt)


GitRunner = Callable[[list[str], Path], subprocess.CompletedProcess]


def git_subprocess(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False, timeout=10)


def code_commit_defect(receipt: Mapping, root: Path,
                       git_runner: GitRunner | None = None) -> str | None:
    """None when receipt.code_commit is the running HEAD or one of its ancestors."""
    run = git_runner or git_subprocess
    try:
        done = run(["git", "merge-base", "--is-ancestor", str(receipt["code_commit"]), "HEAD"],
                   root)
    except (OSError, subprocess.SubprocessError):
        return "GIT_UNAVAILABLE"
    if done.returncode == 0:
        return None
    return "CODE_COMMIT_NOT_ANCESTOR" if done.returncode == 1 else "GIT_UNAVAILABLE"


def _check_sessions(receipt: Mapping, contract: Mapping) -> list[date]:
    """Out-of-range dates are rejected, never treated as holidays."""
    first = _date(receipt["first_session"])
    try:
        _expect(is_trading_day(first), "FIRST_SESSION_NOT_TRADING_DAY")
        count = int(contract["evaluation"]["scheduled_trading_sessions"])
        expected = [d.isoformat() for d in scheduled_sessions(first, count)]
        window_start = previous_trading_day(first).isoformat()
    except CalendarRangeError as exc:
        raise ReceiptError("RECEIPT_INVALID:CALENDAR_RANGE") from exc
    _expect(receipt["scheduled_sessions"] == expected, "SCHEDULED_SESSIONS")
    _expect(receipt["window_start_close_date"] == window_start, "WINDOW_START_CLOSE_DATE")
    return [date.fromisoformat(d) for d in expected]


def _check_config(config_path: Path, contract: Mapping, check: Any) -> None:
    config = load_config(config_path)
    template = contract["strategy_template_id"]
    _expect(template in config.approved_strategy_templates
            and template in config.approved_short_strategy_templates, "ACTIVATION_TEMPLATES")
    index_etfs = set(contract["direction_scope"]["index_3x_etfs_traded_as_long_watch"])
    allowed = set(config.allowed_symbols)
    single_stock = sorted(s for s in allowed if leverage_for(s) != 1 and s not in index_etfs)
    _expect(index_etfs <= allowed, "ALLOWED_SYMBOLS_MISSING_INDEX_3X")
    _expect(not single_stock, "ALLOWED_SYMBOLS_SINGLE_STOCK_LEVERAGED")
    _expect(isinstance(check, Mapping)
            and sorted(check.get("index_3x_etfs_listed", [])) == sorted(index_etfs)
            and check.get("single_stock_leveraged_etfs") == []
            and check.get("passed") is True, "ALLOWED_SYMBOLS_CHECK")


def _check_operating_costs(costs: Any) -> None:
    _expect(isinstance(costs, Mapping), "OPERATING_COSTS")
    items = costs.get("items")
    _expect(isinstance(items, list) and all(
        isinstance(i, Mapping) and _nonempty_str(i.get("name")) and _number(i.get("usd"))
        and i["usd"] >= 0 for i in items), "OPERATING_COSTS_ITEMS")
    total = costs.get("total_usd")
    _expect(_number(total) and abs(total - sum(i["usd"] for i in items)) < 1e-9,
            "OPERATING_COSTS_TOTAL")
    _expect(_nonempty_str(costs.get("allocation")) and _nonempty_str(costs.get("reason")),
            "OPERATING_COSTS_METHOD")


def _check_verified_paths(paths: Any, root: Path) -> None:
    _expect(isinstance(paths, Mapping) and set(paths) == set(VERIFIED_PATH_KEYS),
            "VERIFIED_PATHS")
    for key in VERIFIED_PATH_KEYS:
        nodes = paths[key]
        _expect(isinstance(nodes, list) and nodes, f"VERIFIED_PATHS_{key.upper()}")
        for node in nodes:
            match = _NODE.match(node) if isinstance(node, str) else None
            _expect(match is not None, f"VERIFIED_PATHS_{key.upper()}")
            test_file = root / match.group(1)
            _expect(test_file.is_file() and f"def {match.group(2)}(" in test_file.read_text(
                encoding="utf-8"), f"VERIFIED_PATHS_{key.upper()}")


def _expect(condition: bool, code: str) -> None:
    if not condition:
        raise ReceiptError(f"RECEIPT_INVALID:{code}")


def _date(value: Any) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ReceiptError("RECEIPT_INVALID:DATE") from exc


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
