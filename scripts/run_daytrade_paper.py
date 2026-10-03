"""Exploratory day-trading Paper (CONTRACT_EXPLORATORY_PAPER_DAYTRADE_V1, rules v1.0).

Run after the US close once the session's 1m bars are in the history DB (read-only):

  python scripts/run_daytrade_paper.py --date 2026-10-05
  python scripts/run_daytrade_paper.py --date 2026-10-05 --backfill-check   # data check only, no writes

Writes reports/daytrade_paper/<date>.json (write-once), updates reports/daytrade_paper/ledger.json
and writes docs/journal/<date>.md (facts; the retrospective is left for the assistant).
No broker or order API is called. --dry-run-dir redirects every output (sanity runs only).
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
from src.daytrade_paper.core import data_quality
from src.daytrade_paper.external import (
    events_for,
    fetch_external,
    load_events,
)
from src.daytrade_paper.journal import render
from src.daytrade_paper.simulate import run_day
from src.daytrade_paper.store import (
    RunRefused,
    check_chain,
    ledger_row,
    load_bars,
    load_ledger,
    write_ledger,
    write_once,
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
    args = ap.parse_args(argv)
    day = args.date

    if args.dry_run_dir:
        base = args.dry_run_dir
        report_dir, journal_dir, ext_dir = base / "reports", base / "journal", base / "external"
    else:
        report_dir = ROOT / "reports" / "daytrade_paper"
        journal_dir = ROOT / "docs" / "journal"
        ext_dir = ROOT / "data" / "external" / "daytrade_paper"
    report_path, journal_path = report_dir / f"{day}.json", journal_dir / f"{day}.md"
    ledger_path = report_dir / "ledger.json"

    try:
        cal = load_events(args.events)
        events = events_for(cal, day)
        if not events["known"] and not args.dry_run_dir:
            raise RunRefused(f"{day} is outside the event calendar coverage {cal['coverage']}")
        data = load_bars(args.db, day)
        if not data["regular"][R.SIGNAL]:
            raise RunRefused(f"no QQQ REGULAR 1m bars for {day} in {args.db} (not landed or not a trading day)")
        if args.backfill_check:
            q = data_quality(data["regular"], data["prior"], events["early_close"])
            print(json.dumps({"date": day, "prev_session": data["prev_session"], **q}, ensure_ascii=False, indent=2))
            return 0 if q["ok"] else 3
        for p in (report_path, journal_path):
            if p.exists():
                raise RunRefused(f"{p} already exists; outputs are write-once")
        ledger = load_ledger(ledger_path)
        equity = check_chain(ledger, day, data["prev_session"])
        external = fetch_external(day, ext_dir)
        result = run_day(day, data["regular"], data["qqq_pre"], data["prior"], external, events, equity)
        result["run_meta"] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_head": _git_head(),
            "db": str(args.db),
            "events_file": str(args.events),
            "events_sha256": hashlib.sha256(args.events.read_bytes()).hexdigest(),
            "dry_run": bool(args.dry_run_dir),
            "orders_sent": 0,
        }
        write_once(report_path, json.dumps(result, ensure_ascii=False, indent=2, default=str))
        write_once(journal_path, render(result))
        ledger["days"].append(ledger_row(result, str(report_path)))
        write_ledger(ledger_path, ledger)
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
