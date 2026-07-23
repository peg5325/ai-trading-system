"""통합 저장소 분리 평가 슬라이스 리포트 — 시장(US/KR)·종목별 커버리지 + out-of-time split 지점.

read-only 집계(Toss API 미호출). 공식 평가는 chronological/embargo/LOSO 만 허용하며
random split 은 모델선택 경로에서 금지(계약 §3-7).

캔들 로드는 blind OOS 게이트(`src/history/oos_guard.research_load`)를 경유한다. 이 스크립트는
bars 수·경계 시각만 보고하고 **성과·수익률을 계산하지 않으므로** purpose="coverage" 로 명시
우회한다(§5.1 경계 분리). 성과를 계산하는 소비자는 기본 purpose="performance"(fail-closed)를
써야 하며, `load_candles` 직접 import 는 `tests/test_oos_guard_wiring.py` 가 금지한다.

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

from src.history import OFFICIAL_SPLIT_METHODS, list_slices  # noqa: E402
from src.history.evaluation import chronological_split  # noqa: E402
from src.history.oos_guard import research_load  # noqa: E402


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
        # market 격리 로드(시장 분리 평가 — 동일 티커의 통화/시장 혼재 방지).
        # ★guarded loader(계약 §5.1): 이 스크립트는 성과·수익률을 계산하지 않고 bars 수와
        #   split 경계 시각만 보고하므로 purpose="coverage" 로 명시 우회한다(경계 분리).
        candles = research_load(args.db, s.symbol, args.interval,
                                purpose="coverage", market=s.market)
        row = {
            "market": s.market, "symbol": s.symbol, "interval": s.interval,
            "bars": s.bars, "days": s.days, "earliest": s.earliest, "latest": s.latest,
        }
        try:
            train, test = chronological_split(candles, ratio=args.ratio,
                                              embargo_bars=args.embargo_bars)
            row.update(oot_train_bars=len(train), oot_test_bars=len(test),
                       oot_boundary=test[0].ts_utc.isoformat())
        except ValueError as e:
            row.update(oot_train_bars=None, oot_test_bars=None,
                       oot_boundary=None, oot_note=f"split 불가: {e}")
        out.append(row)

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
        if r.get("oot_note"):
            print(f"    out-of-time(ratio={args.ratio}, embargo={args.embargo_bars}): "
                  f"{r['oot_note']}")
        else:
            print(f"    out-of-time(ratio={args.ratio}, embargo={args.embargo_bars}): "
                  f"train={r['oot_train_bars']} test={r['oot_test_bars']} "
                  f"경계={r['oot_boundary']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
