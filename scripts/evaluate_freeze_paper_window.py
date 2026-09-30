#!/usr/bin/env python3
"""Evaluate the FREEZE Paper window (FINAL after the 10th session, otherwise INTERIM).

Paper evaluation only: not a live-trading approval and no automatic promotion.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.freeze_paper.binding import load_binding
from src.freeze_paper.evaluator import (
    DIVIDENDS_RELATIVE,
    evaluate_window,
    load_spy_dividends,
    write_evaluation,
)
from src.freeze_paper.receipt import CONTRACT_JSON_RELATIVE, ReceiptError, load_receipt
from src.freeze_paper.runner import paper_paths
from src.freeze_paper.spy import spy_close_evidence
from src.premarket.operations import MarketDbLockTimeout, market_db_lock


def default_through(root: Path, sessions: list[date]) -> date:
    """Last scheduled session when recorded (FINAL); else the latest recorded one (INTERIM)."""
    recorded = [d for d in sessions if paper_paths(root, d)["session"].is_file()]
    return sessions[-1] if sessions[-1] in recorded else (recorded[-1] if recorded else sessions[0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FREEZE Paper 10-session evaluator")
    parser.add_argument("--root", type=Path, default=ROOT, help="project root")
    parser.add_argument("--through", help="scheduled session YYYY-MM-DD (earlier = INTERIM)")
    parser.add_argument("--dry-run", action="store_true", help="print only, write nothing")
    args = parser.parse_args(argv)
    root = args.root
    try:
        receipt = load_receipt(root)
    except ReceiptError as exc:
        print(json.dumps({"status": "NOT_ACTIVATED", "reason_codes": [str(exc)]}))
        return 2
    binding = load_binding(root / CONTRACT_JSON_RELATIVE)
    sessions = [date.fromisoformat(d) for d in receipt["scheduled_sessions"]]
    through = date.fromisoformat(args.through) if args.through else default_through(root, sessions)
    days = [receipt["window_start_close_date"],
            *[d.isoformat() for d in sessions if d <= through],
            *receipt["spy_ex_dividend_dates_in_window"]]
    db = paper_paths(root, through)["market_db"]
    try:
        with market_db_lock(root):
            closes = {day: (spy_close_evidence(db, date.fromisoformat(day)) if db.is_file()
                            else None) for day in sorted(set(days))}
    except MarketDbLockTimeout:
        print(json.dumps({"status": "INCOMPLETE", "reason_codes": ["LOCK_TIMEOUT"]}))
        return 2
    evaluation = evaluate_window(
        root=root, receipt=receipt, binding=binding, spy_closes=closes,
        dividends=load_spy_dividends(root / DIVIDENDS_RELATIVE), through=through)
    summary = {"label": evaluation["label"], "outcome": evaluation["outcome"],
               "artifact_sha256": evaluation["artifact_sha256"]}
    if not args.dry_run:
        try:
            json_path, _ = write_evaluation(root, evaluation)
        except FileExistsError:
            print(json.dumps({**summary, "status": "DATA_DRIFT"}))
            return 2
        summary["path"] = json_path.name
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
