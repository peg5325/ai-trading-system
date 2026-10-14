#!/usr/bin/env python
"""H3 국면 조인 — 클린 백필 후보에 look-ahead 없는 시장 국면 라벨을 붙인다.

작업지시서: reports/research/h2-h4/WORKORDER_H3_REGIME.md · 선행: reports/research/h2-h4/h2b_backfill_validation.md
(2026-10-14부터 경로 변경, docs/OUTPUT_LAYOUT.md)

read-only 연구 산출물. 매매 로직·스코어 가중치·runtime config 를 일절 건드리지 않고,
국면 게이트를 **활성화하지 않는다**(오프라인 라벨 부착만).

입력:  datasets/h2b_candidates_final.jsonl  (H2-b 클린 백필 후보, 30m/60m forward 포함)
       data/market_history.sqlite            (1m 캔들)
출력:  datasets/h3_regime_joined.jsonl       (후보 + regime + EOD/Nd forward)

국면 정의(§3):
  A intraday_trend  QQQ 5m EMA20 대비 추세          (라이브 기본 정의)
  B daily_trend     QQQ 일간 EMA20 — **전날까지만**
  B' daily_long_trend QQQ 일간 EMA50 — **전날까지만** (다개월 국면, H3-b 신규)
  C long_trend      QQQ 5m EMA200 대비 추세         (더 긴 창)
  D breadth         유니버스 중 EMA20 위 비율
  E intraday_vs_ema QQQ 연속 vs_ema20               (이산화 없이 그대로)
  F vol_regime      실현변동성 / 자기 과거 중앙값   (인과적 기준선)

모든 라벨은 `*_asof` 를 함께 싣고, 행마다 `assert_no_lookahead` 를 통과해야 한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from bisect import bisect_right
from collections import Counter
from datetime import date, datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from src.history.candidate_stream import build_decision_bars, load_1m_candles
from src.history.forward_extended import (
    DEFAULT_DAY_HORIZONS,
    compute_extended_forwards,
    regular_closes_by_date,
)
from src.history.regime_join import (
    BENCHMARK_SYMBOL,
    DAILY_EMA_PERIOD,
    DAILY_LONG_EMA_PERIOD,
    INTRADAY_EMA_PERIOD,
    LONG_EMA_PERIOD,
    BreadthIndex,
    RegimeLabels,
    assert_no_lookahead,
    breadth_regime,
    context_series,
    daily_bars,
    daily_context_before,
    daily_context_including,
    daily_context_index,
    trend_of,
    vol_regime,
    volatility_series,
)

DEFAULT_SYMBOLS = ("SOXL", "QQQ", "NVDA", "SOXX", "SOXS", "AMD", "AVGO",
                   "MSFT", "GOOGL", "VST", "SPY", "XLU")


def _repo_path(rel: str) -> str:
    """저장소 기준 절대경로 — 다른 디렉터리에서 실행해도 엉뚱한 DB 를 읽지 않게."""
    return rel if os.path.isabs(rel) else os.path.join(_REPO_ROOT, rel)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _iso_z(dt: datetime | None) -> str | None:
    return None if dt is None else dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def load_symbol_bars(db: str, symbols, log=print) -> dict[str, list]:
    """종목별 5m 결정봉. live(paper_loop)와 동일한 resample 규칙(drop_partial)."""
    out = {}
    for sym in symbols:
        candles = load_1m_candles(db, sym, table="candle_history")
        bars = build_decision_bars(candles, sym)
        out[sym] = bars
        log(f"  {sym:6s} 1m={len(candles):7,d}  5m={len(bars):6,d}")
    return out


def build_benchmark_regimes(bars_by_symbol: dict[str, list], benchmark: str, log=print):
    """벤치마크 기준 (A)(C)(B)(F) 시계열을 한 번씩만 계산해 둔다."""
    bars = bars_by_symbol.get(benchmark) or []
    if not bars:
        raise SystemExit(f"벤치마크 {benchmark} 봉이 없다 — 국면 계산 불가")

    log(f"  (A) 장중 5m EMA{INTRADAY_EMA_PERIOD} …")
    intraday = context_series(bars, symbol=benchmark, ema_period=INTRADAY_EMA_PERIOD)
    log(f"  (C) 장기 5m EMA{LONG_EMA_PERIOD} …")
    long_ctx = context_series(bars, symbol=benchmark, ema_period=LONG_EMA_PERIOD)
    log(f"  (B) 하루 국면(일간 EMA{DAILY_EMA_PERIOD}, 전날까지) …")
    dbars = daily_bars(bars, benchmark)
    days, dctxs = daily_context_index(dbars, symbol=benchmark)
    log(f"      일봉 {len(dbars)}일 ({days[0] if days else '-'} ~ {days[-1] if days else '-'})")
    log(f"  (B') 하루 장기 국면(일간 EMA{DAILY_LONG_EMA_PERIOD}, 전날까지) …")
    # 같은 일봉 위에 창만 바꿔 얹는다. 일봉이 EMA 창보다 짧으면 builder 가 None 을 내고,
    # 그 구간은 라벨 없이 남는다(0 이나 마지막 값으로 때우지 않는다).
    dlong_days, dlong_ctxs = daily_context_index(
        dbars, symbol=benchmark, ema_period=DAILY_LONG_EMA_PERIOD)
    warm = sum(1 for c in dlong_ctxs if c is not None and c.benchmark_trend)
    log(f"      EMA{DAILY_LONG_EMA_PERIOD} 성립 일수 {warm}/{len(dbars)}")
    log("  (F) 변동성 국면(인과적 기준선) …")
    vol, ref = volatility_series(bars)

    ts_index = [b.ts_utc for b in bars]
    return {"bars": bars, "ts": ts_index, "intraday": intraday, "long": long_ctx,
            "daily_days": days, "daily_ctxs": dctxs,
            "daily_long_days": dlong_days, "daily_long_ctxs": dlong_ctxs,
            "vol": vol, "vol_ref": ref}


def build_breadth(bars_by_symbol: dict[str, list], log=print) -> BreadthIndex:
    """(D) breadth — 종목마다 EMA20 대비 위치를 사전계산. builder 재사용."""
    bi = BreadthIndex()
    for sym, bars in bars_by_symbol.items():
        if not bars:
            continue
        ctxs = context_series(bars, symbol=sym, ema_period=INTRADAY_EMA_PERIOD)
        bi.add(sym, bars, ctxs)
        log(f"  breadth {sym:6s} {len(bars):6,d} 봉")
    return bi


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="H3 국면 조인(read-only)")
    ap.add_argument("--db", default="data/market_history.sqlite")
    ap.add_argument("--candidates", default="datasets/h2b_candidates_final.jsonl")
    ap.add_argument("--out", default="datasets/h3_regime_joined.jsonl")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--benchmark", default=BENCHMARK_SYMBOL)
    ap.add_argument("--day-horizons", default=",".join(str(d) for d in DEFAULT_DAY_HORIZONS))
    ap.add_argument("--limit", type=int, default=0, help="후보 N 건만(스모크용)")
    args = ap.parse_args(argv)

    db = _repo_path(args.db)
    cand_path = _repo_path(args.candidates)
    out_path = _repo_path(args.out)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    day_horizons = tuple(int(x) for x in args.day_horizons.split(",") if x.strip())

    print(f"[1/5] 5m 결정봉 로드 — {db}")
    bars_by_symbol = load_symbol_bars(db, symbols)

    print(f"[2/5] 벤치마크({args.benchmark}) 국면 시계열")
    bm = build_benchmark_regimes(bars_by_symbol, args.benchmark)

    print("[3/5] breadth 인덱스")
    breadth_idx = build_breadth(bars_by_symbol)

    print("[4/5] 종목별 forward 인덱스(EOD/Nd)")
    fwd_idx = {}
    for sym, bars in bars_by_symbol.items():
        closes = regular_closes_by_date(bars)
        fwd_idx[sym] = {"closes": closes, "days": sorted(closes),
                        "ts": [b.ts_utc for b in bars], "bars": bars}
        print(f"  {sym:6s} 정규장 마감 {len(closes)}일")

    print(f"[5/5] 조인 — {cand_path}")
    stats = Counter()
    n_out = 0
    # 임시 파일에 쓰고 정상 종료 시에만 원자적으로 교체한다. 최종 경로에 바로 쓰면 루프
    # 중간에 예외가 났을 때 **완결된 것처럼 보이는 부분 파일**이 남고, 다운스트림
    # (h3_regime_analysis.py)이 그것을 정상 입력으로 읽어 조용히 축소된 표본으로
    # 리포트를 낸다. 잘린 표본은 실패보다 나쁘다 — 결론이 그럴듯해 보이기 때문이다.
    tmp_path = out_path + ".tmp"
    with open(cand_path) as fin, open(tmp_path, "w") as fout:
        for line in fin:
            if args.limit and n_out >= args.limit:
                break
            row = json.loads(line)
            stats["rows_in"] += 1
            sym = row["symbol"]
            dt = _parse(row["bar_ts_utc"])

            # --- 국면 라벨 ---
            # 벤치마크에서 ts <= decision_time 인 마지막 봉. bisect_right-1 이므로
            # 결정 시점 이후 봉은 구조적으로 닿을 수 없다.
            k = bisect_right(bm["ts"], dt) - 1
            if k < 0:
                stats["no_benchmark_bar"] += 1
                labels = RegimeLabels(decision_ts_utc=row["bar_ts_utc"], et_date=row["et_date"])
            else:
                a_ctx, c_ctx = bm["intraday"][k], bm["long"][k]
                # 하루 국면은 **ET 거래일** 기준으로 '전날까지'를 고른다. UTC 날짜로 자르면
                # PRE 세션(ET 04:00 = UTC 08:00)과 POST(ET 20:00 = 익일 UTC 00:00)에서
                # 거래일이 어긋나 당일 마감이 새어든다.
                anchor_day = date.fromisoformat(row["et_date"])
                d_ctx = daily_context_before(bm["daily_days"], bm["daily_ctxs"],
                                             anchor_day)
                dl_ctx = daily_context_before(bm["daily_long_days"],
                                              bm["daily_long_ctxs"], anchor_day)
                br, br_n, br_asof = breadth_idx.at(dt)
                vr, vlabel = vol_regime(bm["vol"][k], bm["vol_ref"][k])
                labels = RegimeLabels(
                    decision_ts_utc=row["bar_ts_utc"], et_date=row["et_date"],
                    intraday_trend=trend_of(a_ctx),
                    intraday_vs_ema=None if a_ctx is None else a_ctx.benchmark_vs_ema20,
                    intraday_asof=None if a_ctx is None else a_ctx.benchmark_source_bar_end,
                    long_trend=trend_of(c_ctx),
                    long_vs_ema=None if c_ctx is None else c_ctx.benchmark_vs_ema20,
                    long_asof=None if c_ctx is None else c_ctx.benchmark_source_bar_end,
                    daily_trend=trend_of(d_ctx),
                    daily_vs_ema=None if d_ctx is None else d_ctx.benchmark_vs_ema20,
                    daily_asof=None if d_ctx is None else d_ctx.benchmark_source_bar_end,
                    daily_long_trend=trend_of(dl_ctx),
                    daily_long_vs_ema=(None if dl_ctx is None
                                       else dl_ctx.benchmark_vs_ema20),
                    daily_long_asof=(None if dl_ctx is None
                                     else dl_ctx.benchmark_source_bar_end),
                    breadth=br, breadth_symbols=br_n, breadth_regime=breadth_regime(br),
                    breadth_asof=_iso_z(br_asof),
                    vol_ratio=vr, vol_regime=vlabel,
                    vol_asof=None if a_ctx is None else a_ctx.benchmark_source_bar_end)
                if labels.intraday_trend:
                    stats[f"intraday_{labels.intraday_trend}"] += 1
                if labels.daily_trend:
                    stats[f"daily_{labels.daily_trend}"] += 1
                if labels.daily_long_trend:
                    stats[f"daily_long_{labels.daily_long_trend}"] += 1

            # ★ 행마다 강제 — 하나라도 새면 전체 리포트가 무의미하므로 크게 실패시킨다.
            assert_no_lookahead(labels)

            # --- 확장 forward ---
            fi = fwd_idx.get(sym)
            ext = {}
            if fi:
                j = bisect_right(fi["ts"], dt) - 1
                if j >= 0 and fi["ts"][j] == dt:
                    ext = compute_extended_forwards(
                        fi["bars"][j], closes=fi["closes"], trading_days=fi["days"],
                        day_horizons=day_horizons)
                else:
                    stats["anchor_bar_missing"] += 1
            else:
                # 후보 파일에 있는데 --symbols 로 안 실은 종목. 조용히 빈 forward 를
                # 내보내면 커버리지 구멍이 리포트에서 '데이터 없음'으로 위장된다.
                stats["symbol_not_loaded"] += 1
            for key, fw in ext.items():
                stats[f"fwd_{key}_" + ("ok" if fw["complete"] else fw["incomplete_reason"])] += 1

            row["regime"] = labels.to_dict()
            # ⚠ 음성 대조군 — 당일 마감을 쓴 **오염된** 하루 국면. `regime` 블록 **밖에** 두어
            #    `assert_no_lookahead` 의 불변식과 분석 경로를 건드리지 않는다. 리포트의
            #    대조군 절에서 "차단 장치가 실제로 일을 하는가"를 보이는 데만 쓴다.
            if k >= 0:
                ctl = daily_context_including(bm["daily_days"], bm["daily_ctxs"],
                                              date.fromisoformat(row["et_date"]))
                row["lookahead_control"] = {
                    "daily_trend_same_day": trend_of(ctl),
                    "daily_asof_same_day": (None if ctl is None
                                            else ctl.benchmark_source_bar_end)}
            else:
                row["lookahead_control"] = {"daily_trend_same_day": None,
                                            "daily_asof_same_day": None}
            row["forwards_ext"] = ext
            fout.write(json.dumps(row, ensure_ascii=False) + "\n")
            n_out += 1

    os.replace(tmp_path, out_path)      # 원자적 교체 — 여기 도달해야 결과가 보인다

    print(f"\n조인 완료 → {out_path}  ({n_out:,d} 행)")
    for k in sorted(stats):
        print(f"  {k:32s} {stats[k]:,d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
