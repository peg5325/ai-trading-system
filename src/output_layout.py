"""Single source of truth for where gitignored outputs live (docs/OUTPUT_LAYOUT.md).

Every function takes the repository root and returns a path; nothing here touches the
filesystem. File basenames are owned by the writers; this module only owns directories.
`classify` maps an entry of the pre-2026-10-14 layout to its new location and is the
only rule set `scripts/migrate_output_layout.py` uses.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path, PurePosixPath

PAPER_BATCH_2026_07 = "paper_2026-07"
PAPER_ARCHIVE_2026_07 = "2026-07_paper_sessions"
H2_H4 = "h2-h4"
MOVES_LEDGER = "MOVES.json"


def _day_key(day: date | str) -> str:
    if isinstance(day, date):
        return day.strftime("%Y%m%d")
    key = str(day).replace("-", "")
    if not re.fullmatch(r"\d{8}", key):
        raise ValueError(f"trading day must be YYYYMMDD or YYYY-MM-DD, got {day!r}")
    return key


def reports_dir(root: Path) -> Path:
    return Path(root) / "reports"


def moves_ledger(root: Path) -> Path:
    return reports_dir(root) / MOVES_LEDGER


# ---- premarket -------------------------------------------------------------

def premarket_dir(root: Path) -> Path:
    return reports_dir(root) / "premarket"


def premarket_day_relpath(day: date | str) -> Path:
    """Day folder relative to the reports directory (for callers that hold reports_root)."""
    return Path("premarket") / "days" / _day_key(day)


def premarket_day_dir(root: Path, day: date | str) -> Path:
    return reports_dir(root) / premarket_day_relpath(day)


def premarket_ledgers_dir(root: Path) -> Path:
    return premarket_dir(root) / "ledgers"


def premarket_logs_dir(root: Path) -> Path:
    return premarket_dir(root) / "logs"


def premarket_launchd_dir(root: Path) -> Path:
    return premarket_dir(root) / "launchd"


# ---- history collection ----------------------------------------------------

def history_collection_dir(root: Path) -> Path:
    return reports_dir(root) / "history-collection"


def history_collection_logs_dir(root: Path) -> Path:
    return history_collection_dir(root) / "logs"


# ---- day trading -----------------------------------------------------------

def daytrade_dir(root: Path) -> Path:
    return reports_dir(root) / "daytrade"


def daytrade_paper_dir(root: Path) -> Path:
    return daytrade_dir(root) / "paper"


def daytrade_shadow_dir(root: Path) -> Path:
    return daytrade_dir(root) / "shadow"


def daytrade_v11_dir(root: Path) -> Path:
    return daytrade_dir(root) / "v11"


def daytrade_v13_dir(root: Path) -> Path:
    return daytrade_dir(root) / "v13"


# ---- research, data ops, archive -------------------------------------------

def research_dir(root: Path, name: str) -> Path:
    return reports_dir(root) / "research" / name


def data_ops_dir(root: Path) -> Path:
    return reports_dir(root) / "data-ops"


def archive_dir(root: Path, name: str) -> Path:
    return reports_dir(root) / "archive" / name


# ---- paper runtime sessions (runs/, not reports/) --------------------------

def runs_dir(root: Path) -> Path:
    return Path(root) / "runs"


def paper_run_dir(root: Path, session: str, batch: str = "paper") -> Path:
    return runs_dir(root) / batch / session


# ---- classification of the pre-2026-10-14 layout ---------------------------

# Top-level reports/ names that already belong to the new layout.
LAYOUT_REPORT_ENTRIES = frozenset({
    "premarket", "history-collection", "daytrade", "research", "data-ops", "archive", MOVES_LEDGER,
})

_REPORT_DIR_RENAMES = {
    "premarket-operation-logs": "premarket/logs",
    "premarket-launchd": "premarket/launchd",
    "history-collection-logs": "history-collection/logs",
    "daytrade_paper": "daytrade/paper",
    "daytrade_shadow": "daytrade/shadow",
    "daytrade_v11": "daytrade/v11",
    "daytrade_v13": "daytrade/v13",
}
_RESEARCH_DIRS = frozenset({"daily_screen_v1", "entry_study_v1", "reversal_study_v1", "phase1a_probe"})
_PREMARKET_DATED = re.compile(r"^premarket-[a-z-]+\.(\d{8})\..+$")
_PREMARKET_LEDGER = re.compile(
    r"^premarket-(postclose-ledger\.json|postclose-cumulative\..+|freeze-paper-ledger\.json"
    r"|freeze-paper-evaluation\..+)$")
_H2_H4 = re.compile(
    r"^(h2b_backfill_validation\..+|h3_regime_validation\..+|h3b_longhorizon_validation\..+"
    r"|h4_screener_ranking\.json|h2_h3_preliminary_s001\.md|WORKORDER_H.*\.md"
    r"|PROMPT_h2b_backfill_cc\.md|score_correlation\.md)$")
_DATA_OPS = re.compile(r"^(p0_us30_.+|P0_US30_.+|.*replica_receipt.*|raw_snapshot_manifest.*)$")
_ARCHIVE = re.compile(
    r"^(SESSION_.*_FINAL\.md|session_.*\.md|paper_report_.*\.md|paper_trading_session_.*"
    r"|preflight_report_.*|phase6_.*_preflight\.md"
    r"|(krx|us)_.*(reviews|rule_improvement|system_review).*\.jsonl?"
    r"|CROSS_SESSION_ANALYSIS\.md|DECISION_MEMO_AB\.md|design_delta_review\.md"
    r"|exit_and_slicing_analysis\.md|INSTRUMENTATION_SPEC\.md|NEXT_SESSION_IMPROVEMENTS\.md)$")
_ROOT_LOGS = re.compile(r"^logs(?:_(.+))?$")
_ROOT_STATUS = re.compile(r"^status(?:_(.+))?\.json$")


def classify_report_entry(name: str) -> str | None:
    """New path relative to the repo root for a top-level reports/ entry of the old layout.

    None means unclassified (left in place). Entries already in the new layout are not
    passed here; see LAYOUT_REPORT_ENTRIES."""
    if name in _REPORT_DIR_RENAMES:
        return f"reports/{_REPORT_DIR_RENAMES[name]}"
    if name.startswith("explore_") or name in _RESEARCH_DIRS:
        return f"reports/research/{name}"
    if _PREMARKET_LEDGER.match(name):
        return f"reports/premarket/ledgers/{name}"
    dated = _PREMARKET_DATED.match(name)
    if dated:
        return f"reports/premarket/days/{dated.group(1)}/{name}"
    if _H2_H4.match(name):
        return f"reports/research/{H2_H4}/{name}"
    if _DATA_OPS.match(name):
        return f"reports/data-ops/{name}"
    if _ARCHIVE.match(name):
        return f"reports/archive/{PAPER_ARCHIVE_2026_07}/{name}"
    return None


def classify_root_entry(name: str, is_dir: bool) -> str | None:
    """New path for a repo-root paper runtime output (logs[_<session>]/, status[_<session>].json)."""
    match = _ROOT_LOGS.match(name) if is_dir else _ROOT_STATUS.match(name)
    if not match:
        return None
    session = match.group(1) or "default"
    leaf = "logs" if is_dir else "status.json"
    return f"runs/{PAPER_BATCH_2026_07}/{session}/{leaf}"


def classify(relpath: str, is_dir: bool) -> str | None:
    """New repo-relative path for an old-layout entry (POSIX, e.g. 'reports/foo.json'), else None."""
    parts = PurePosixPath(relpath).parts
    if len(parts) == 1:
        return classify_root_entry(parts[0], is_dir)
    if len(parts) == 2 and parts[0] == "reports" and parts[1] not in LAYOUT_REPORT_ENTRIES:
        return classify_report_entry(parts[1])
    return None
