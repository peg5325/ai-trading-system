"""Exploratory day-trading Paper (CONTRACT_EXPLORATORY_PAPER_DAYTRADE_V1, rules v1.0).

Run after the US close once the session's 1m bars are in the history DB (read-only):

  python scripts/run_daytrade_paper.py --date 2026-10-05
  python scripts/run_daytrade_paper.py --date 2026-10-05 --backfill-check   # data check only, no writes
  python scripts/run_daytrade_paper.py --auto --out-root <checkout>          # launchd: catch up

Writes reports/daytrade_paper/<date>.json (write-once), updates reports/daytrade_paper/ledger.json
and writes docs/journal/<date>.md (facts; the retrospective is left for the assistant).
No broker or order API is called. --dry-run-dir redirects every output (sanity runs only).
--out-root moves reports/ and data/external/ (e.g. to the main checkout when run from a worktree).
A live run whose data checks fail exits 3 and writes nothing unless --accept-incomplete is given.

--auto runs every NYSE session after the ledger's last date (or from PAPER_START) up to the last
session closed at least SETTLE ago, oldest first, and stops at the first one that does not finish.
A session runs only once <out-root>/reports/history-collection/<YYYYMMDD>.operation.json is COMPLETE;
otherwise it exits 3 (waiting) and the next launchd slot retries. Exit 4 needs a human: a receipt for an
older day that the collector will never fetch (STALLED), or data checks failing on collected data
(NEEDS_HUMAN). Exit 2: refused (e.g. outside the calendar). Exit 0 means nothing is pending.
--accept-incomplete stays a human decision and is refused with --auto.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import data_quality, decision_time
from src.daytrade_paper.external import (
    events_for,
    fetch_external,
    is_trading_day,
    load_events,
    prev_trading_day,
)
from src.daytrade_paper.journal import render
from src.daytrade_paper.simulate import run_day
from src.daytrade_paper.store import (
    RunRefused,
    check_chain,
    commit_outputs,
    ledger_row,
    load_bars,
    load_ledger,
)

EVENTS = ROOT / "config" / "daytrade_events_v1.yaml"
PAPER_START = "2026-10-05"  # first session of rules v1.0
NY = ZoneInfo("America/New_York")
SETTLE = timedelta(hours=1)  # a session counts as closed this long after the bell


def _git_head() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def latest_closed_session(cal: dict, now: datetime) -> str:
    """The most recent NYSE session whose close (13:00 ET on early-close days) is >= SETTLE ago."""
    et = now.astimezone(NY)
    day = et.date().isoformat()
    if is_trading_day(cal, day):
        close = time(13) if events_for(cal, day)["early_close"] else time(16)
        if et >= datetime.combine(et.date(), close, NY) + SETTLE:
            return day
    return prev_trading_day(cal, day)


def pending_sessions(cal: dict, ledger: dict, latest: str, first: str = PAPER_START) -> list[str]:
    """NYSE sessions still to run, oldest first: after the ledger's last date, or from `first`."""
    days = ledger["days"]
    d = date.fromisoformat(days[-1]["date"]) + timedelta(days=1) if days else date.fromisoformat(first)
    out: list[str] = []
    while d.isoformat() <= latest:
        if is_trading_day(cal, d.isoformat()):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def collection_complete(out_root: Path, day: str) -> bool:
    """True when the history-collect receipt for `day` exists and is COMPLETE."""
    receipt = out_root / "reports" / "history-collection" / f"{day.replace('-', '')}.operation.json"
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("status") == "COMPLETE" and data.get("trading_day") == day


def run_auto(args, now: datetime | None = None) -> int:
    out_root = args.out_root.resolve()
    cal = load_events(args.events)
    latest = latest_closed_session(cal, now or datetime.now(timezone.utc))
    ledger = load_ledger(out_root / "reports" / "daytrade_paper" / "ledger.json")
    todo = pending_sessions(cal, ledger, latest)
    print(json.dumps({"auto": "PENDING" if todo else "UP_TO_DATE", "latest_closed": latest, "pending": todo}))
    for day in todo:
        if not events_for(cal, day)["known"]:
            print(f"REFUSED: {day} is outside the event calendar coverage {cal['coverage']}; extend "
                  f"{args.events.name} first", file=sys.stderr)
            return 2
        if not collection_complete(out_root, day):
            if day < latest:  # the collector only fetches the latest day; this one will not land by itself
                print(f"STALLED: no COMPLETE history-collection receipt for {day}; backfill it by hand",
                      file=sys.stderr)
                return 4
            print(f"WAITING: history-collection receipt for {day} is not COMPLETE; the next slot retries",
                  file=sys.stderr)
            return 3
        rc = run_session(day, args)
        if rc == 3:  # data checks failed on collected data: retrying will not change it
            print(f"NEEDS_HUMAN: {day} data checks failed after a COMPLETE collection", file=sys.stderr)
            return 4
        if rc != 0:
            return rc
    return 0


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--date", help="US session date YYYY-MM-DD (ET)")
    which.add_argument("--auto", action="store_true",
                       help="run every pending session up to the last closed one (launchd)")
    ap.add_argument("--backfill-check", action="store_true",
                    help="only check that the session's 1m bars are in the DB and complete; write nothing")
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "market_history.sqlite")
    ap.add_argument("--events", type=Path, default=EVENTS)
    ap.add_argument("--dry-run-dir", type=Path, default=None,
                    help="write report, ledger, journal and external CSVs under this dir instead")
    ap.add_argument("--out-root", type=Path, default=ROOT,
                    help="root for reports/daytrade_paper/ and data/external/daytrade_paper/ "
                         "(default: this script's repo); journals still go to <script repo>/docs/journal/")
    ap.add_argument("--accept-incomplete", action="store_true",
                    help="live run only: record INCOMPLETE_DATA instead of refusing (exit 3) when data checks fail")
    args = ap.parse_args(argv)
    if args.auto:
        if args.backfill_check or args.dry_run_dir or args.accept_incomplete:
            ap.error("--auto cannot be combined with --backfill-check, --dry-run-dir or --accept-incomplete")
        return run_auto(args, now)
    return run_session(args.date, args)


def run_session(day: str, args) -> int:

    if args.dry_run_dir:
        base = args.dry_run_dir
        report_dir, journal_dir, ext_dir = base / "reports", base / "journal", base / "external"
    else:
        out_root = args.out_root.resolve()
        if "/.claude/worktrees/" in f"{out_root}/":
            print(f"WARNING: --out-root {out_root} is inside .claude/worktrees/; outputs are lost "
                  "when the worktree is removed", file=sys.stderr)
        report_dir = out_root / "reports" / "daytrade_paper"
        journal_dir = ROOT / "docs" / "journal"
        ext_dir = out_root / "data" / "external" / "daytrade_paper"
    report_path, journal_path = report_dir / f"{day}.json", journal_dir / f"{day}.md"
    ledger_path = report_dir / "ledger.json"

    try:
        cal = load_events(args.events)
        events = events_for(cal, day)
        if not events["known"] and not args.dry_run_dir:
            raise RunRefused(f"{day} is outside the event calendar coverage {cal['coverage']}")
        if not is_trading_day(cal, day):
            raise RunRefused(f"{day} is not an NYSE trading day (weekend or listed holiday)")
        prev_day = prev_trading_day(cal, day)
        prev_early = events_for(cal, prev_day)["early_close"]
        data = load_bars(args.db, day)
        if not data["regular"][R.SIGNAL]:
            raise RunRefused(f"no QQQ REGULAR 1m bars for {day} in {args.db} (not landed or not a trading day)")
        q = data_quality(data["regular"], data["prior"], events["early_close"], decision_time(events),
                         data["prev_times"], prev_day, prev_early)
        if args.backfill_check:
            print(json.dumps({"date": day, "prev_session": data["prev_session"], **q}, ensure_ascii=False, indent=2))
            return 0 if q["ok"] else 3
        for p in (report_path, journal_path):
            if p.exists():
                raise RunRefused(f"{p} already exists; outputs are write-once")
        if not q["ok"] and not args.dry_run_dir and not args.accept_incomplete:
            print("REFUSED (data incomplete, nothing written; rerun later or pass --accept-incomplete): "
                  + "; ".join(q["issues"]), file=sys.stderr)
            return 3
        ledger = load_ledger(ledger_path)
        equity = check_chain(ledger, day, prev_day)
        external = fetch_external(day, ext_dir)
        result = run_day(day, data["regular"], data["qqq_pre"], data["prior"], external, events, equity,
                         prev_times=data["prev_times"], expected_prev=prev_day, prev_early_close=prev_early)
        result["run_meta"] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_head": _git_head(),
            "db": str(args.db),
            "events_file": str(args.events),
            "events_sha256": hashlib.sha256(args.events.read_bytes()).hexdigest(),
            "dry_run": bool(args.dry_run_dir),
            "orders_sent": 0,
        }
        commit_outputs(report_path, json.dumps(result, ensure_ascii=False, indent=2, default=str),
                       journal_path, render(result), ledger_path, ledger,
                       ledger_row(result, str(report_path)))
    except RunRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"date": day, "situations": result["situations"], "trades": len(result["trades"]),
                      "day_pnl": round(result["pnl"]["day_pnl"], 4),
                      "equity_end": round(result["pnl"]["equity_end"], 4),
                      "report": str(report_path), "journal": str(journal_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
