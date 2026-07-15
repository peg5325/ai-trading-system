"""Offline Shadow Outcome batch — journal(shadow) + candle → forward return 부착 리포트(PR 4-B).

세션 후 read-only 배치. 주문·매매 로직 무관. 런타임 shadow 레코드는 건드리지 않는다.
INCOMPLETE horizon(세션 초과)은 집계에서 자동 제외. price-direction shadow(실 체결 아님).

사용:
  ./.venv/bin/python scripts/shadow_outcomes.py --journal data/journal.sqlite \
      --candles data/market.sqlite --horizons 30,60
  ./.venv/bin/python scripts/shadow_outcomes.py --journal data/journal.sqlite \
      --candles data/market.sqlite --json
설계: docs/DESIGN_DELTA_V2_1.md §2-4
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.journal.sqlite_store import SqliteJournalStore  # noqa: E402
from src.shadow.outcome import aggregate_by_policy, build_shadow_outcomes  # noqa: E402
from src.storage.candle_store import CandleStore  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Offline Shadow Outcome batch(read-only)")
    ap.add_argument("--journal", default="data/journal.sqlite")
    ap.add_argument("--candles", default="data/market.sqlite")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--horizons", default="30,60", help="쉼표 구분 분 단위 horizon")
    ap.add_argument("--built-at", default=None, help="빌드 시각(ISO Z, 재현성)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())
    journal = SqliteJournalStore(args.journal)
    candles = CandleStore(args.candles)
    ds = build_shadow_outcomes(journal, candles, horizons_min=horizons,
                               interval=args.interval, built_at=args.built_at,
                               source=args.journal)

    if args.json:
        print(json.dumps({"meta": ds.to_meta_dict(),
                          "aggregate": {h: aggregate_by_policy(ds, h) for h in horizons},
                          "records": ds.to_dicts()}, ensure_ascii=False, indent=2))
        return 0

    print(f"=== Shadow Outcome ({args.journal}) — records={len(ds)} horizons={horizons} ===")
    print("⚠ price-direction shadow: 결정봉 종가 대비 방향 프록시(실 체결 수량/비용 아님).")
    for h in horizons:
        agg = aggregate_by_policy(ds, h)
        print(f"\n## horizon +{h}m (INCOMPLETE 제외)")
        if not agg:
            print("  (완결 레코드 없음)")
        for pid, a in sorted(agg.items()):
            print(f"  Policy {pid}: complete={a['n_complete']} blocked={a['n_blocked']} "
                  f"allowed={a['n_allowed']}")
            print(f"    blocked avoided_loss={a['blocked_avoided_loss']:.4f} "
                  f"missed_profit={a['blocked_missed_profit']:.4f} "
                  f"(net={a['blocked_avoided_loss'] - a['blocked_missed_profit']:+.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
