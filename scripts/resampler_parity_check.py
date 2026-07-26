"""실데이터 리샘플러 parity 검사 (이슈 #76) — **coverage 목적, 성과 계산 없음**.

고정된 clean research snapshot 의 1분봉을 네 경로에 동일하게 넣고 **직렬화 결과가
byte-equivalent** 인지만 확인한다:

  1. research canonical core  (`src/bars/resample.py`)
  2. live adapter             (`indicator_engine.resample` + `paper_loop.frame_to_candles`)
  3. research 소비자          (`candidate_stream.build_decision_bars`, 5m)
  4. replay                   (`ReplaySystem._candles`)

★ 연구 규율
- 로드는 **공식 게이트** `oos_guard.research_load(purpose="coverage")` 만 사용한다
  (`load_candles` 직접 호출 금지 — `tests/test_oos_guard_wiring.py` AST 경계).
- **수익률·forward return·expectancy·PF/MDD·leaderboard 를 계산하지 않는다.** 비교 대상은
  timestamp/symbol/interval/session/OHLCV/market/currency/adjusted 뿐이다.
- 기본 표본은 **blind OOS(2026-01-21~2026-07-21) 이전** 거래일이다. OOS 날짜를 쓰려면
  `--allow-oos-coverage` 를 명시해야 한다(그래도 purpose 는 coverage 고정).
- receipt 에는 절대경로·credential 을 쓰지 않는다(basename 만).

사용:
    python scripts/resampler_parity_check.py \
        --db data/research_snapshots/<clean>.sqlite \
        --out reports/RESAMPLER_PARITY_RECEIPT.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.engines.indicator_engine import candles_to_frame, resample  # noqa: E402
from src.history.oos_guard import (TRACK_INTRADAY, blind_oos_window,  # noqa: E402
                                   research_load)
from src.bars.resample import (opening_range,  # noqa: E402
                               resample_1m_to_5m, resample_1m_to_15m)
from src.live.paper_loop import frame_to_candles  # noqa: E402
from src.models import ET, Candle  # noqa: E402
from src.replay.system import ReplaySystem, candle_to_dict  # noqa: E402

SCHEMA = "resampler-parity/receipt/v1"
TRACKED_SNAPSHOT_RECEIPT = os.path.join("docs", "research",
                                        "P0_US30_ORB_CLEAN_SNAPSHOT_RECEIPT.json")

# 대표 거래일(모두 blind OOS 이전). DST 전환·조기폐장·정상일을 함께 본다.
DEFAULT_DAYS = [
    "2025-03-10",   # US DST 시작(spring forward) 직후 첫 거래일
    "2025-11-03",   # US DST 해제(fall back) 직후 첫 거래일
    "2025-11-28",   # 추수감사절 다음날 — 13:00 ET 조기폐장
    "2025-12-24",   # 크리스마스 이브 — 13:00 ET 조기폐장
    "2026-01-02",   # 연초 정상 거래일
    "2026-01-20",   # blind OOS 시작 직전 거래일
]
# ETF·대형주·레버리지/인버스를 섞는다(P0-US30 표본 — 이 스냅샷에 KR 종목은 없다).
DEFAULT_SYMBOLS = ["SPY", "QQQ", "NVDA", "TSLA", "SOXL", "SOXS"]


class _StubStore:
    """replay 경로용 read-only 스텁(저장소 접근 없이 동일 1분봉을 돌려준다)."""

    def __init__(self, candles: list[Candle]):
        self._c = candles

    def query(self, symbol: str, interval: str) -> list[Candle]:
        return [c for c in self._c if c.symbol == symbol and c.interval == interval]


def canon(candles: list[Candle]) -> list[list]:
    """비교용 결정론 직렬화(성과 필드 없음)."""
    return [[c.symbol, c.interval, c.ts_utc.isoformat(), c.ts_exchange.isoformat(),
             c.session, c.open, c.high, c.low, c.close, c.volume,
             c.currency, bool(c.adjusted), c.market] for c in candles]


def day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """ET 거래일 [00:00, 익일 00:00) → UTC 반개구간."""
    lo = datetime.combine(day, time(0, 0), tzinfo=ET)
    return lo.astimezone(timezone.utc), (lo + timedelta(days=1)).astimezone(timezone.utc)


def compare_day(db: str, symbol: str, day: date, *, allow_oos: bool) -> dict:
    lo, hi = day_bounds_utc(day)
    oos_lo, oos_hi = blind_oos_window(TRACK_INTRADAY)
    in_oos = oos_lo <= day <= oos_hi
    if in_oos and not allow_oos:
        raise SystemExit(
            f"거부: {day} 는 blind OOS({oos_lo}~{oos_hi}) 안이다. coverage 목적이라도 "
            f"--allow-oos-coverage 를 명시해야 한다.")
    # 공식 게이트 경유(coverage 전용 — 성과 계산 없음)
    c1m = research_load(db, symbol, "1m", start=lo, end=hi,
                        purpose="coverage", track=TRACK_INTRADAY)
    result = {"symbol": symbol, "day": day.isoformat(), "in_blind_oos": in_oos,
              "bars_1m": len(c1m)}
    if not c1m:
        result.update({"status": "NO_DATA", "mismatches": []})
        return result

    mismatches: list[str] = []
    for rule, core_fn in (("5min", resample_1m_to_5m), ("15min", resample_1m_to_15m)):
        core = canon(core_fn(c1m, closed_only=True))
        live = canon(frame_to_candles(resample(candles_to_frame(c1m), rule), symbol))
        if core != live:
            mismatches.append(f"{rule}:core!=live")
        if rule == "5min":
            from src.history.candidate_stream import build_decision_bars
            if canon(build_decision_bars(c1m, symbol)) != live:
                mismatches.append("5min:candidate_stream!=live")
            replayed = ReplaySystem(journal=None,
                                    candle_store=_StubStore(c1m))._candles(symbol, None)
            expect = [candle_to_dict(x) for x in
                      frame_to_candles(resample(candles_to_frame(c1m), rule), symbol)]
            if replayed != expect:
                mismatches.append("5min:replay!=live")
            result["bars_5m"] = len(core)
            result["sessions"] = sorted({r[4] for r in core})
        else:
            result["bars_15m"] = len(core)
        # 순서 불변(실데이터): 입력을 섞어도 같은 결과여야 한다.
        shuffled = c1m[len(c1m) // 3:] + c1m[:len(c1m) // 3]
        if canon(core_fn(shuffled, closed_only=True)) != core:
            mismatches.append(f"{rule}:order_dependent")

    # ORB 결정 객체(15분 opening range)도 같은 core 에서 나오는지 — 완전한 창이면 09:30 15m
    # 버킷과 OHLCV 가 정확히 같아야 한다(둘 다 09:30~09:44 REGULAR 1분봉 집합이다).
    ors = opening_range(c1m, minutes=15, market="US", closed_only=True)
    result["bars_15m_or"] = len(ors)
    if ors:
        b15 = [b for b in resample_1m_to_15m(c1m, closed_only=True)
               if b.session == "REGULAR"
               and b.ts_exchange.hour == 9 and b.ts_exchange.minute == 30]
        if not b15:
            mismatches.append("15m_or:no_matching_bucket")
        else:
            o, b = ors[0], b15[0]
            if (o.open, o.high, o.low, o.close, o.volume) != \
                    (b.open, b.high, b.low, b.close, b.volume):
                mismatches.append("15m_or:ohlcv!=15m_bucket")
            if o.ts_utc != b.ts_utc:
                mismatches.append("15m_or:label!=15m_bucket")

    result.update({"status": "OK" if not mismatches else "MISMATCH",
                   "mismatches": mismatches})
    return result


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def tracked_snapshot_identity(repo_root: str) -> dict:
    path = os.path.join(repo_root, TRACKED_SNAPSHOT_RECEIPT)
    with open(path, encoding="utf-8") as f:
        rec = json.load(f)
    snap = rec.get("snapshot", {})
    return {"basename": snap.get("basename"),
            "file_sha256": snap.get("file_sha256"),
            "content_sha256": snap.get("content_sha256"),
            "as_of": rec.get("contract", {}).get("as_of"),
            "collection_run_id": rec.get("contract", {}).get("collection_run_id")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="clean research snapshot 경로")
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--days", nargs="*", default=DEFAULT_DAYS)
    ap.add_argument("--out", required=True, help="sanitized receipt 출력 경로")
    ap.add_argument("--allow-oos-coverage", action="store_true",
                    help="blind OOS 날짜를 coverage 목적으로 포함(성과는 여전히 계산 안 함)")
    ap.add_argument("--skip-identity-check", action="store_true",
                    help="tracked snapshot basename 대조를 생략(임시 DB 검사용)")
    args = ap.parse_args(argv)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    identity = tracked_snapshot_identity(repo_root)
    db_base = os.path.basename(args.db)
    # identity 검증: basename 뿐 아니라 **실제 파일 SHA-256** 을 계산해 tracked receipt 와 대조한다.
    # (이름만 맞는 아무 DB 로 canonical hash 가 박힌 receipt 를 만들 수 없게 — 독립 리뷰 B2)
    if args.skip_identity_check:
        snapshot_block = {"basename": db_base, "identity_verified": False,
                          "note": "identity check skipped (--skip-identity-check)"}
    else:
        if db_base != identity["basename"]:
            raise SystemExit(
                f"거부: --db basename({db_base}) 이 tracked snapshot({identity['basename']}) 과 다르다.")
        actual = sha256_file(args.db)
        if actual != identity["file_sha256"]:
            raise SystemExit(
                f"거부: --db file SHA-256 이 tracked receipt 와 다르다 "
                f"(계산 {actual[:16]}… vs 기대 {str(identity['file_sha256'])[:16]}…).")
        snapshot_block = {**identity, "identity_verified": True,
                          "file_sha256_recomputed": actual}

    rows = []
    for day_s in args.days:
        day = date.fromisoformat(day_s)
        for sym in args.symbols:
            rows.append(compare_day(args.db, sym, day,
                                    allow_oos=args.allow_oos_coverage))

    checked = [r for r in rows if r["status"] != "NO_DATA"]
    mism = [r for r in rows if r["status"] == "MISMATCH"]
    receipt = {
        "schema": SCHEMA,
        "issue": 76,
        "purpose": "coverage",
        "performance_computed": False,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "snapshot": snapshot_block,
        "snapshot_basename_checked": db_base,
        "blind_oos": {"track": TRACK_INTRADAY,
                      "start": blind_oos_window(TRACK_INTRADAY)[0].isoformat(),
                      "end": blind_oos_window(TRACK_INTRADAY)[1].isoformat(),
                      "days_inside_window": sum(1 for r in rows if r["in_blind_oos"])},
        "compared_paths": ["research_core", "live_adapter", "candidate_stream", "replay"],
        "extra_invariants": ["input_order_invariance",
                             "opening_range_15m == 09:30 15m bucket (OHLCV+label)"],
        "compared_fields": ["ts_utc", "ts_exchange", "symbol", "interval", "session",
                            "open", "high", "low", "close", "volume",
                            "currency", "adjusted", "market"],
        "summary": {
            "cells_requested": len(rows),
            "cells_with_data": len(checked),
            "cells_no_data": len(rows) - len(checked),
            "cells_matched": len(checked) - len(mism),
            "cells_mismatched": len(mism),
            "bars_1m_total": sum(r["bars_1m"] for r in rows),
            "bars_5m_total": sum(r.get("bars_5m", 0) for r in rows),
            "bars_15m_total": sum(r.get("bars_15m", 0) for r in rows),
            "bars_15m_or_total": sum(r.get("bars_15m_or", 0) for r in rows),
        },
        "cells": rows,
        "verdict": "PARITY_OK" if (checked and not mism) else
                   ("NO_DATA" if not checked else "PARITY_MISMATCH"),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(receipt, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    print(json.dumps(receipt["summary"], ensure_ascii=False, sort_keys=True))
    print(f"verdict={receipt['verdict']} out={os.path.basename(args.out)}")
    return 0 if receipt["verdict"] == "PARITY_OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
