#!/usr/bin/env python
"""백필 경계 결측 점검 — UTC 일자 경계에서 분봉이 통째로 빠졌는지 본다 (read-only).

왜 이 스크립트가 있는가 (#45 → #49)
--------------------------------------------------------------------------------
Toss 1m 캔들 API 는 `before` 가 속한 **UTC 일자 안쪽만** 응답한다. gap-skip 초기 구현이
빈 응답을 "요청 창 전체가 비었다"로 읽고 200분씩 건너뛰는 바람에, 경계를 넘을 때마다
직전 일자의 꼬리(`D-1 20:41~23:59Z`)를 **조회조차 하지 않고** 버렸다. 실측 피해는
SOXL 에서 00~19시가 36일치인데 21~23시만 9~10일치였다.

이 결측은 조용하다 — 총 봉 수도, 거래일 수도 그럴듯하게 늘어나기 때문이다. 드러나는
곳은 **UTC 시간대별 거래일 수의 불균일**뿐이다. 그래서 그것만 본다.

`scripts/history_quality_report.py` 로는 잡히지 않는다: 그쪽은 정규장 창(US 09:30–16:00 ET)
만 검사하는데, 유실 구간은 US POST 세션(≈20:41~23:59Z)이라 검사 범위 밖이다.

판정
--------------------------------------------------------------------------------
종목마다 "봉이 존재하는 UTC 시간대"의 거래일 수를 세고, 그 중앙값 대비 비율이
`--min-ratio`(기본 0.5) 아래로 떨어지는 시간대를 결측으로 신고한다. 절대 일수가 아니라
**중앙값 대비 비율**로 보는 이유: 시장마다 거래시간이 달라(US/KR) 절대 기준을 쓰면
정상 종목이 걸리거나 결측이 숨는다.

거래가 원래 없는 시간대(중앙값의 표본 자체가 0)는 세지 않는다 — 없는 걸 결측이라
부르면 매 실행이 빨간불이라 아무도 안 보게 된다.

사용:
  ./.venv/bin/python scripts/backfill_boundary_check.py --symbols QQQ,SPY
  ./.venv/bin/python scripts/backfill_boundary_check.py --symbols QQQ --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _repo_path(rel: str) -> str:
    return rel if os.path.isabs(rel) else os.path.join(_REPO_ROOT, rel)


def _median(v: list[int]) -> float:
    s = sorted(v)
    m = len(s) // 2
    return float(s[m]) if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def hour_day_counts(con: sqlite3.Connection, symbol: str, interval: str) -> dict[int, int]:
    """UTC 시간대 → 그 시간대에 봉이 하나라도 있는 거래일 수."""
    rows = con.execute(
        "SELECT CAST(substr(ts_utc, 12, 2) AS INTEGER) AS hh, "
        "       COUNT(DISTINCT substr(ts_utc, 1, 10)) AS days "
        "FROM candle_history WHERE symbol = ? AND interval = ? "
        "GROUP BY hh ORDER BY hh", (symbol, interval)).fetchall()
    return {int(h): int(d) for h, d in rows}


def check_symbol(con: sqlite3.Connection, symbol: str, interval: str,
                 min_ratio: float) -> dict:
    counts = hour_day_counts(con, symbol, interval)
    if not counts:
        return {"symbol": symbol, "status": "no_data", "hours": {},
                "median_days": None, "suspect_hours": []}

    # 중앙값은 **거래가 있는 시간대만** 으로 잡는다. 24시간 전부를 넣으면 야간 0 이
    # 중앙값을 끌어내려 진짜 결측이 정상으로 보인다.
    active = [d for d in counts.values() if d > 0]
    med = _median(active)
    suspect = []
    for h in sorted(counts):
        d = counts[h]
        if d <= 0:
            continue                      # 원래 거래 없는 시간대 — 결측이 아니다
        ratio = d / med if med else 0.0
        if ratio < min_ratio:
            suspect.append({"hour_utc": h, "days": d, "ratio": round(ratio, 3)})
    return {"symbol": symbol,
            "status": "MISSING" if suspect else "OK",
            "hours": counts, "median_days": med,
            "min_days": min(active), "max_days": max(active),
            "suspect_hours": suspect}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="백필 경계 결측 점검(UTC 시간대별 거래일 수 균일성, read-only)")
    ap.add_argument("--db", default="data/market_history.sqlite")
    ap.add_argument("--symbols", required=True, help="쉼표 구분 종목")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--min-ratio", type=float, default=0.5,
                    help="중앙값 대비 이 비율 미만인 시간대를 결측으로 신고(기본 0.5)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    db_path = _repo_path(args.db)
    # ★ read-only 로 연다. 기본 `sqlite3.connect()` 는 **없는 파일을 만들어 버려서**,
    #   `--db` 오타 하나로 0바이트 sqlite 가 생기고 이어서 "no such table" 원시
    #   트레이스백으로 죽는다. 점검 도구가 점검 대상을 만들어내면 안 된다.
    #   (`with sqlite3.connect(...)` 는 트랜잭션만 관리하고 연결은 닫지 않는다 → closing)
    if not os.path.exists(db_path):
        print(f"DB 가 없다: {db_path}", file=sys.stderr)
        return 2
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as con:
            results = [check_symbol(con, s, args.interval, args.min_ratio)
                       for s in symbols]
    except sqlite3.DatabaseError as e:
        print(f"DB 를 읽을 수 없다: {db_path} — {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"db": args.db, "interval": args.interval,
                          "min_ratio": args.min_ratio, "results": results},
                         ensure_ascii=False, indent=2))
    else:
        print(f"=== 경계 결측 점검  {args.db}  interval={args.interval} "
              f"(중앙값 대비 < {args.min_ratio} 이면 결측) ===")
        for r in results:
            print(f"\n### {r['symbol']}  [{r['status']}]")
            if r["status"] == "no_data":
                print("  (봉 없음)")
                continue
            print(f"  거래 있는 시간대 일수: 중앙값 {r['median_days']:.0f} · "
                  f"최소 {r['min_days']} · 최대 {r['max_days']}")
            print("  시간대별 거래일 수(UTC):")
            for h in sorted(r["hours"]):
                d = r["hours"][h]
                if d <= 0:
                    continue
                bar = "#" * max(1, round(40 * d / max(1, r["max_days"])))
                mark = "  ⚠" if any(s["hour_utc"] == h for s in r["suspect_hours"]) else ""
                print(f"    {h:02d}Z {d:5d} {bar}{mark}")
            if r["suspect_hours"]:
                print(f"  ⚠️ 결측 의심 시간대: "
                      + ", ".join(f"{s['hour_utc']:02d}Z({s['days']}일, "
                                  f"{s['ratio']:.2f}x)" for s in r["suspect_hours"]))

    # 하나라도 결측이면 비정상 종료 — CI/스크립트가 조용히 지나치지 않게.
    # `no_data`(요청한 종목이 통째로 없음)도 실패로 친다. 그게 정상 종료면 종목명 오타나
    # 백필 누락이 "점검 통과"로 보인다 — 조용히 지나가면 안 되는 상태의 전형이다.
    return 1 if any(r["status"] in ("MISSING", "no_data") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
