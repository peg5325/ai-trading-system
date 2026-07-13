"""통합 저장소 데이터 품질 리포트 — market_history.sqlite read-only 집계.

사용:
  ./.venv/bin/python scripts/history_quality_report.py --db data/market_history.sqlite
  ./.venv/bin/python scripts/history_quality_report.py --db data/market_history.sqlite --json

symbol별 earliest/latest·total bars·trading days·full-session days·missing days·
missing intraday minutes·gap·5m resample 가능 여부 + market summary. Toss API 미호출.
설계: docs/MARKET_HISTORY_BACKFILL_DESIGN.md
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history import quality_report  # noqa: E402


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="market_history 품질 리포트(read-only)")
    ap.add_argument("--db", default="data/market_history.sqlite")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--calendar-days", type=int, default=30)
    ap.add_argument("--min-coverage", type=float, default=90.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    rep = quality_report(args.db, interval=args.interval,
                         calendar_days=args.calendar_days, min_coverage_pct=args.min_coverage)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    print(f"=== market_history 품질 리포트 ({args.db}) ===")
    print(f"interval={rep['interval']}  as_of={rep['as_of']}  "
          f"window={rep['calendar_days']}d  full-session 기준 ≥{rep['min_coverage_pct']}%")
    for s in rep["symbols"]:
        print(f"\n### {s['market']} {s['symbol']} [{s['interval']}]")
        print(f"  earliest {s['earliest']}  latest {s['latest']}")
        print(f"  total_bars={s['total_bars']}  trading_days={s['trading_days']}  "
              f"full_session_days={s['full_session_days']}")
        print(f"  missing_days(최근 {rep['calendar_days']}d)={s['missing_days_count']}  "
              f"missing_intraday_min={s['total_missing_intraday_min']}  gaps={s['total_gaps']}  "
              f"5m_resample={'가능' if s['resample_5m'] else '불가/위험'}")
        for d in s["per_day"]:
            flag = "FULL" if d["full_session"] else ("PARTIAL" if d["coverage_pct"] >= 40 else "SPARSE")
            print(f"    {d['day']}: reg={d['reg_bars']} cov={d['coverage_pct']}% "
                  f"miss_min={d['missing_minutes']} gaps={d['gaps']} [{flag}]")
    print("\n=== market summary ===")
    for mk, m in rep["markets"].items():
        print(f"  {mk}: symbols={m['symbols']}  full_session_days(합)={m['full_session_days']}  "
              f"total_bars={m['total_bars']}  {m['min_earliest']} ~ {m['max_latest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
