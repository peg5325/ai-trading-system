"""Exploratory ORB + VWAP pullback backtest, USD 2,000 (spec docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V0.md).

Exploratory only (spec §0): not a preregistration, not a decision basis. Reads 1m bars only through
research_load(purpose="performance", track=intraday, market="US") for 2023-01-01 ~ 2026-01-20 and refuses any
request reaching 2026-01-21 00:00 ET. Prints the §4 tables and writes the result JSON (write-once) to --out-dir.

--config V0 (default, result.json) reproduces V0; V1 / V1_LONGONLY follow docs/research/
EXPLORE_ORB_VWAP_PULLBACK_2000USD_V1.md and write EXPLORE_ORB_VWAP_PULLBACK_2000USD_V1[_LONGONLY].json.
V1 results are in-sample post-hoc adjustments (spec V1 §0). V2_QQQ follows docs/research/
EXPLORE_ORB_VWAP_PULLBACK_2000USD_V2.md (QQQ only, no SPY loaded) and writes EXPLORE_ORB_VWAP_PULLBACK_2000USD_V2_QQQ.json.
V3_QQQ / V3_TQQQ follow docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V3.md (V2_QQQ without the RVOL / gap screener,
one symbol each) and write EXPLORE_ORB_VWAP_PULLBACK_2000USD_V3_QQQ.json / _V3_TQQQ.json.
V4_RVOL_SNIPER follows docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V4_SNIPER.md (seven stocks, the highest-RVOL
qualifying one per day; QQQ loaded for the buy-and-hold comparison only) and writes
EXPLORE_ORB_VWAP_PULLBACK_2000USD_V4_SNIPER.json.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from dataclasses import asdict
from datetime import date as Date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.explore_orb_vwap_v0.config import (
    CLI_PRESETS,
    V0,
    V4_BENCHMARK,
    EngineConfig,
    EngineConfigV4,
)
from src.explore_orb_vwap_v0.data_loader import (
    DEV_LAST_DAY,
    LOAD_START,
    DataLoader,
)
from src.explore_orb_vwap_v0.reporter import BacktestReporter
from src.explore_orb_vwap_v0.strategy import run_backtest
from src.explore_orb_vwap_v0.universe import prepare_symbol
from src.history.exchange_calendar import trading_days

SPEC = ROOT / "docs" / "research" / "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V0.md"
SPEC_V1 = ROOT / "docs" / "research" / "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V1.md"
SPEC_V2 = ROOT / "docs" / "research" / "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V2.md"
SPEC_V3 = ROOT / "docs" / "research" / "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V3.md"
SPEC_V4 = ROOT / "docs" / "research" / "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V4_SNIPER.md"
DEFAULT_OUT_NAME = {
    "V0": "result.json",
    "V1": "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V1.json",
    "V1_LONGONLY": "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V1_LONGONLY.json",
    "V2_QQQ": "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V2_QQQ.json",
    "V3_QQQ": "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V3_QQQ.json",
    "V3_TQQQ": "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V3_TQQQ.json",
    "V4_RVOL_SNIPER": "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V4_SNIPER.json",
}
SPECS = {"V0": SPEC, "V1": SPEC_V1, "V1_LONGONLY": SPEC_V1, "V2_QQQ": SPEC_V2, "V3_QQQ": SPEC_V3,
         "V3_TQQQ": SPEC_V3, "V4_RVOL_SNIPER": SPEC_V4}


def git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def run(db: str, out_dir: Path, symbols=None, start: Date = LOAD_START, end: Date = DEV_LAST_DAY,
        config: EngineConfig = V0, out_name: str | None = None) -> str:
    symbols = tuple(symbols) if symbols is not None else config.universe
    if out_name is not None and Path(out_name).name != out_name:
        raise SystemExit(f"--out-name must be a bare file name, got {out_name!r}")
    out = out_dir / (out_name or DEFAULT_OUT_NAME[config.name])
    if out.exists():
        raise SystemExit(f"{out} already exists; refusing to overwrite")
    v4 = isinstance(config, EngineConfigV4)
    if v4 and V4_BENCHMARK in symbols:
        raise SystemExit(f"{V4_BENCHMARK} is the V4 benchmark and cannot be a candidate (spec V4 §3)")
    bench = (V4_BENCHMARK,) if v4 else ()
    loader = DataLoader(db, start, end)
    data = loader.load(symbols + bench)
    days = trading_days(start, end)
    split = config.split_gap if v4 else None
    prepared = {s: prepare_symbol(s, data[s].regular, days, split_gap=split) for s in symbols}
    benchmark = prepare_symbol(bench[0], data[bench[0]].regular, days) if bench else None
    result = run_backtest(prepared, days, config=config, benchmark=benchmark)
    reporter = BacktestReporter(result)
    spec = SPECS.get(config.name, SPEC_V1)
    provenance = {
        "git_head": git("rev-parse", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")),
        "config_name": config.name,
        "config": asdict(config),
        "spec_path": str(spec.relative_to(ROOT)),
        "spec_sha256": hashlib.sha256(spec.read_bytes()).hexdigest(),
        "db_path": str(db),
        "load_window": [start.isoformat(), end.isoformat()],
        "symbols": list(symbols),
        "rows_read": {s: data[s].row_count for s in symbols + bench},
    }
    if bench:
        provenance["benchmark_only"] = list(bench)
    if config.rs_reference is None:
        provenance["scored_start_rule"] = ("first trading day on which a traded symbol is complete with prior close, "
                                           + ("20-session RVOL base, " if config.rvol_min is not None else "")
                                           + "warm 5m ATR/EMA"
                                           + (" and prior daily EMA20/ATR14" if config.daily_filters else "")
                                           + "; no RS reference (spec V2 §1.3)"
                                           + ("" if config.rvol_min is not None
                                              else "; RVOL base not required (spec V3 §2)")
                                           + ("; any of the universe stocks (spec V4 §3)" if v4 else ""))
        provenance["scored_start"] = (result.days[0].day if result.days else None)
    out_dir.mkdir(parents=True, exist_ok=True)
    reporter.to_json(out, provenance)
    return reporter.markdown()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="path to market_history.sqlite")
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--config", choices=sorted(CLI_PRESETS), default="V0")
    ap.add_argument("--out-name", default=None,
                    help="result JSON file name (default: result.json for V0, "
                         "EXPLORE_ORB_VWAP_PULLBACK_2000USD_<config>.json for the V1 / V2 / V3 configs, "
                         "EXPLORE_ORB_VWAP_PULLBACK_2000USD_V4_SNIPER.json for V4_RVOL_SNIPER)")
    args = ap.parse_args(argv)
    print(run(args.db, args.out_dir, config=CLI_PRESETS[args.config], out_name=args.out_name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
