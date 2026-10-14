"""V3C asymmetric trend swing, three configs (spec docs/research/EXPLORE_V3C_ASYM_SWING_2000USD.md).

Exploratory only (post-hoc corrections of V3B; outside the Charter): not a decision basis. Reads QQQ 1m bars only
through the ORB exploration loader (research_load; refuses 2026-01-21 and later), runs V3C_REGIME_ASYM /
V3C_TACTICAL_SHORT / V3C_STRICT_RUNNER on the continuous stream, writes one JSON per config and
EXPLORE_V3C_ASYM_SWING.json (write-once, all built before any write) and prints the spec §6 tables.
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
from src.explore_orb_vwap_v0.reporter import BacktestReporter, drawdown
from src.explore_v3_asym_swing.engine import SCORED_START, SYMBOL, run
from src.explore_v3_asym_swing.engine_v3c import CONFIGS_C, EngineC
from src.explore_v3_asym_swing.indicators import build_stream
from src.history.exchange_calendar import trading_days

SPEC = ROOT / "docs" / "research" / "EXPLORE_V3C_ASYM_SWING_2000USD.md"
PREFIX = "EXPLORE_V3C_ASYM_SWING"
COST_WALL = 0.002
STATUS = "exploratory, post-hoc (third pass on the same window), outside the Charter, not a decision basis (spec §0)"
EXIT_REASONS = ("hard_stop", "ema_cross", "lock_1", "lock_2", "lock_3", "st_flip", "eod_bull_short", "gap_eod_bull_short",
                "end_of_data")
STAGES = ("gear1", "gear2_stage1", "gear2_stage2", "gear2_stage3")
SKIP_KEYS = ("chase", "no_rebound_confirm", "regime_short_block", "cycle_cap", "cooldown", "stale_regime")


def git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def gross_pct(t) -> float:
    return t.side * (t.legs[-1].raw - t.entry_raw) / t.entry_raw


def group(trades) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    net = [t.net_pnl for t in trades]
    ref = [t.ref_pnl for t in trades]
    return {"trades": n, "win_rate": sum(x > 0 for x in net) / n, "net_sum": sum(net), "net_mean": sum(net) / n,
            "ref_win_rate": sum(x > 0 for x in ref) / n, "ref_sum": sum(ref), "ref_mean": sum(ref) / n,
            "gross_mean_pct": statistics.mean(gross_pct(t) for t in trades),
            "hold_hours_mean": statistics.mean(t.hold_minutes for t in trades) / 60.0,
            "hold_minutes_median": statistics.median(t.hold_minutes for t in trades),
            "hold_calendar_days_mean": statistics.mean(
                (Date.fromisoformat(t.exit_day) - Date.fromisoformat(t.day)).days for t in trades)}


def buy_hold(stream) -> dict:
    s = [m for m in stream if m.day >= SCORED_START]
    return {"first_day": s[0].day, "last_day": s[-1].day, "open": s[0].o, "close": s[-1].c,
            "return": s[-1].c / s[0].o - 1.0}


def summary(res) -> dict:
    m = BacktestReporter(res).metrics()
    tr = res.trades
    net = [t.net_pnl for t in tr]
    n = len(net)
    sd = statistics.stdev(net) if n > 1 else None
    t_stat = statistics.mean(net) / (sd / math.sqrt(n)) if sd else None
    gross = statistics.mean(gross_pct(t) for t in tr) if n else None
    ref_cum = (m.get("reference_cost") or {}).get("cumulative_return")
    crit = {"a_gross_mean_pct": gross, "a_pass": gross is not None and gross >= COST_WALL,
            "b_ref_cumulative": ref_cum, "b_pass": ref_cum is not None and ref_cum > 0,
            "c_t": t_stat, "c_pass": t_stat is not None and t_stat >= 2.0}
    crit["supported"] = crit["a_pass"] and crit["b_pass"] and crit["c_pass"]
    years = {}
    for y, v in (m.get("per_year") or {}).items():
        yd = [d for d in res.days if d.day[:4] == y]
        _, mdd_pct, _ = drawdown(yd[0].start_equity, [d.end_equity for d in yd])
        _, rmdd_pct, _ = drawdown(v["ref_start_equity"], [d.ref_end_equity for d in yd])
        yt = [t for t in tr if t.day[:4] == y]
        years[y] = {"trades": len(yt), "net": sum(t.net_pnl for t in yt), "return": v["return"],
                    "ref_net": sum(t.ref_pnl for t in yt), "ref_return": v["ref_return"],
                    "mdd_pct": mdd_pct, "ref_mdd_pct": rmdd_pct}
    return {
        "metrics": m, "criteria": crit,
        "stages": {g: group([t for t in tr if t.stage == k]) for k, g in enumerate(STAGES)},
        "exits": {r: group([t for t in tr if t.exit_reason == r]) for r in EXIT_REASONS},
        "sides": {"long": group([t for t in tr if t.side > 0]), "short": group([t for t in tr if t.side < 0]),
                  "short_bull_regime": group([t for t in tr if t.bull_short])},
        "years": years, "skips": res.skips,
        "signal_skips": {k: res.skips.get(k, 0) for k in SKIP_KEYS},
    }


def _p(x, d=2) -> str:
    return "n/a" if x is None else f"{x * 100:.{d}f}%"


def _n(x) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


def _group_rows(sums: dict, key: str, labels) -> list[str]:
    out = [("| config | group | trades | share | win | net sum | net mean | ref win | ref sum | ref mean | pre-cost mean | "
            "hold mean (h) | hold median (min) | exit - entry (calendar days) |"),
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, s in sums.items():
        total = s["metrics"]["trade_count"] or 1
        for g in labels:
            v = s[key][g]
            if not v["trades"]:
                out.append(f"| {name} | {g} | 0 | 0.00% |" + " n/a |" * 10)
                continue
            out.append(f"| {name} | {g} | {v['trades']} | {_p(v['trades'] / total)} | {_p(v['win_rate'])} | "
                       f"{_n(v['net_sum'])} | {_n(v['net_mean'])} | {_p(v['ref_win_rate'])} | {_n(v['ref_sum'])} | "
                       f"{_n(v['ref_mean'])} | {_p(v['gross_mean_pct'], 4)} | {v['hold_hours_mean']:,.2f} | "
                       f"{v['hold_minutes_median']:,.1f} | {v['hold_calendar_days_mean']:,.2f} |")
    return out


def markdown(sums: dict, bh: dict) -> str:
    out = ["## Main table (exploratory, post-hoc; outside the Charter)", "",
           ("| config | trades | win | PF | net (USD) | return | CAGR | MDD % | ref net (USD) | ref return | ref CAGR | ref PF | "
            "ref MDD % | pre-cost / trade | t | criteria |"),
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, s in sums.items():
        m, c = s["metrics"], s["criteria"]
        if not m.get("trade_count"):
            out.append(f"| {name} | 0 |" + " n/a |" * 14)
            continue
        rc = m["reference_cost"]
        out.append(f"| {name} | {m['trade_count']} | {_p(m['trade_win_rate'])} | {_n(m['trade_profit_factor'])} | "
                   f"{_n(m['trade_net_pnl'])} | {_p(m['cumulative_return'])} | {_p(m['cagr'])} | {_p(m['mdd_pct'])} | "
                   f"{_n(rc['final_equity'] - m['start_equity'])} | {_p(rc['cumulative_return'])} | {_p(rc['cagr'])} | "
                   f"{_n(rc['profit_factor'])} | {_p(rc['mdd_pct'])} | {_p(c['a_gross_mean_pct'], 4)} | {_n(c['c_t'])} | "
                   f"{'supported' if c['supported'] else 'FAIL'} |")
    out.append(f"\nQQQ 1x hold {bh['first_day']} open ~ {bh['last_day']} close: {_p(bh['return'])}")
    out += ["", "## Exit reasons"] + [""] + _group_rows(sums, "exits", EXIT_REASONS)
    out += ["", "## Stages (stage at exit)"] + [""] + _group_rows(sums, "stages", STAGES)
    out += ["", "## Long / short"] + [""] + _group_rows(sums, "sides", ("long", "short", "short_bull_regime"))
    out += ["", "## By year (MDD within the year, the year's opening equity as the first peak)", "",
            "| config | year | trades | net (USD) | return | MDD % | ref net (USD) | ref return | ref MDD % |",
            "|---|---|---|---|---|---|---|---|---|"]
    for name, s in sums.items():
        for y, v in s["years"].items():
            out.append(f"| {name} | {y} | {v['trades']} | {_n(v['net'])} | {_p(v['return'])} | {_p(v['mdd_pct'])} | "
                       f"{_n(v['ref_net'])} | {_p(v['ref_return'])} | {_p(v['ref_mdd_pct'])} |")
    out += ["", "## Skipped signals (first blocking condition per signal, spec §4.5)", "",
            "| config | " + " | ".join(SKIP_KEYS) + " | all counters |", "|---|" + "---|" * (len(SKIP_KEYS) + 1)]
    out += [f"| {name} | " + " | ".join(str(s["signal_skips"][k]) for k in SKIP_KEYS) + f" | {s['skips']} |"
            for name, s in sums.items()]
    return "\n".join(out) + "\n"


def run_all(db: str, out_dir: Path, start: Date = LOAD_START, end: Date = DEV_LAST_DAY) -> str:
    combined = out_dir / f"{PREFIX}.json"
    targets = [combined] + [out_dir / f"{PREFIX}_{c.name}.json" for c in CONFIGS_C]
    for p in targets:
        if p.exists():
            raise SystemExit(f"{p} already exists; refusing to overwrite")
    data = DataLoader(db, start, end).load((SYMBOL,))
    stream = build_stream(data[SYMBOL].regular)
    cal = [d.isoformat() for d in trading_days(start, end)]
    bh = buy_hold(stream)
    provenance = {
        "git_head": git("rev-parse", "HEAD"), "git_dirty": bool(git("status", "--porcelain")),
        "spec_path": str(SPEC.relative_to(ROOT)), "spec_sha256": hashlib.sha256(SPEC.read_bytes()).hexdigest(),
        "db_path": str(db), "load_window": [start.isoformat(), end.isoformat()], "symbols": [SYMBOL],
        "rows_read": {SYMBOL: data[SYMBOL].row_count}, "stream_bars": len(stream),
        "stream_days": len({m.day for m in stream}), "scored_start": SCORED_START,
    }
    texts: dict[Path, str] = {}
    sums = {}
    for c in CONFIGS_C:
        res = run(stream, c, cal, engine_cls=EngineC)
        s = summary(res)
        sums[c.name] = s
        doc = {"status": STATUS,
               "provenance": {**provenance, "config_name": c.name, "config": asdict(c)},
               "metrics": {**s["metrics"], "qqq_buy_hold": bh},
               **{k: s[k] for k in ("stages", "exits", "sides", "years", "criteria", "skips", "signal_skips")},
               "days": [asdict(d) for d in res.days], "trades": [asdict(t) for t in res.trades]}
        texts[out_dir / f"{PREFIX}_{c.name}.json"] = json.dumps(doc, indent=1, ensure_ascii=False) + "\n"
    combined_doc = {"status": STATUS,
                    "provenance": provenance, "qqq_buy_hold": bh,
                    "configs": {n: {k: v for k, v in s.items() if k != "metrics"} | {"metrics": {
                        k: s["metrics"].get(k) for k in (
                            "period", "total_trading_days", "trade_days", "trade_count", "trade_win_rate",
                            "trade_profit_factor", "trade_net_pnl", "cumulative_return", "cagr", "mdd_usd", "mdd_pct",
                            "longest_drawdown_trading_days", "exit_reasons", "day_status", "reference_cost",
                            "long_short")}} for n, s in sums.items()}}
    texts[combined] = json.dumps(combined_doc, indent=1, ensure_ascii=False) + "\n"
    md = markdown(sums, bh)
    out_dir.mkdir(parents=True, exist_ok=True)
    for p, text in texts.items():
        p.write_text(text, encoding="utf-8")
    return md


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--out-dir", required=True, type=Path)
    args = ap.parse_args(argv)
    print(run_all(args.db, args.out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
