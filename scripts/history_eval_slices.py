"""통합 저장소 분리 평가 슬라이스 리포트 — 시장(US/KR)·종목별 커버리지 + out-of-time split 지점.

read-only 집계(Toss API 미호출). 공식 평가는 chronological/embargo/LOSO 만 허용하며
random split 은 모델선택 경로에서 금지(계약 §3-7).

사용:
  ./.venv/bin/python scripts/history_eval_slices.py --db data/market_history.sqlite
  ./.venv/bin/python scripts/history_eval_slices.py --db data/market_history.sqlite --ratio 0.7 --json
설계: docs/MARKET_HISTORY_BACKFILL_DESIGN.md §6
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history import OFFICIAL_SPLIT_METHODS, list_slices, load_candles  # noqa: E402
from src.history.evaluation import chronological_split  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="market_history 분리 평가 슬라이스(read-only)")
    ap.add_argument("--db", default="data/market_history.sqlite")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--ratio", type=float, default=0.7, help="out-of-time train 비율")
    ap.add_argument("--embargo-bars", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    slices = list_slices(args.db, args.interval)
    out = []
    for s in slices:
        candles = load_candles(args.db, s.symbol, args.interval)
        train, test = chronological_split(candles, ratio=args.ratio,
                                          embargo_bars=args.embargo_bars)
        out.append({
            "market": s.market, "symbol": s.symbol, "interval": s.interval,
            "bars": s.bars, "days": s.days, "earliest": s.earliest, "latest": s.latest,
            "oot_train_bars": len(train), "oot_test_bars": len(test),
            "oot_boundary": (test[0].ts_utc.isoformat() if test else None),
        })

    if args.json:
        print(json.dumps({"official_methods": list(OFFICIAL_SPLIT_METHODS),
                          "slices": out}, ensure_ascii=False, indent=2))
        return 0

    print(f"=== 분리 평가 슬라이스 ({args.db}) interval={args.interval} ===")
    print(f"공식 split: {OFFICIAL_SPLIT_METHODS}  (random 은 모델선택 경로 금지)")
    cur_market = None
    for r in out:
        if r["market"] != cur_market:
            cur_market = r["market"]
            print(f"\n## 시장 {cur_market}")
        print(f"  {r['symbol']:<8} [{r['interval']}] bars={r['bars']} days={r['days']}  "
              f"{r['earliest']} ~ {r['latest']}")
        print(f"    out-of-time(ratio={args.ratio}, embargo={args.embargo_bars}): "
              f"train={r['oot_train_bars']} test={r['oot_test_bars']} "
              f"경계={r['oot_boundary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
