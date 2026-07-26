"""§5.5 opening range 창 완전성 coverage 측정 — 재현 스크립트 (이슈 #89).

사전등록 §5.5 는 "OR 15분 전부 필요" 규칙이 표본을 무너뜨리지 않는다는 근거로 실측치를 인용한다.
그 수치의 **재현 경로가 저장소에 없으면 근거가 아니라 주장**이다(독립 적대적 리뷰 지적).

★ 이 스크립트는 **봉의 개수만 센다.** 가격·수익률·성과를 계산하지 않으며, blind OOS 구간을
  포함하지 않는다. 로드는 공식 게이트 `oos_guard.research_load(purpose="coverage")` 만 경유한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.exchange_calendar import trading_days  # noqa: E402
from src.history.oos_guard import (TRACK_INTRADAY, blind_oos_window,  # noqa: E402
                                   research_load)
from src.models import ET  # noqa: E402

DEV_START, DEV_END, WARMUP = date(2023, 4, 20), date(2026, 1, 20), 20
OR_LO, OR_HI = 9 * 60 + 30, 9 * 60 + 45          # 09:30~09:44 (primary)
DEFAULT_SYMBOLS = ["SPY", "QQQ", "NVDA", "TSLA", "SOXL", "SOXS", "NFLX", "VST", "XLU", "PLTR"]


def day_bounds(d: date) -> tuple[datetime, datetime]:
    lo = datetime.combine(d, time(0, 0), tzinfo=ET)
    return lo.astimezone(timezone.utc), (lo + timedelta(days=1)).astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--sample-days", type=int, default=12)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    ev = trading_days(DEV_START, DEV_END)[WARMUP:]
    step = max(1, len(ev) // args.sample_days)
    days = [ev[i] for i in range(0, len(ev), step)][:args.sample_days]
    oos_lo, _ = blind_oos_window(TRACK_INTRADAY)
    assert all(d < oos_lo for d in days), "blind OOS 날짜가 표본에 들어갔다"

    full = incomplete = nodata = 0
    post_present = post_absent = 0        # §8.7 fallback 의 구조적 빈도
    cases = []
    for d in days:
        lo, hi = day_bounds(d)
        for sym in args.symbols:
            c = research_load(args.db, sym, "1m", start=lo, end=hi,
                              purpose="coverage", track=TRACK_INTRADAY)
            reg = [x for x in c if x.session == "REGULAR"]
            if not reg:
                nodata += 1
                continue
            # 그 날 POST 봉이 있는가 — 없으면 마지막 REGULAR 버킷이 닫히지 않아 §8.7 fallback
            if any(x.session == "POST" for x in c):
                post_present += 1
            else:
                post_absent += 1
            n = sum(1 for x in reg
                    if OR_LO <= x.ts_exchange.hour * 60 + x.ts_exchange.minute < OR_HI)
            if n == 15:
                full += 1
            else:
                incomplete += 1
                cases.append({"day": d.isoformat(), "symbol": sym, "or_bars": n})

    total = full + incomplete + nodata
    receipt = {
        "schema": "prereg-orb-coverage-probe/v1", "issue": 89,
        "purpose": "coverage", "performance_computed": False,
        "measures_only": ["incomplete_opening_range", "post_bar_presence"],
        "window": {"start": DEV_START.isoformat(), "end": DEV_END.isoformat(),
                   "warmup_trading_days": WARMUP, "blind_oos_days_included": 0},
        "sample": {"days": [d.isoformat() for d in days],
                   "symbols": list(args.symbols), "symbol_days": total},
        "result": {"or_complete": full, "or_incomplete": incomplete,
                   "no_regular_data": nodata,
                   "complete_pct": round(100.0 * full / total, 2) if total else None},
        "eod_fallback_exposure": {
            "symbol_days_with_post_bars": post_present,
            "symbol_days_without_post_bars": post_absent,
            "without_post_pct": round(100.0 * post_absent / (post_present + post_absent), 2)
                                if (post_present + post_absent) else None,
            "meaning": "POST 봉이 없는 날은 마지막 REGULAR 버킷이 닫히지 않아 §8.7 fallback 이 발동한다"},
        "incomplete_cases": cases,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(receipt, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(json.dumps(receipt["result"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
