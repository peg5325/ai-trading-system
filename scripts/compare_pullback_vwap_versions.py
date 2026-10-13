"""Compare 2+ exploratory pullback-VWAP result JSON files as Markdown (spec V1 §4, spec V2 §3).

Reads only the JSON files written by scripts/explore_pullback_vwap_v0.py (no price data). Numbers are copied from
each file's `metrics`; rounding happens only in this output. Exploratory, not a decision basis.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _num(x) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.2f}%"


YEARS = ("2023", "2024", "2025", "2026")


def label(doc: dict, path: Path) -> str:
    """provenance.config_name; the original V0 result.json predates that field, so a missing name means V0."""
    return (doc.get("provenance") or {}).get("config_name") or "V0"


def _side_table(title: str, rows: dict) -> list[str]:
    out = ["", f"#### {title}", "", "| key | trades | win rate | net P&L (USD) | PF |", "|---|---|---|---|---|"]
    for k, st in rows.items():
        out.append(f"| {k} | {st['trades']} | {_pct(st['win_rate'])} | {_num(st['net_pnl'])} | "
                   f"{_num(st['profit_factor'])} |")
    return out


def compare(paths: list[Path]) -> str:
    docs = [(p, json.loads(p.read_text(encoding="utf-8"))) for p in paths]
    out = ["## Version comparison (exploratory, not a decision basis)", "",
           ("| config | file | period | trades | win rate | PF | net P&L (USD) | cumulative return | CAGR | "
            "MDD (USD) | MDD (%) | longest drawdown (trading days) | ref final equity (USD) | ref return | ref PF |"),
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for p, doc in docs:
        m = doc["metrics"]
        if not m.get("period"):
            out.append(f"| {label(doc, p)} | {p.name} | no scored days |" + " |" * 12)
            continue
        rc = m["reference_cost"]
        out.append(
            f"| {label(doc, p)} | {p.name} | {m['period']['first_day']} ~ {m['period']['last_day']} | "
            f"{m['trade_count']} | {_pct(m['trade_win_rate'])} | {_num(m['trade_profit_factor'])} | "
            f"{_num(m['trade_net_pnl'])} | {_pct(m['cumulative_return'])} | {_pct(m['cagr'])} | "
            f"{_num(m['mdd_usd'])} | {_pct(m['mdd_pct'])} | {m['longest_drawdown_trading_days']} | "
            f"{_num(rc['final_equity'])} | {_pct(rc['cumulative_return'])} | {_num(rc['profit_factor'])} |")
    out += ["", "## By year", "",
            "| config | year | trades | net P&L (USD) | return | ref return |", "|---|---|---|---|---|---|"]
    for p, doc in docs:
        per_year = doc["metrics"].get("per_year") or {}
        for y in YEARS:
            v = per_year.get(y)
            if v is None:
                out.append(f"| {label(doc, p)} | {y} | n/a | n/a | n/a | n/a |")
                continue
            out.append(f"| {label(doc, p)} | {y} | {v['trades_trades']} | {_num(v['trades_net_pnl'])} | "
                       f"{_pct(v['return'])} | {_pct(v['ref_return'])} |")
    for p, doc in docs:
        m = doc["metrics"]
        if not m.get("period"):
            continue
        out += ["", f"### {label(doc, p)} ({p.name})"]
        out += _side_table("By symbol", m["per_symbol"])
        out += _side_table("Long / short", m["long_short"])
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("results", nargs="+", type=Path, help="result JSON files (2 or more)")
    args = ap.parse_args(argv)
    if len(args.results) < 2:
        ap.error("give at least 2 result JSON files")
    print(compare(args.results), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
