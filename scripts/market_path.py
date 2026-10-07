"""Price path of one US session from the R1 shadow REST candle log, for the daily journal (read-only).

Prints open, high/low (1-minute closes) with times, close, change vs. the prior close and 30-minute
segments with volume for QQQ/TQQQ/SQQQ. The reasons for the moves are researched and written by hand.

usage: python scripts/market_path.py --date 2026-10-06 [--out-root <checkout>]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
NY = ZoneInfo("America/New_York")
SYMBOLS = ("QQQ", "TQQQ", "SQQQ")


def load_bars(candles_path: Path) -> dict[str, dict[str, tuple[float, float, float]]]:
    """Completed regular-session bars keyed by ET start minute; the last fetch of a bar wins."""
    bars: dict[str, dict[str, tuple[float, float, float]]] = defaultdict(dict)
    for line in candles_path.open():
        row = json.loads(line)
        row = row.get("data", row)
        fetched = datetime.fromisoformat(row["fetched"])
        for c in row["candles"]:
            end = datetime.fromisoformat(c["timestamp"])
            if end > fetched:  # still in progress when fetched
                continue
            start = (end - timedelta(minutes=1)).astimezone(NY).strftime("%H:%M")
            if "09:30" <= start <= "15:59":
                bars[row["symbol"]][start] = (float(c["openPrice"]), float(c["closePrice"]), float(c["volume"]))
    return bars


def summarize(b: dict[str, tuple[float, float, float]], prev_close: float | None) -> dict:
    ks = sorted(b)
    closes = [(k, b[k][1]) for k in ks]
    hi = max(closes, key=lambda x: x[1])
    lo = min(closes, key=lambda x: x[1])
    pct = (lambda p: round((p / prev_close - 1) * 100, 2)) if prev_close else (lambda p: None)
    segs = defaultdict(list)
    for k in ks:
        h, m = map(int, k.split(":"))
        segs[f"{h:02d}:{m // 30 * 30:02d}"].append(b[k])
    return {
        "minutes": len(ks), "open": b[ks[0]][0], "close": closes[-1][1], "prev_close": prev_close,
        "close_vs_prev_pct": pct(closes[-1][1]),
        "high_close": {"t": hi[0], "price": hi[1], "vs_prev_pct": pct(hi[1])},
        "low_close": {"t": lo[0], "price": lo[1], "vs_prev_pct": pct(lo[1])},
        "segments": [{"t": s, "start": v[0][0], "end": v[-1][1], "chg_pct": round((v[-1][1] / v[0][0] - 1) * 100, 2),
                      "vs_prev_pct": pct(v[-1][1]), "volume": sum(x[2] for x in v)} for s, v in segs.items()],
    }


def prior_closes(db: Path, prev_day: str) -> dict[str, float]:
    """Last regular-session close of each symbol on `prev_day`, via Paper's read-only bar loader."""
    from src.daytrade_paper.store import load_bars as paper_bars

    regular = paper_bars(db, prev_day)["regular"]
    return {s: regular[s][-1].close for s in SYMBOLS if regular.get(s)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", required=True)
    p.add_argument("--out-root", type=Path, default=ROOT)
    p.add_argument("--db", type=Path, help="market_history.sqlite (default <out-root>/data/market_history.sqlite)")
    args = p.parse_args(argv)
    d = args.out_root / "reports" / "daytrade_shadow" / args.date
    candles = d / "candles.jsonl"
    if not candles.exists():
        print(f"no shadow candle log: {candles}", file=sys.stderr)
        return 2
    prev: dict[str, float] = {}
    prev_source = "unavailable"
    result = d / "result.json"
    if result.exists():
        inputs = json.loads(result.read_text()).get("inputs", {})
        db = args.db or args.out_root / "data" / "market_history.sqlite"
        if inputs.get("prev_session") and db.exists():
            prev = prior_closes(db, inputs["prev_session"])
            prev_source = f"DB regular close {inputs['prev_session']}"
            if inputs.get("prev_close") is not None and abs(prev.get("QQQ", 0) - inputs["prev_close"]) > 1e-6:
                print(f"QQQ prior close differs: DB {prev.get('QQQ')} vs shadow {inputs['prev_close']}", file=sys.stderr)
                return 1
    bars = load_bars(candles)
    out = {s: summarize(bars[s], prev.get(s)) for s in SYMBOLS if bars.get(s)}
    print(json.dumps({"date": args.date, "basis": "Toss REST 1m, high/low are 1-minute closes",
                      "prev_close_source": prev_source, **out}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
