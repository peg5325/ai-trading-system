"""Exploratory day-trading Paper (CONTRACT_EXPLORATORY_PAPER_DAYTRADE_V1, rules v1.0).

Run after the US close once the session's 1m bars are in the history DB (read-only):

  python scripts/run_daytrade_paper.py --date 2026-10-05
  python scripts/run_daytrade_paper.py --date 2026-10-05 --backfill-check   # data check only, no writes

Writes reports/daytrade_paper/<date>.json (write-once), updates reports/daytrade_paper/ledger.json
and writes docs/journal/<date>.md (facts; the retrospective is left for the assistant).
No broker or order API is called. --dry-run-dir redirects every output (sanity runs only).
--out-root moves reports/ and data/external/ (e.g. to the main checkout when run from a worktree).
A live run whose data checks fail exits 3 and writes nothing unless --accept-incomplete is given.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

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


def _git_head() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="US session date YYYY-MM-DD (ET)")
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
    day = args.date

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
