"""Trade overlap between two exploratory pullback-VWAP result JSON files for one symbol (spec V2 §3).

Counts the trades of file B for SYMBOL whose (day, entry_minute) also appears among file A's trades for SYMBOL, and
prints the risk in USD (shares x risk_per_share) median / max of file B's trades for SYMBOL. Reads only the result
JSON files (no price data). Exploratory, not a decision basis.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def _num(x) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


def overlap(a: dict, b: dict, symbol: str) -> dict:
    keys_a = {(t["day"], t["entry_minute"]) for t in a["trades"] if t["symbol"] == symbol}
    tb = [t for t in b["trades"] if t["symbol"] == symbol]
    risk = [t["shares"] * t["risk_per_share"] for t in tb]
    return {
        "symbol": symbol,
        "a_trades": sum(1 for t in a["trades"] if t["symbol"] == symbol),
        "b_trades": len(tb),
        "b_shared_with_a": sum(1 for t in tb if (t["day"], t["entry_minute"]) in keys_a),
        "b_risk_usd_median": statistics.median(risk) if risk else None,
        "b_risk_usd_max": max(risk) if risk else None,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("a", type=Path, help="result JSON A (reference)")
    ap.add_argument("b", type=Path, help="result JSON B")
    ap.add_argument("symbol")
    args = ap.parse_args(argv)
    a, b = (json.loads(p.read_text(encoding="utf-8")) for p in (args.a, args.b))
    r = overlap(a, b, args.symbol)
    print(f"symbol {r['symbol']}: A ({args.a.name}) {r['a_trades']} trades, B ({args.b.name}) {r['b_trades']} trades")
    print(f"B trades sharing (day, entry_minute) with A: {r['b_shared_with_a']} / {r['b_trades']}")
    print(f"B risk USD (shares x risk_per_share): median {_num(r['b_risk_usd_median'])}, "
          f"max {_num(r['b_risk_usd_max'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
