"""Exploratory QQQ false-breakout short fade, USD 2,000 (spec docs/research/EXPLORE_FALSE_BREAKOUT_FADE_2000USD_V1.md).

Exploratory only (spec §0): not a decision basis. Reads QQQ 1m bars only through the ORB exploration loader
(research_load, purpose="performance", track=intraday, market="US") for 2023-01-01 ~ 2026-01-20 and refuses any
request reaching 2026-01-21 00:00 ET. Prints the §6 tables and writes EXPLORE_FALSE_BREAKOUT_FADE_2000USD_V1.json
(write-once) to --out-dir.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import date as Date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.explore_false_breakout_fade_v1.engine import SYMBOL, run_backtest
from src.explore_orb_vwap_v0.data_loader import DEV_LAST_DAY, LOAD_START, DataLoader
from src.explore_orb_vwap_v0.reporter import BacktestReporter
from src.explore_orb_vwap_v0.universe import prepare_symbol
from src.history.exchange_calendar import trading_days

SPEC = ROOT / "docs" / "research" / "EXPLORE_FALSE_BREAKOUT_FADE_2000USD_V1.md"
OUT_NAME = "EXPLORE_FALSE_BREAKOUT_FADE_2000USD_V1.json"
LEG_REASONS = ("stop", "breakeven", "tp1_vwap", "tp2_orb_low", "tp2_2r", "flatten")


def git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def leg_counts(trades) -> dict[str, dict]:
    """Per exit reason over every leg: leg count, trades with such a leg, shares-weighted net P&L of those legs
    before commission (side x (fill - entry fill) x shares)."""
    out = {r: {"legs": 0, "trades": 0, "pnl_before_commission": 0.0} for r in LEG_REASONS}
    for t in trades:
        seen = set()
        for leg in t.legs:
            c = out[leg.reason]
            c["legs"] += 1
            c["pnl_before_commission"] += t.side * (leg.fill - t.entry_fill) * leg.shares
            if leg.reason not in seen:
                c["trades"] += 1
                seen.add(leg.reason)
    return out


def extra_markdown(result, legs: dict, setups: list[dict]) -> str:
    out = ["", "## Day status", "", "| status | days |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in result.skips.items()]
    out += ["", "## Exit legs by reason (every leg)", "",
            "| reason | legs | trades with this leg | P&L before commission (USD) |", "|---|---|---|---|"]
    for k, c in legs.items():
        out.append(f"| {k} | {c['legs']} | {c['trades']} | {c['pnl_before_commission']:,.2f} |")
    above = sum(1 for s in setups if s["vwap_at_signal"] is not None and s["vwap_at_signal"] >= s["entry_raw"])
    out += ["", f"setups (traps in window): {len(setups)}; VWAP at the trap bar >= entry open: {above}"]
    return "\n".join(out) + "\n"


def run(db: str, out_dir: Path, start: Date = LOAD_START, end: Date = DEV_LAST_DAY) -> str:
    out = out_dir / OUT_NAME
    if out.exists():
        raise SystemExit(f"{out} already exists; refusing to overwrite")
    data = DataLoader(db, start, end).load((SYMBOL,))
    days = trading_days(start, end)
    qqq = prepare_symbol(SYMBOL, data[SYMBOL].regular, days)
    result, setups = run_backtest(qqq, days)
    rep = BacktestReporter(result)
    legs = leg_counts(result.trades)
    provenance = {
        "git_head": git("rev-parse", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")),
        "config_name": "FALSE_BREAKOUT_FADE_V1",
        "spec_path": str(SPEC.relative_to(ROOT)),
        "spec_sha256": hashlib.sha256(SPEC.read_bytes()).hexdigest(),
        "db_path": str(db),
        "load_window": [start.isoformat(), end.isoformat()],
        "symbols": [SYMBOL],
        "rows_read": {SYMBOL: data[SYMBOL].row_count},
        "scored_start": result.days[0].day if result.days else None,
    }
    doc = {
        "status": "exploratory, not a decision basis (spec §0)",
        "provenance": provenance,
        "metrics": {**rep.metrics(), "exit_legs": legs},
        "days": [asdict(d) for d in result.days],
        "trades": [asdict(t) for t in result.trades],
        "setups": setups,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return rep.markdown() + extra_markdown(result, legs, setups)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="path to market_history.sqlite")
    ap.add_argument("--out-dir", required=True, type=Path)
    args = ap.parse_args(argv)
    print(run(args.db, args.out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
