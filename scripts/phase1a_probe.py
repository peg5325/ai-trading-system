#!/usr/bin/env python
"""Phase 1A Toss provider capability probe (이슈 #68) — 재현 가능 스크립트.

READ-ONLY 시장데이터 조회만. 주문 0. 로컬 DB 쓰기 0. 자격증명은 `src.config.load_settings`
(.env)에서 로드하며 **출력·기록하지 않는다**. 측정 결과(시장데이터 + 시각/지연)만 JSON 으로 저장.

산출: `docs/research/PROBE_REPORT_TOSS_CAPABILITY_V1.md` 의 수치를 이 스크립트가 재생성한다
(라이브 데이터라 daily latest·generated_utc 는 실행 시각에 따라 전진한다 — capability 결론은 불변).

사용:
    ./.venv/bin/python scripts/phase1a_probe.py --out reports/research/phase1a_probe/toss_capability_probe.json

요청 파라미터(고정): 아래 상수. spacing 0.4s, daily page cap 13(≈10y), 1m wall 은 targeted
before-jump(1y/2y/3y/5y), 세션 라벨은 최근 6페이지 집계, split/adjustment 는 알려진 분할일 대조.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_settings
from src.providers.toss_client import TossClient
from src.models import classify_session, MARKET_US, exchange_tz
from src.output_layout import research_dir

ET = ZoneInfo("America/New_York")

# --- 고정 요청 파라미터(재현용) ---
DAILY_SYMBOLS = ["SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "SOXL", "TQQQ"]
MINUTE_WALL_SYMBOLS = ["SPY", "AAPL"]
SESSION_SYMBOL = "SPY"
SPACING = 0.4
MAX_DAILY_PAGES = 13
MINUTE_TARGETS = {
    "~1y": "2025-07-22T12:00:00.000+09:00",
    "~2y": "2024-07-22T12:00:00.000+09:00",
    "~3y": "2023-07-22T12:00:00.000+09:00",
    "~5y": "2021-07-22T12:00:00.000+09:00",
}
# 알려진 forward split (date -> ratio) — 수정주가(adjusted) 연속성 실증용.
SPLITS = {
    "NVDA": ("2024-06-10", 10.0), "AMZN": ("2022-06-06", 20.0),
    "TSLA": ("2022-08-25", 3.0), "AAPL": ("2020-08-31", 4.0),
}
# 1분봉 adjustment 실증: NVDA 10:1(2024-06-10) 분할 전(2024-06-07 ET 정오) 근처.
MINUTE_ADJ_BEFORE = "2024-06-08T02:00:00.000+09:00"  # ≈ 2024-06-07 13:00 ET


def _et_date(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(ET).date().isoformat()


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 1A Toss capability probe (read-only)")
    ap.add_argument("--out", default=str(research_dir(".", "phase1a_probe") / "toss_capability_probe.json"))
    ap.add_argument("--spacing", type=float, default=SPACING)
    args = ap.parse_args()

    s = load_settings()
    if not s.has_credentials:
        print("ERROR: .env 의 TOSS_CLIENT_ID/SECRET 가 비어있습니다.", file=sys.stderr)
        return 2

    report: dict = {"generated_utc": datetime.now(timezone.utc).isoformat(),
                    "spacing_s": args.spacing, "daily": {}, "minute_wall": {},
                    "sessions_live": {}, "splits": {}, "minute_adjustment": {},
                    "http_429": 0, "latencies_ms": []}

    with TossClient(s) as c:
        def fetch(sym, iv, before=None, count=200):
            t0 = time.monotonic()
            r = c.get_candles(sym, iv, count=count, before=before)
            report["latencies_ms"].append(round((time.monotonic() - t0) * 1000))
            return r

        # 1) 일봉 깊이 + shape (nextBefore 따라 wall 또는 page cap 까지)
        for sym in DAILY_SYMBOLS:
            bars, raw_ts, before, pages, hit_wall, err = {}, [], None, 0, False, None
            try:
                while pages < MAX_DAILY_PAGES:
                    r = fetch(sym, "1d", before=before)
                    cs = r.get("candles", []) or []
                    nb = r.get("nextBefore")
                    if not cs:
                        hit_wall = True
                        break
                    pages += 1
                    for row in cs:
                        d = _et_date(row["timestamp"])
                        bars[d] = float(row["closePrice"])
                        raw_ts.append((d, row["timestamp"]))
                    if not nb:
                        hit_wall = True
                        break
                    before = nb
                    time.sleep(args.spacing)
            except Exception as e:  # noqa: BLE001
                if getattr(getattr(e, "response", None), "status_code", None) == 429:
                    report["http_429"] += 1
                err = f"{type(e).__name__}: {str(e)[:120]}"
            dates = sorted(bars)
            report["daily"][sym] = {
                "pages": pages, "bars": len(bars),
                "earliest": dates[0] if dates else None, "latest": dates[-1] if dates else None,
                "hit_wall": hit_wall, "reached_cap": pages >= MAX_DAILY_PAGES and not hit_wall,
                "error": err}
            if sym in SPLITS and dates:
                sdate, ratio = SPLITS[sym]
                bd = [d for d in dates if d < sdate]
                ad = [d for d in dates if d >= sdate]
                if bd and ad:
                    pre, post = bars[bd[-1]], bars[ad[0]]
                    jump = pre / post if post else None
                    report["splits"][sym] = {
                        "split_date": sdate, "declared_ratio": ratio,
                        "close_before": round(pre, 4), "close_after": round(post, 4),
                        "pre_over_post": round(jump, 3) if jump else None,
                        "verdict": ("adjusted" if jump and jump < ratio * 0.5 else
                                    "raw/unadjusted" if jump and jump > ratio * 0.7 else "ambiguous")}
                else:
                    report["splits"][sym] = {"split_date": sdate, "note": "split date outside probed depth"}
            summer = next((t for (d, t) in raw_ts if d[5:7] in ("06", "07", "08")), None)
            winter = next((t for (d, t) in raw_ts if d[5:7] in ("12", "01", "02")), None)
            report["daily"][sym]["dst_sample"] = {
                "summer_raw_ts": summer, "summer_utc": datetime.fromisoformat(summer).astimezone(timezone.utc).isoformat() if summer else None,
                "winter_raw_ts": winter, "winter_utc": datetime.fromisoformat(winter).astimezone(timezone.utc).isoformat() if winter else None}
            time.sleep(args.spacing)

        # 2) 1m 보존 wall — targeted before-jump
        for sym in MINUTE_WALL_SYMBOLS:
            report["minute_wall"][sym] = {}
            for label, before in MINUTE_TARGETS.items():
                try:
                    r = fetch(sym, "1m", before=before, count=200)
                    cs = r.get("candles", []) or []
                    report["minute_wall"][sym][label] = {
                        "before": before, "rows": len(cs),
                        "oldest": cs[-1]["timestamp"] if cs else None,
                        "newest": cs[0]["timestamp"] if cs else None}
                except Exception as e:  # noqa: BLE001
                    if getattr(getattr(e, "response", None), "status_code", None) == 429:
                        report["http_429"] += 1
                    report["minute_wall"][sym][label] = {"before": before, "error": f"{type(e).__name__}: {str(e)[:100]}"}
                time.sleep(args.spacing)

        # 3) 라이브 1m 세션 라벨 커버리지
        sess, before = {}, None
        for _ in range(6):
            r = fetch(SESSION_SYMBOL, "1m", before=before, count=200)
            cs = r.get("candles", []) or []
            if not cs:
                break
            for row in cs:
                m = datetime.fromisoformat(row["timestamp"]).astimezone(exchange_tz(MARKET_US))
                lbl = classify_session(m, MARKET_US)
                sess[lbl] = sess.get(lbl, 0) + 1
            nb = r.get("nextBefore")
            if not nb:
                break
            before = nb
            time.sleep(args.spacing)
        report["sessions_live"][SESSION_SYMBOL] = sess

        # 4) 1분봉 adjustment 실증 — NVDA 10:1 분할 전 가격이 수정 스케일인가
        try:
            r = fetch("NVDA", "1m", before=MINUTE_ADJ_BEFORE, count=5)
            cs = r.get("candles", []) or []
            report["minute_adjustment"]["NVDA_pre_split_2024_06_07"] = {
                "before": MINUTE_ADJ_BEFORE,
                "closes": [round(float(x["closePrice"]), 2) for x in cs],
                "expect_adjusted_approx": 121, "raw_would_be_approx": 1210,
                "verdict": "adjusted" if cs and float(cs[0]["closePrice"]) < 300 else "raw/unclear"}
        except Exception as e:  # noqa: BLE001
            report["minute_adjustment"]["NVDA_pre_split_2024_06_07"] = {"error": f"{type(e).__name__}: {str(e)[:100]}"}

    lat = report["latencies_ms"]
    report["latency_summary"] = {"calls": len(lat), "min_ms": min(lat) if lat else None,
                                 "max_ms": max(lat) if lat else None,
                                 "avg_ms": round(sum(lat) / len(lat)) if lat else None}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"[written] {args.out}  (429={report['http_429']}, calls={report['latency_summary']['calls']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
