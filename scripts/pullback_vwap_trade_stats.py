"""Per-trade statistics for exploratory pullback-VWAP result JSON files (spec V3 §4).

Reads only the result JSON written by scripts/explore_pullback_vwap_v0.py (no price data) and prints, per file:
net P&L per trade mean / sd / t, bootstrap P(mean <= 0) (fixed seed and resample count), gross P&L before costs,
reference-cost P&L, risk USD distribution (shares x risk_per_share) and the share reaching USD 20, entry notional,
and the prompt cost (slippage 0.02 x 2 + commission) as a share of notional. Exploratory, not a decision basis.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from pathlib import Path

SEED = 20261013
RESAMPLES = 100_000
RISK_TARGET = 19.99  # USD 20 within rounding


def trade_stats(doc: dict, seed: int = SEED, resamples: int = RESAMPLES) -> dict:
    tr = doc["trades"]
    n = len(tr)
    if n == 0:
        return {"n": 0}
    net = [t["net_pnl"] for t in tr]
    gross = [sum(t["side"] * (leg["raw"] - t["entry_raw"]) * leg["shares"] for leg in t["legs"]) for t in tr]
    gross_pct = [g / (t["shares"] * t["entry_raw"]) for g, t in zip(gross, tr)]
    risk = sorted(t["shares"] * t["risk_per_share"] for t in tr)
    notional = [t["shares"] * t["entry_fill"] for t in tr]
    cost_pct = [(t["commission"] + 0.04 * t["shares"]) / (t["shares"] * t["entry_fill"]) for t in tr]
    mean = statistics.mean(net)
    sd = statistics.stdev(net) if n > 1 else None
    rng = random.Random(seed)
    le0 = sum(statistics.mean(rng.choices(net, k=n)) <= 0 for _ in range(resamples))
    q = statistics.quantiles(risk, n=4) if n > 1 else [risk[0]] * 3
    return {
        "n": n, "net_sum": sum(net), "net_mean": mean, "net_sd": sd,
        "t": mean / (sd / math.sqrt(n)) if sd else None,
        "bootstrap_p_mean_le_0": (le0 + 1) / (resamples + 1), "seed": seed, "resamples": resamples,
        "gross_sum": sum(gross), "gross_pct_mean": statistics.mean(gross_pct),
        "ref_sum": sum(t["ref_pnl"] for t in tr),
        "risk_min": risk[0], "risk_p25": q[0], "risk_median": statistics.median(risk), "risk_p75": q[2],
        "risk_max": risk[-1], "risk_at_target_share": sum(r >= RISK_TARGET for r in risk) / n,
        "notional_median": statistics.median(notional), "prompt_cost_pct_median": statistics.median(cost_pct),
    }


def _f(x, fmt: str) -> str:
    return "n/a" if x is None else format(x, fmt)


def render(name: str, s: dict) -> str:
    if s["n"] == 0:
        return f"| {name} | 0 |" + " n/a |" * 13
    return (f"| {name} | {s['n']} | {_f(s['net_sum'], ',.2f')} | {_f(s['net_mean'], '.3f')} | {_f(s['net_sd'], '.3f')} | "
            f"{_f(s['t'], '.2f')} | {s['bootstrap_p_mean_le_0']:.4f} | {s['gross_sum']:,.2f} | "
            f"{s['gross_pct_mean'] * 100:.4f}% | {s['ref_sum']:,.2f} | "
            f"{s['risk_min']:.2f} / {s['risk_p25']:.2f} / {s['risk_median']:.2f} / {s['risk_p75']:.2f} / "
            f"{s['risk_max']:.2f} | {s['risk_at_target_share'] * 100:.1f}% | {s['notional_median']:,.0f} | "
            f"{s['prompt_cost_pct_median'] * 100:.3f}% |")


HEADER = ("| file | trades | net sum (USD) | net mean | net sd | t | bootstrap P(mean<=0) | gross sum (USD) | "
          "gross mean % | ref-cost sum (USD) | risk USD min / p25 / median / p75 / max | risk >= USD 20 | "
          "notional median (USD) | prompt cost / notional (median) |")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("results", nargs="+", type=Path)
    args = ap.parse_args(argv)
    print(f"seed {SEED}, resamples {RESAMPLES:,} (exploratory, not a decision basis)\n")
    print(HEADER)
    print("|" + "---|" * 14)
    for p in args.results:
        print(render(p.name, trade_stats(json.loads(p.read_text(encoding="utf-8")))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
