#!/usr/bin/env python3
"""Evaluate a frozen premarket Daily Brief after regular close."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.premarket.postclose import (
    build_cumulative_report,
    build_postclose_evaluation,
    load_or_create_ledger,
    render_cumulative_markdown,
    render_evaluation_markdown,
    upsert_ledger_entry,
    write_json,
    write_text,
)
from src.premarket.report import json_bytes, write_immutable_pair


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="FREEZE Daily Brief -> read-only post-close evaluation ledger"
    )
    parser.add_argument("--freeze-brief", required=True, help="immutable FREEZE Daily Brief JSON")
    parser.add_argument("--market-db", default="data/market_history.sqlite")
    parser.add_argument(
        "--evaluation-as-of",
        required=True,
        help="tz-aware ISO timestamp; must be at or after regular close",
    )
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-markdown", required=True)
    parser.add_argument("--ledger-json", required=True)
    parser.add_argument("--cumulative-json", required=True)
    parser.add_argument("--cumulative-markdown", required=True)
    args = parser.parse_args(argv)

    freeze_path = Path(args.freeze_brief)
    freeze_brief, freeze_file_sha256 = _load_json_with_sha256(freeze_path)
    evaluation = build_postclose_evaluation(
        freeze_brief,
        market_db=args.market_db,
        evaluation_as_of=args.evaluation_as_of,
        freeze_input_file_sha256=freeze_file_sha256,
    )
    if evaluation["status"] in {"INCOMPLETE_PRE_CLOSE", "INCOMPLETE_INVALID_FREEZE"}:
        # These are operational contract errors, not auditable daily outcomes.
        # Persisting either would occupy the immutable date path and prevent a
        # legitimate post-close FREEZE evaluation from being written later.
        print(json.dumps({
            "status": evaluation["status"],
            "trading_day": evaluation["trading_day"],
            "reason_codes": evaluation["reason_codes"],
        }, ensure_ascii=False, sort_keys=True))
        return 2

    write_immutable_pair(
        (args.out_json, json_bytes(evaluation)),
        (args.out_markdown, render_evaluation_markdown(evaluation).encode("utf-8")),
    )

    ledger = load_or_create_ledger(args.ledger_json)
    ledger = upsert_ledger_entry(ledger, evaluation)
    cumulative = build_cumulative_report(ledger)
    write_json(args.ledger_json, ledger)
    write_json(args.cumulative_json, cumulative)
    write_text(args.cumulative_markdown, render_cumulative_markdown(cumulative))

    print(json.dumps({
        "status": evaluation["status"],
        "trading_day": evaluation["trading_day"],
        "artifact_sha256": evaluation["artifact_sha256"],
        "ledger_entry_count": len(ledger["entries"]),
        "complete_entry_count": cumulative["complete_entry_count"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


def _load_json_with_sha256(path: Path) -> tuple[dict, str]:
    content = path.read_bytes()
    data = json.loads(content.decode("utf-8"))
    if not isinstance(data, dict):
        raise TypeError("freeze Daily Brief JSON root must be an object")
    return data, hashlib.sha256(content).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
