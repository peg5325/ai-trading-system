#!/usr/bin/env python3
"""H4 스크리너 선별력(랭킹) 검증 — 사전등록 v1.4(FINAL) 확정 실행본.

기준: docs/research/PREREG_screener_ranking_h4.md §3~§6 을 그대로 구현. read-only 분석.
- arms: top_N(tie-aware 분수배분) / bottom_N / random_N(=전수열거 기대값=풀 평균) / equal_all(항등 검산)
- 지평: 30m·60m(데이터셋), EOD(캔들에서 산출)
- 판정단위: 일-블록. 유의성=Wilcoxon signed-rank(양측·0차이 제거·유효일<6 유의불가), 효과크기=일별값의 산술평균
- OOS: 고유 거래일 앞⌊0.70D⌋ in-sample / 나머지 OOS, embargo=in-sample 경계 1일 제거
- GO: 9셀 중 ≥1이 (a)(b)(d)(e) 전부 충족. else NULL.
"""
from __future__ import annotations
import json, math, sqlite3
from collections import defaultdict
from datetime import datetime
import numpy as np
# scipy 미의존(저장소 판정 경로 관례) — Wilcoxon signed-rank 를 직접 구현한다.


def _iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def wilcoxon_signed_rank_p(diffs):
    """양측 Wilcoxon signed-rank p. scipy 무의존.
    |d| 동점 없으면 **정확(exact) 분포를 DP 로 계산**(scipy exact 재현). 동점 있으면 정규근사 폴백."""
    d = np.array([x for x in diffs if abs(x) > 1e-12], dtype=float)
    n = len(d)
    if n == 0:
        return None
    ad = np.abs(d)
    _, counts = np.unique(ad, return_counts=True)
    has_tie = bool((counts > 1).any())
    if not has_tie:
        return _exact_signed_rank_p(d, ad)
    return _approx_signed_rank_p(d, ad, n)   # 동점 폴백(본 데이터에선 미발동)


def _exact_signed_rank_p(d, ad):
    """정확 양측 p — |d| distinct(순위 1..n) 가정. W+ 의 귀무분포(부분합 {1..n})를 DP 로."""
    n = len(d)
    ranks = _rankdata(ad)                    # distinct → 1..n 정수
    w = int(round(float(ranks[d > 0].sum())))
    total = 1 << n
    maxs = n * (n + 1) // 2
    dp = [0] * (maxs + 1)
    dp[0] = 1
    for r in range(1, n + 1):
        for s in range(maxs, r - 1, -1):
            dp[s] += dp[s - r]
    p_le = sum(dp[:w + 1])
    p_ge = sum(dp[w:])
    return min(2.0 * min(p_le, p_ge) / total, 1.0)


def _approx_signed_rank_p(d, ad, n):
    """정규근사 폴백(동점 시). 연속성·동점 보정."""
    r = _rankdata(ad)
    w_plus = float(r[d > 0].sum())
    mean = n * (n + 1) / 4.0
    _, counts = np.unique(ad, return_counts=True)
    tie = float(((counts ** 3) - counts).sum())
    var = n * (n + 1) * (2 * n + 1) / 24.0 - tie / 48.0
    if var <= 0:
        return None
    z = (w_plus - mean - math.copysign(0.5, w_plus - mean)) / math.sqrt(var)
    return 2.0 * (1.0 - _phi(abs(z)))


def _rankdata(a):
    """평균 순위(scipy.rankdata 동치, scipy 무의존)."""
    a = np.asarray(a)
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a), dtype=float)
    sa = a[order]
    i = 0
    while i < len(a):
        j = i
        while j < len(a) and sa[j] == sa[i]:
            j += 1
        ranks[order[i:j]] = (i + j - 1) / 2.0 + 1.0   # 1-based 평균순위
        i = j
    return ranks

DATASET = "datasets/h2b_candidates_final.jsonl"
MARKET = "data/market_history.sqlite"
MIN_EFFECT = 0.34      # %p 사전 고정 최소 효과크기 (§4)
COST_REAL = 0.34       # %p 실현 왕복비용 (§6(e))
COST_CFG = 0.14        # %p config 이론값 (감도 병기)
BONF = 0.05 / 9        # ≈0.0056
NS = [1, 2, 3]
HORIZONS = ["30", "60", "eod"]


def load_candidates():
    rows = []
    for line in open(DATASET):
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        if o.get("session") not in ("PRE", "REGULAR", "POST"):
            continue
        rows.append(o)
    return rows


def build_eod(symbols, dates):
    """(symbol, et_date) -> (정규장 마감 REGULAR 종가, 그 봉의 ts_utc).
    저장소 공식 EOD 계약(src/history/forward_extended.py:15) 준수. POST/OVERNIGHT 은 마감 아님."""
    con = sqlite3.connect(MARKET)
    eod = {}
    for sym in symbols:
        cur = con.execute(
            "SELECT ts_exchange, ts_utc, close FROM candle_history "
            "WHERE symbol=? AND interval='1m' AND session='REGULAR' ORDER BY ts_utc", (sym,))
        for ts_ex, ts_utc, close in cur:
            d = ts_ex[:10]  # ET date
            eod[(sym, d)] = (close, ts_utc)   # 마지막(정규장 마감)이 남음(오름차순)
    con.close()
    return eod


def horizon_ret(cand, hz, eod):
    """후보의 지평별 forward return(%). 미완결이면 None."""
    if hz in ("30", "60"):
        f = (cand.get("forwards") or {}).get(hz)
        if not f or not f.get("complete"):
            return None
        return f.get("return_pct")
    # EOD: 정규장 마감 종가 대비. 앵커가 POST/OVERNIGHT 면 마감 지남 → 미완결(계약).
    if cand.get("session") not in ("PRE", "REGULAR"):
        return None
    entry = cand.get("close")
    rec = eod.get((cand["symbol"], cand["et_date"]))
    if entry is None or rec is None or entry == 0:
        return None
    ec, ec_ts = rec
    # 계약(forward_extended:113): 마감봉 ts_utc <= 앵커 ts_utc 이면 미완결(eod_passed).
    if _iso(ec_ts) <= _iso(cand["bar_ts_utc"]):
        return None
    return (ec - entry) / entry * 100.0


def tie_aware_weights(scores, N, top=True):
    """점수 리스트 -> 각 후보 가중치(합=N; top_N 이면 상위, 아니면 하위). tie-aware 분수배분."""
    k = len(scores)
    order = sorted(range(k), key=lambda i: (-scores[i] if top else scores[i]))
    w = [0.0] * k
    remaining = N
    i = 0
    while remaining > 1e-12 and i < k:
        # 현재 경계 점수와 동일한 그룹 묶기
        j = i
        cur = scores[order[i]]
        grp = []
        while j < k and scores[order[j]] == cur:
            grp.append(order[j]); j += 1
        if len(grp) <= remaining + 1e-12:
            for g in grp:
                w[g] = 1.0
            remaining -= len(grp)
        else:  # 남은 슬롯을 동점 그룹에 균등 분수 배분
            share = remaining / len(grp)
            for g in grp:
                w[g] = share
            remaining = 0.0
        i = j
    return w


def arm_returns_at_ts(cands, N, hz, eod):
    """한 시점의 arm별 return. 후보 수(유효 지평)≥N+1 아니면 None."""
    scores, rets = [], []
    for c in cands:
        r = horizon_ret(c, hz, eod)
        if r is None:
            continue
        scores.append(c["candidate_score"]); rets.append(r)
    k = len(scores)
    if k < N + 1:
        return None
    scores = np.array(scores); rets = np.array(rets)
    wt = np.array(tie_aware_weights(list(scores), N, top=True))
    wb = np.array(tie_aware_weights(list(scores), N, top=False))
    top_r = float((wt * rets).sum() / wt.sum())
    bot_r = float((wb * rets).sum() / wb.sum())
    rand_r = float(rets.mean())          # 전수열거 기대값 ≡ 풀 평균
    equal_r = float(rets.mean())         # 항등 검산
    return dict(top=top_r, bottom=bot_r, random=rand_r, equal=equal_r, k=k)


def per_day(cands_by_ts_day, N, hz, eod):
    """일별 arm 평균. return {day: {top,bottom,random}}."""
    day_arms = defaultdict(lambda: defaultdict(list))
    for (ts, day), cs in cands_by_ts_day.items():
        a = arm_returns_at_ts(cs, N, hz, eod)
        if a is None:
            continue
        day_arms[day]["top"].append(a["top"])
        day_arms[day]["bottom"].append(a["bottom"])
        day_arms[day]["random"].append(a["random"])
        day_arms[day]["equal"].append(a["equal"])
    out = {}
    for day, d in day_arms.items():
        out[day] = {k: float(np.mean(v)) for k, v in d.items()}
    return out


def signed_rank(diffs):
    """양측 Wilcoxon signed-rank(scipy 무의존). 0차이 제거. 유효<6 -> p=None(유의불가)."""
    d = [x for x in diffs if abs(x) > 1e-12]
    if len(d) < 6:
        return None, len(d)
    p = wilcoxon_signed_rank_p(d)
    return (float(p) if p is not None else None), len(d)


def main():
    cands = load_candidates()
    symbols = sorted({c["symbol"] for c in cands})
    dates = sorted({c["et_date"] for c in cands})
    eod = build_eod(symbols, dates)

    # 시점(ts)×날(day) 그룹
    by_tsday = defaultdict(list)
    for c in cands:
        by_tsday[(c["bar_ts_utc"], c["et_date"])].append(c)

    # OOS 분할: 고유 거래일
    D = len(dates)
    n_in = math.floor(0.70 * D)
    in_days = dates[:n_in]
    embargo_day = in_days[-1] if in_days else None
    in_days = in_days[:-1]                 # embargo: 경계 1일 제거
    oos_days = dates[n_in:]
    in_set, oos_set = set(in_days), set(oos_days)

    results = {"meta": {"dataset": DATASET, "n_candidates": len(cands),
                        "trading_days": D, "in_sample_days": len(in_days),
                        "embargo_day": embargo_day, "oos_days": len(oos_days),
                        "min_effect_pp": MIN_EFFECT, "bonferroni_p": BONF},
               "cells": [], "go_cells": []}

    for N in NS:
        for hz in HORIZONS:
            dayv = per_day(by_tsday, N, hz, eod)
            # 일별 차이
            d_tr = {d: v["top"] - v["random"] for d, v in dayv.items()}
            d_tb = {d: v["top"] - v["bottom"] for d, v in dayv.items()}
            top_cost = {d: v["top"] - COST_REAL for d, v in dayv.items()}
            top_cost14 = {d: v["top"] - COST_CFG for d, v in dayv.items()}
            # equal 항등 검산 (셀별 동일 시점집합)
            id_err = max((abs(v["random"] - v["equal"]) for v in dayv.values()), default=0.0)

            def blk(days, dd):
                vals = [dd[d] for d in days if d in dd]
                return (float(np.mean(vals)) if vals else float("nan"), len(vals))

            a_mean, a_n = blk(in_days, d_tr)
            a_p, a_eff = signed_rank([d_tr[d] for d in in_days if d in d_tr])
            b_mean, _ = blk(in_days, d_tb)
            d_mean, d_n = blk(oos_days, d_tr)
            d_p, d_eff = signed_rank([d_tr[d] for d in oos_days if d in d_tr])
            e_mean, _ = blk(oos_days, top_cost)
            e14_mean, _ = blk(oos_days, top_cost14)

            cond_a = (a_mean >= MIN_EFFECT) and (a_p is not None and a_p < BONF)
            cond_b = (b_mean > 0)
            cond_d = (d_mean >= MIN_EFFECT) and (d_p is not None and d_p < 0.05)
            cond_e = (e_mean > 0)
            go = cond_a and cond_b and cond_d and cond_e

            cell = dict(N=N, horizon=hz,
                        a_insample_top_minus_random_mean=round(a_mean, 4), a_p=a_p, a_eff_days=a_eff,
                        b_insample_top_minus_bottom_mean=round(b_mean, 4),
                        d_oos_top_minus_random_mean=round(d_mean, 4), d_p=d_p, d_eff_days=d_eff,
                        e_oos_top_minus_cost034_mean=round(e_mean, 4),
                        e_oos_top_minus_cost014_mean=round(e14_mean, 4),
                        identity_check_max_abs_err=round(id_err, 8),
                        cond_a=cond_a, cond_b=cond_b, cond_d=cond_d, cond_e=cond_e, GO=go)
            results["cells"].append(cell)
            if go:
                results["go_cells"].append(f"N{N}_{hz}")

    results["verdict"] = "GO" if results["go_cells"] else "NULL"
    print(json.dumps(results, ensure_ascii=False, indent=1))
    with open("reports/h4_screener_ranking.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
