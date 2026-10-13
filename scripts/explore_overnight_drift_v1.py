"""Exploratory QQQ overnight drift long, USD 2,000 (spec docs/research/EXPLORE_OVERNIGHT_DRIFT_2000USD_V1.md).

Exploratory only (spec §0, outside the Charter's same-day scope): not a decision basis. Reads QQQ 1m bars only through
the ORB exploration loader (research_load, purpose="performance", track=intraday, market="US") for 2023-01-01 ~
2026-01-20 and refuses any request reaching 2026-01-21 00:00 ET. Prints the §6 tables and writes
EXPLORE_OVERNIGHT_DRIFT_2000USD_V1.json (write-once) to --out-dir, with the pre-declared benchmarks (no-filter
overnight, overnight / intraday split; QQQ 1x hold is in the metrics).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
from dataclasses import asdict
from datetime import date as Date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.explore_orb_vwap_v0.data_loader import DEV_LAST_DAY, LOAD_START, DataLoader
from src.explore_orb_vwap_v0.reporter import BacktestReporter
from src.explore_orb_vwap_v0.universe import prepare_symbol
from src.explore_overnight_drift_v1.engine import SYMBOL, return_split, run_backtest
from src.history.exchange_calendar import trading_days

SPEC = ROOT / "docs" / "research" / "EXPLORE_OVERNIGHT_DRIFT_2000USD_V1.md"
OUT_NAME = "EXPLORE_OVERNIGHT_DRIFT_2000USD_V1.json"


def git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def hold_stats(holds) -> dict:
    h = [x.hours for x in holds]
    return {"mean_hours": statistics.mean(h) if h else None, "median_hours": statistics.median(h) if h else None,
            "max_hours": max(h) if h else None}


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.2f}%"


def extra_markdown(result, holds, bench_metrics: dict, split: dict) -> str:
    hs = hold_stats(holds)
    scored = len(result.days)
    out = ["", "## Holding time", "",
           (f"mean {hs['mean_hours']:.2f} h, median {hs['median_hours']:.2f} h, max {hs['max_hours']:.2f} h "
            "(real elapsed time, DST included)") if holds else "no trades",
           "", "## Non-traded day status", "", "| status | days |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in result.skips.items()]
    bm = bench_metrics
    out += ["", "## Pre-declared benchmarks (spec §6, not candidates)", "", "| benchmark | value |", "|---|---|",
            (f"| no-filter overnight (same rules and costs): trades / net P&L / return / ref-cost return | "
             f"{bm['trade_count']} / {bm.get('trade_net_pnl', 0):,.2f} / {_pct(bm.get('cumulative_return'))} / "
             f"{_pct((bm.get('reference_cost') or {}).get('cumulative_return'))} |"),
            (f"| QQQ split over {split['days']} of {scored} scored days (complete with a complete prior day; "
             f"includes the first day's overnight gap, so it differs from the 1x hold window): overnight "
             f"(prior close -> open) / intraday (open -> close) / combined, no costs | "
             f"{_pct(split['overnight_return'])} / {_pct(split['intraday_return'])} / "
             f"{_pct(split['combined_return'])} |"),
            "",
            ("Trade.initial_stop / risk_per_share hold the gap-stop level (D close x 0.985) and fill minus it; the stop "
             "is a next-open check, so they are not a risk amount.")]
    return "\n".join(out) + "\n"


def run(db: str, out_dir: Path, start: Date = LOAD_START, end: Date = DEV_LAST_DAY) -> str:
    out = out_dir / OUT_NAME
    if out.exists():
        raise SystemExit(f"{out} already exists; refusing to overwrite")
    data = DataLoader(db, start, end).load((SYMBOL,))
    days = trading_days(start, end)
    qqq = prepare_symbol(SYMBOL, data[SYMBOL].regular, days)
    result, holds = run_backtest(qqq, days)
    bench, bench_holds = run_backtest(qqq, days, filters=False)
    rep = BacktestReporter(result)
    bench_metrics = BacktestReporter(bench).metrics()
    scored = [d.day for d in result.days]
    split = return_split(qqq, scored)
    provenance = {
        "git_head": git("rev-parse", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")),
        "config_name": "OVERNIGHT_DRIFT_V1",
        "spec_path": str(SPEC.relative_to(ROOT)),
        "spec_sha256": hashlib.sha256(SPEC.read_bytes()).hexdigest(),
        "db_path": str(db),
        "load_window": [start.isoformat(), end.isoformat()],
        "symbols": [SYMBOL],
        "rows_read": {SYMBOL: data[SYMBOL].row_count},
        "scored_start": scored[0] if scored else None,
    }
    doc = {
        "status": "exploratory, outside the Charter same-day scope, not a decision basis (spec §0)",
        "provenance": provenance,
        "metrics": {**rep.metrics(), "holding": hold_stats(holds)},
        "benchmarks": {
            "no_filter_overnight": {**bench_metrics, "holding": hold_stats(bench_holds)},
            "qqq_return_split": split,
        },
        "days": [asdict(d) for d in result.days],
        "trades": [asdict(t) for t in result.trades],
        "holds": [asdict(h) for h in holds],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return rep.markdown() + extra_markdown(result, holds, bench_metrics, split)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="path to market_history.sqlite")
    ap.add_argument("--out-dir", required=True, type=Path)
    args = ap.parse_args(argv)
    print(run(args.db, args.out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
