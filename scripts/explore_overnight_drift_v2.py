"""Overnight drift V2 ablation (spec docs/research/EXPLORE_OVERNIGHT_DRIFT_2000USD_V2.md).

Exploratory only (post-hoc configs, outside the Charter's same-day scope): not a decision basis. Reads QQQ 1m bars
only through the ORB exploration loader (research_load; refuses 2026-01-21 and later), runs the five configs on the
same scored days, checks that V1_BASE and NO_FILTER_BENCH reproduce the stored V1 result (--v1-json; on any mismatch
nothing is written and the exit code is 2), then writes one JSON per config plus the combined
EXPLORE_OVERNIGHT_DRIFT_2000USD_V2.json (write-once) and prints the §5 tables.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
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
from src.explore_overnight_drift_v1.ablation import (
    CONFIGS,
    gross_pct,
    paired_exit_diff,
    run_all,
    year_mdd,
)
from src.explore_overnight_drift_v1.engine import SYMBOL
from src.history.exchange_calendar import trading_days

SPEC = ROOT / "docs" / "research" / "EXPLORE_OVERNIGHT_DRIFT_2000USD_V2.md"
PREFIX = "EXPLORE_OVERNIGHT_DRIFT_2000USD_V2"
COST_WALL = 0.002


def git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def hold_stats(holds) -> dict:
    h = [x.hours for x in holds]
    return {"mean_hours": statistics.mean(h) if h else None, "median_hours": statistics.median(h) if h else None,
            "max_hours": max(h) if h else None}


def reproduction(results: dict, v1: dict) -> list[str]:
    """Spec V2 §4: V1_BASE days / trades / metrics and NO_FILTER_BENCH metrics equal the stored V1 result."""
    bad = []
    res, holds = results["V1_BASE"]
    m = {**BacktestReporter(res).metrics(), "holding": hold_stats(holds)}
    if [asdict(d) for d in res.days] != v1["days"]:
        bad.append("V1_BASE days")
    if [asdict(t) for t in res.trades] != v1["trades"]:
        bad.append("V1_BASE trades")
    if json.loads(json.dumps(m)) != v1["metrics"]:
        bad.append("V1_BASE metrics")
    res, holds = results["NO_FILTER_BENCH"]
    m = {**BacktestReporter(res).metrics(), "holding": hold_stats(holds)}
    if json.loads(json.dumps(m)) != v1["benchmarks"]["no_filter_overnight"]:
        bad.append("NO_FILTER_BENCH metrics")
    return bad


def summary(res, holds) -> dict:
    m = BacktestReporter(res).metrics()
    net = [t.net_pnl for t in res.trades]
    n = len(net)
    sd = statistics.stdev(net) if n > 1 else None
    t_stat = statistics.mean(net) / (sd / math.sqrt(n)) if sd else None
    gross = statistics.mean(gross_pct(t) for t in res.trades) if n else None
    ref = m["reference_cost"]["cumulative_return"] if m.get("period") else None
    rtm = (m["cumulative_return"] / m["mdd_pct"]) if m.get("mdd_pct") else None
    verdict = {"a_gross_mean_pct": gross, "a_pass": gross is not None and gross >= COST_WALL,
               "b_ref_cumulative": ref, "b_pass": ref is not None and ref > 0,
               "c_t": t_stat, "c_pass": t_stat is not None and t_stat >= 2.0}
    verdict["supported"] = verdict["a_pass"] and verdict["b_pass"] and verdict["c_pass"]
    return {"metrics": m, "return_to_mdd": rtm, "year_mdd": year_mdd(res.days, res.start_equity),
            "holding": hold_stats(holds), "criteria": verdict}


def _p(x) -> str:
    return "n/a" if x is None else f"{x * 100:.2f}%"


def _n(x) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


def _p4(x) -> str:
    return "n/a" if x is None else f"{x * 100:.4f}%"


def _diff(a, b):
    return None if a is None or b is None else b - a


def markdown(sums: dict, q1: dict, q2: dict, ema50_not_ready: int) -> str:
    out = ["## Configs (exploratory, post-hoc; spec V2 §0)", "",
           ("| config | traded days | win rate | PF | net P&L (USD) | return | CAGR | MDD (USD) | MDD (%) | return/MDD | "
            "longest DD (days) | ref-cost return | pre-cost / trade | t | criteria |"),
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, s in sums.items():
        m, c = s["metrics"], s["criteria"]
        out.append(f"| {name} | {m['trade_days']} | {_p(m['trade_win_rate'])} | {_n(m['trade_profit_factor'])} | "
                   f"{_n(m['trade_net_pnl'])} | {_p(m['cumulative_return'])} | {_p(m['cagr'])} | {_n(m['mdd_usd'])} | "
                   f"{_p(m['mdd_pct'])} | {_n(s['return_to_mdd'])} | {m['longest_drawdown_trading_days']} | "
                   f"{_p(c['b_ref_cumulative'])} | {_p4(c['a_gross_mean_pct'])} | {_n(c['c_t'])} | "
                   f"{'supported' if c['supported'] else 'FAIL'} |")
    out += ["", "## By year", "", "| config | year | trades | net P&L (USD) | return | year MDD (USD) | year MDD (%) |",
            "|---|---|---|---|---|---|---|"]
    for name, s in sums.items():
        for y, v in s["metrics"]["per_year"].items():
            ym = s["year_mdd"][y]
            out.append(f"| {name} | {y} | {v['trades_trades']} | {_n(v['trades_net_pnl'])} | {_p(v['return'])} | "
                       f"{_n(ym['mdd_usd'])} | {_p(ym['mdd_pct'])} |")
    out += ["", "## Q1 (V1_BASE -> V2A_EMA20_ONLY: open and VWAP filters removed)", "", "| item | V1_BASE | V2A | change |",
            "|---|---|---|---|"]
    for k, lab in (("trades", "trades"), ("return", "return"), ("mdd_pct", "MDD %"), ("gross", "pre-cost / trade")):
        a, b = q1["V1_BASE"][k], q1["V2A_EMA20_ONLY"][k]
        fmt = str if k == "trades" else (_p4 if k == "gross" else _p)
        out.append(f"| {lab} | {fmt(a)} | {fmt(b)} | {fmt(_diff(a, b))} |")
    share = q2["b_better_share"]
    out += ["", "## Q2 (same entries: V2B 09:31 exit vs V2A 09:35 exit / disaster stop)", "",
            (f"pairs {q2['pairs']} (unpaired V2A {q2['unpaired_a']}, V2B {q2['unpaired_b']}); "
             f"mean (09:31 exit - V2A exit) / entry = {_p4(q2['mean_pct'])}, sd {_p4(q2['sd_pct'])}, "
             f"t {_n(q2['t'])}, 09:31 better in {_p(share)} of pairs. "
             "Costs are identical for both exits, so the after-cost difference is the same."),
            "", f"V2C EMA50 not-ready scored days (no entry): {ema50_not_ready}"]
    return "\n".join(out) + "\n"


def run(db: str, out_dir: Path, v1_json: Path, start: Date = LOAD_START, end: Date = DEV_LAST_DAY) -> str:
    combined = out_dir / f"{PREFIX}.json"
    targets = [combined] + [out_dir / f"{PREFIX}_{c.name}.json" for c in CONFIGS]
    for p in targets:
        if p.exists():
            raise SystemExit(f"{p} already exists; refusing to overwrite")
    data = DataLoader(db, start, end).load((SYMBOL,))
    days = trading_days(start, end)
    qqq = prepare_symbol(SYMBOL, data[SYMBOL].regular, days)
    run_out = run_all(qqq, days)
    results = run_out["results"]
    v1 = json.loads(v1_json.read_text(encoding="utf-8"))
    bad = reproduction(results, v1)
    if bad:
        print(f"REPRODUCTION FAILED: {bad}; nothing written (spec V2 §4)", file=sys.stderr)
        raise SystemExit(2)
    sums = {c.name: summary(*results[c.name]) for c in CONFIGS}
    scored = [d.day for d in results["V1_BASE"][0].days]
    ema50_not_ready = sum(1 for d in results["V2C_EMA50_OPEN_EXIT"][0].days if d.status == "daily_not_ready")
    q1 = {n: {"trades": sums[n]["metrics"]["trade_count"], "return": sums[n]["metrics"]["cumulative_return"],
              "mdd_pct": sums[n]["metrics"]["mdd_pct"], "gross": sums[n]["criteria"]["a_gross_mean_pct"]}
          for n in ("V1_BASE", "V2A_EMA20_ONLY")}
    q2 = paired_exit_diff(results["V2A_EMA20_ONLY"][0].trades, results["V2B_EMA20_OPEN_EXIT"][0].trades)
    provenance = {
        "git_head": git("rev-parse", "HEAD"), "git_dirty": bool(git("status", "--porcelain")),
        "spec_path": str(SPEC.relative_to(ROOT)), "spec_sha256": hashlib.sha256(SPEC.read_bytes()).hexdigest(),
        "db_path": str(db), "load_window": [start.isoformat(), end.isoformat()], "symbols": [SYMBOL],
        "rows_read": {SYMBOL: data[SYMBOL].row_count}, "scored_start": scored[0] if scored else None,
        "v1_json": str(v1_json), "v1_json_sha256": hashlib.sha256(v1_json.read_bytes()).hexdigest(),
        "reproduction": "V1_BASE days/trades/metrics and NO_FILTER_BENCH metrics equal the stored V1 result",
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    for c in CONFIGS:
        res, holds = results[c.name]
        doc = {"status": "exploratory, post-hoc, not a decision basis (spec V2 §0)",
               "provenance": {**provenance, "config_name": c.name, "config": asdict(c)},
               "metrics": {**sums[c.name]["metrics"], "holding": sums[c.name]["holding"]},
               "days": [asdict(d) for d in res.days], "trades": [asdict(t) for t in res.trades],
               "holds": [asdict(h) for h in holds]}
        (out_dir / f"{PREFIX}_{c.name}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n",
                                                          encoding="utf-8")
    doc = {"status": "exploratory, post-hoc, not a decision basis (spec V2 §0)", "provenance": provenance,
           "configs": {n: {k: v for k, v in s.items() if k != "metrics"} | {
               "metrics": {k: s["metrics"].get(k) for k in (
                   "period", "total_trading_days", "trade_days", "trade_count", "trade_win_rate",
                   "trade_profit_factor", "trade_net_pnl", "cumulative_return", "cagr", "mdd_usd", "mdd_pct",
                   "longest_drawdown_trading_days", "trade_avg_win", "trade_avg_loss", "exit_reasons",
                   "day_status", "per_year", "reference_cost", "qqq_buy_hold")}} for n, s in sums.items()},
           "q1": q1, "q2": q2, "ema50_not_ready_days": ema50_not_ready}
    combined.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return markdown(sums, q1, q2, ema50_not_ready)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--v1-json", required=True, type=Path, help="stored EXPLORE_OVERNIGHT_DRIFT_2000USD_V1.json")
    args = ap.parse_args(argv)
    print(run(args.db, args.out_dir, args.v1_json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
