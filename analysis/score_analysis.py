#!/usr/bin/env python3
"""Screener Score <-> 성과 상관분석 (offline, read-only).

각 후보(체결 여부 무관)의 candidate_score / 원시 지표를 진입 이후 forward return과
대조해 스크리너의 예측력을 측정한다. 매매/전략 코드와 무관한 순수 분석 도구.

사용:
    python analysis/score_analysis.py \
        datasets/dataset_us_exit_test_2000usd.jsonl \
        datasets/dataset_krx_exit_test_session004.jsonl \
        --window 30 --out reports/score_correlation.md

의존: numpy, scipy. (표만 필요하면 --no-corr 로 scipy 없이도 구동)
데이터 한계(작성 시점): 컴포넌트 하위점수(Trend/Momentum/Volume...)가 데이터셋에 없어
원시 지표(RSI/VWAP/EMA/StochRSI/BB) 프록시로 대체했다.
→ 이 한계는 #46(FeatureRecord 컴포넌트 export)에서 해소됐다. 컴포넌트 단위 분석은
   scripts/h2b_backfill_analysis.py 를 쓰고, 이 스크립트는 기존 데이터셋 호환용으로 유지한다.

⚠ 통계 기준 주의: 이 스크립트의 별표(*/**/***)는 **다중비교 미보정 탐색치**이며 scipy 에
   의존한다. 같은 저장소의 scripts/h2b_backfill_analysis.py 는 (a) scipy 없이 재현 가능하게
   직접 구현했고 (b) Bonferroni 보정을 병기한다. 판정용 결론은 그쪽을 쓰고, 이 파일은
   기존 1.2 데이터셋 호환 조회용으로만 본다.
"""
from __future__ import annotations
import argparse, json, sys
from collections import Counter

try:
    import numpy as np
    from scipy import stats
    HAVE_SCI = True
except Exception:
    HAVE_SCI = False

BUCKETS = [("<60", 0, 60), ("60-70", 60, 70), ("70-80", 70, 80),
           ("80-90", 80, 90), ("90+", 90, 1e9)]


def load(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            if "__meta__" in o:
                continue
            rows.append(o)
    return rows


def _fwd(rec, w):
    for fo in rec["label"].get("forwards", []):
        if fo["window_min"] == w:
            return fo
    return None


def extract(rows, window):
    """후보 레코드 -> 분석용 dict. score 있고 forward 완성된 것만."""
    out = []
    for r in rows:
        f = r["feature"]
        s = f.get("candidate_score")
        if s is None:
            continue
        fo = _fwd(r, window)
        if not fo or fo.get("forward_return") is None:
            continue
        if not r["label"].get("complete", True):
            continue
        c, v, e7, e20 = f.get("close"), f.get("vwap"), f.get("ema7"), f.get("ema20")
        bbu, bbl = f.get("bb_upper"), f.get("bb_lower")
        rec = dict(score=s, ret=fo["forward_return"], mfe=fo.get("mfe"),
                   mae=fo.get("mae"), taken=r["label"].get("trade_taken"),
                   sym=f.get("symbol"), rsi=f.get("rsi14"),
                   stochrsi=f.get("stochrsi_k"))
        if None not in (c, v):
            rec["vwap_dist"] = (c - v) / v
        if None not in (e7, e20) and e20:
            rec["ema_trend"] = (e7 - e20) / e20
        if None not in (c, bbu, bbl) and bbu > bbl:
            rec["bb_pos"] = (c - bbl) / (bbu - bbl)
            rec["bb_width"] = (bbu - bbl) / c
        out.append(rec)
    return out


def bucket_table(data):
    lines = []
    lines.append("| 점수구간 | n | 평균수익률% | 승률(ret>0) | 평균MFE% | 평균MAE% |")
    lines.append("|---|---|---|---|---|---|")
    for name, lo, hi in BUCKETS:
        d = [x for x in data if lo <= x["score"] < hi]
        if not d:
            lines.append(f"| {name} | 0 | - | - | - | - |")
            continue
        rets = [x["ret"] * 100 for x in d]
        mfe = [x["mfe"] * 100 for x in d if x["mfe"] is not None]
        mae = [x["mae"] * 100 for x in d if x["mae"] is not None]
        win = 100 * sum(1 for r in rets if r > 0) / len(rets)
        mean = sum(rets) / len(rets)
        # mfe/mae 는 데이터셋에 따라 통째로 없을 수 있는 선택 필드다(fo.get).
        # 빈 리스트에 평균을 내면 ZeroDivisionError 로 죽으므로 '-' 로 표기한다.
        mfe_s = f"{(sum(mfe)/len(mfe)):+.2f}" if mfe else "-"
        mae_s = f"{(sum(mae)/len(mae)):+.2f}" if mae else "-"
        lines.append(f"| {name} | {len(d)} | {mean:+.3f} | {win:.1f}% | {mfe_s} | {mae_s} |")
    return "\n".join(lines)


FEATS = [("score", "최종점수"), ("rsi", "RSI(모멘텀)"), ("vwap_dist", "VWAP거리"),
         ("ema_trend", "EMA추세"), ("stochrsi", "StochRSI"),
         ("bb_pos", "BB위치"), ("bb_width", "BB폭(변동성)")]


def corr_table(data):
    if not HAVE_SCI:
        return "_(scipy 미설치 — 상관표 생략)_"
    ret = np.array([x["ret"] for x in data])
    lines = ["| 지표 | rho | p |", "|---|---|---|"]
    for key, lab in FEATS:
        vals = np.array([x.get(key, np.nan) for x in data], dtype=float)
        m = ~np.isnan(vals)
        if m.sum() < 5:
            continue
        rho, p = stats.spearmanr(vals[m], ret[m])
        sig = "***" if p < .01 else ("**" if p < .05 else ("*" if p < .1 else ""))
        lines.append(f"| {lab} | {rho:+.3f} | {p:.3f} {sig} |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("datasets", nargs="+", help="dataset_*.jsonl 경로(여럿=pool)")
    ap.add_argument("--window", type=int, default=30, help="forward window(min)")
    ap.add_argument("--out", default=None, help="markdown 출력 경로")
    args = ap.parse_args()

    per, pooled = {}, []
    for path in args.datasets:
        data = extract(load(path), args.window)
        per[path] = data
        pooled += data

    md = [f"# Screener Score ↔ 성과 상관분석 (auto, window={args.window}m)", ""]
    for path, data in per.items():
        md += [f"## {path}  (n={len(data)})", "",
               f"체결 후보: {sum(1 for x in data if x['taken'])} / 전체 {len(data)}", "",
               bucket_table(data), "", "지표 상관(Spearman):", corr_table(data), ""]
    if len(per) > 1:
        md += [f"## POOLED (n={len(pooled)})", "", bucket_table(pooled), "",
               "지표 상관(Spearman):", corr_table(pooled), ""]

    text = "\n".join(md)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        print(f"written: {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
