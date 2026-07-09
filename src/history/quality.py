"""통합 저장소(market_history.sqlite) 데이터 품질 리포트 — read-only 집계.

symbol별: earliest/latest, total bars, trading days, full-session days,
missing days, missing intraday minutes, gap count, 5m resample 가능 여부.
market별 summary 포함. Toss API 호출 없음.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

# 정규장 창(거래소 로컬 minute-of-day). US 09:30-16:00 ET, KR 09:00-15:30 KST.
REG_WINDOW = {"US": (9 * 60 + 30, 16 * 60), "KR": (9 * 60, 15 * 60 + 30)}


def _mod(tx: str) -> int:
    """ts_exchange 로컬 시:분 → minute-of-day."""
    return int(tx[11:13]) * 60 + int(tx[14:16])


def _weekdays(d0: date, d1: date) -> list[date]:
    out, d = [], d0
    while d <= d1:
        if d.weekday() < 5:  # 주말 제외(공휴일 미반영 — 한계)
            out.append(d)
        d += timedelta(days=1)
    return out


def quality_report(db_path: str, *, interval: str = "1m",
                   calendar_days: int = 30, min_coverage_pct: float = 90.0,
                   today: date | None = None) -> dict:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    symbols = [r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM candle_history WHERE interval=? ORDER BY symbol", (interval,))]
    if today is None:
        # 저장된 최신 봉 날짜를 기준일로 사용(결정론·테스트 안정성)
        row = con.execute("SELECT MAX(substr(ts_exchange,1,10)) FROM candle_history "
                          "WHERE interval=?", (interval,)).fetchone()
        today = date.fromisoformat(row[0]) if row and row[0] else date(1970, 1, 1)

    per_symbol = []
    for sym in symbols:
        rows = con.execute(
            "SELECT market, ts_utc, ts_exchange FROM candle_history "
            "WHERE symbol=? AND interval=? ORDER BY ts_utc", (sym, interval)).fetchall()
        if not rows:
            continue
        market = rows[0]["market"]
        lo, hi = REG_WINDOW.get(market, (0, 24 * 60))
        reg_span = hi - lo
        by_day: dict[str, list[int]] = {}
        for r in rows:
            by_day.setdefault(r["ts_exchange"][:10], []).append(_mod(r["ts_exchange"]))

        per_day = []
        full_days = 0
        total_missing_min = 0
        total_gaps = 0
        for day, mods in sorted(by_day.items()):
            reg = sorted(set(m for m in mods if lo <= m < hi))
            cov = len(reg) / reg_span * 100 if reg_span else 0.0
            miss = reg_span - len(reg) if reg else reg_span
            gaps = sum(1 for a, b in zip(reg, reg[1:]) if b - a > 1)
            is_full = cov >= min_coverage_pct
            full_days += int(is_full)
            if reg:
                total_missing_min += miss
            total_gaps += gaps
            per_day.append(dict(day=day, total_bars=len(mods), reg_bars=len(reg),
                                coverage_pct=round(cov, 1), missing_minutes=miss,
                                gaps=gaps, full_session=is_full))

        present_days = set(by_day)
        exp = [d.isoformat() for d in _weekdays(today - timedelta(days=calendar_days), today)]
        missing_days = [d for d in exp if d not in present_days]
        # 5m 리샘플: full-session day 가 최소 1일이면 그 구간 리샘플 가능(gap→불완전 5m는 별도 표시)
        resample_5m = full_days >= 1

        per_symbol.append(dict(
            symbol=sym, market=market, interval=interval,
            earliest=rows[0]["ts_utc"], latest=rows[-1]["ts_utc"],
            total_bars=len(rows), trading_days=len(by_day),
            full_session_days=full_days,
            missing_days_count=len(missing_days), missing_days=missing_days,
            total_missing_intraday_min=total_missing_min, total_gaps=total_gaps,
            resample_5m=resample_5m, per_day=per_day))

    # market summary
    markets: dict[str, dict] = {}
    for s in per_symbol:
        m = markets.setdefault(s["market"], dict(symbols=0, full_session_days=0,
                                                 total_bars=0, min_earliest=None, max_latest=None))
        m["symbols"] += 1
        m["full_session_days"] += s["full_session_days"]
        m["total_bars"] += s["total_bars"]
        m["min_earliest"] = s["earliest"] if not m["min_earliest"] else min(m["min_earliest"], s["earliest"])
        m["max_latest"] = s["latest"] if not m["max_latest"] else max(m["max_latest"], s["latest"])

    con.close()
    return dict(db=db_path, interval=interval, as_of=today.isoformat(),
                calendar_days=calendar_days, min_coverage_pct=min_coverage_pct,
                symbols=per_symbol, markets=markets)
