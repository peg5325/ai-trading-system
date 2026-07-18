#!/usr/bin/env python
"""H2-b 재현성 분석 — vwap_position/volume 의 음(−)의 예측력이 재현되는가.

작업지시서: reports/WORKORDER_H2B_BACKFILL.md · 선행: reports/h2_h3_preliminary_s001.md

read-only 연구 산출물. 매매 로직·스코어 가중치·runtime config 를 일절 건드리지 않는다.
"재현됨"도 "재현 안 됨"도 똑같이 성공이며, 결과에 따라 스코어를 고치지 않는다.

산출:
  reports/h2b_backfill_validation.md    (사람용)
  reports/h2b_backfill_validation.json  (작업지시서 §6 스키마)
  datasets/h2b_candidates_<range>.jsonl (후보 데이터셋)

재현 커맨드는 리포트 말미에 자동 기록된다.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.candidate_stream import (
    CandidateStreamExtractor,
    align_live_outcome,
    completed_returns,
    load_1m_candles,
)
from src.history.correlation import (
    assert_official_method,
    bonferroni_threshold,
    loso_sign_stability,
    spearman,
)

# 작업지시서 §2 — 종목 선정은 "수익 날 종목"이 아니라 "가설을 반증/일반화 검증하는 종목".
TIERS = {
    "SOXL": "T1", "QQQ": "T1", "NVDA": "T1",          # 연속성(라이브 대조)
    "SOXX": "T2", "SOXS": "T2",                        # 레버리지 자연실험
    "AMD": "T3", "AVGO": "T3",                         # 섹터 내 일반화
    "MSFT": "T4", "GOOGL": "T4", "VST": "T4",          # 섹터 밖 일반화
    "SPY": "T5", "XLU": "T5",                          # 대조군(반증)
}
TIER_LABEL = {
    "T1": "연속성", "T2": "레버리지실험", "T3": "섹터 내",
    "T4": "섹터 밖", "T5": "대조군",
}
# 레버리지 그룹 — 방향까지 구분한다(KNOWN_LEVERAGE 는 배수만 알려준다).
LEVERAGE_GROUP = {"SOXL": "3x_long", "SOXS": "3x_inverse"}

FEATURES = ("candidate_score", "trend", "momentum", "volume",
            "vwap_position", "volatility", "volume_ratio")
# H2-b 의 본 질문 대상.
H2B_FEATURES = ("vwap_position", "volume")
# s001 에서 한쪽 지평만 유의 → 위양성 후보.
SUSPECT_FEATURES = ("momentum", "volatility")

PARITY_TOLERANCE = 1e-6


def leverage_group(symbol: str) -> str:
    return LEVERAGE_GROUP.get(symbol.upper(), "1x")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# 1) 패리티
# ---------------------------------------------------------------------------

def run_parity(journal_db: str, market_db: str, horizons) -> dict:
    """us-phase6-s001 라이브 재현. 실패해도 예외로 죽지 않고 상태를 반환한다."""
    out = {
        "target_session": "us-phase6-s001", "target_date": "2026-07-15",
        "live_candidates": 0, "reconstructed_candidates": 0, "matched_candidates": 0,
        "score_max_abs_diff": 0.0, "component_max_abs_diff": 0.0,
        "invariant_ok": True, "status": "SKIPPED", "notes": "",
    }
    if not (os.path.exists(journal_db) and os.path.exists(market_db)):
        out["notes"] = f"아티팩트 없음: {journal_db} / {market_db}"
        return out

    con = sqlite3.connect(f"file:{journal_db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT cycle_ts, data FROM journal_cycle ORDER BY cycle_ts").fetchall()
    finally:
        con.close()
    live = []
    for cts, data in rows:
        d = json.loads(data)
        for o in d.get("outcomes") or []:
            live.append({"cycle_ts": cts, "symbol": o["symbol"],
                         "snapshot": o.get("indicator_snapshot"),
                         "candidate": o.get("candidate")})

    ex = CandidateStreamExtractor(horizons_min=horizons)
    prepared = {}
    for sym in sorted({r["symbol"] for r in live}):
        candles = load_1m_candles(market_db, sym, table="candle")
        prepared[sym] = ex.prepare(candles, sym)

    live_cands = [r for r in live if r["candidate"]]
    out["live_candidates"] = len(live_cands)
    score_max = comp_max = 0.0
    matched = recon = 0
    unaligned = 0
    offsets: dict[int, int] = {}
    invariant_ok = True
    for r in live:
        if not r["snapshot"]:
            continue
        bars, ind = prepared[r["symbol"]]
        i = align_live_outcome(ind, r["snapshot"])
        if i is None:
            unaligned += 1
            continue
        bar_ts = ind.index[i].to_pydatetime()
        cts = datetime.fromisoformat(r["cycle_ts"].replace("Z", "+00:00"))
        off = int((bar_ts - cts).total_seconds() // 60)
        offsets[off] = offsets.get(off, 0) + 1
        if not r["candidate"]:
            continue
        feats_live = r["candidate"].get("features") or {}
        lev = feats_live.get("leverage_factor") or 1
        cand = ex.screen_at(r["symbol"], ind, i, lev)
        if cand is None:
            continue
        recon += 1
        matched += 1
        score_max = max(score_max, abs(float(cand.score) - float(r["candidate"]["score"])))
        lc = feats_live.get("score_components") or {}
        rc = (cand.features or {}).get("score_components") or {}
        for name, lv in lc.items():
            rv = rc.get(name)
            if not rv:
                comp_max = float("inf")
                continue
            for fld in ("normalized_score", "weight", "weighted_score"):
                comp_max = max(comp_max, abs(float(rv[fld]) - float(lv[fld])))
        f = cand.features or {}
        total = sum(c["weighted_score"] for c in rc.values())
        if abs(total - f["weighted_component_sum"]) > 1e-9 or \
           abs((total - f["penalty_score"]) - f["pre_clamp_score"]) > 1e-9:
            invariant_ok = False

    out.update({
        "reconstructed_candidates": recon, "matched_candidates": matched,
        "score_max_abs_diff": score_max, "component_max_abs_diff": comp_max,
        "invariant_ok": invariant_ok,
        "bar_offset_distribution": {str(k): v for k, v in sorted(offsets.items())},
        "unaligned_outcomes": unaligned,
    })
    ok = (unaligned == 0 and recon == len(live_cands) and invariant_ok
          and score_max <= PARITY_TOLERANCE and comp_max <= PARITY_TOLERANCE)
    out["status"] = "PASS" if ok else "FAIL"
    if ok:
        out["notes"] = (f"후보 {recon}건 전건 일치. 봉 오프셋 분포={out['bar_offset_distribution']} "
                        f"(라이브 루프 지연에 따라 [T,T+5)/[T-5,T) 가 섞인다 — 지문 정렬로 흡수).")
    else:
        out["notes"] = (f"불일치: unaligned={unaligned}, recon={recon} vs live={len(live_cands)}, "
                        f"score_max={score_max}, comp_max={comp_max}, invariant_ok={invariant_ok}")
    return out


# ---------------------------------------------------------------------------
# 2) 후보 스트림
# ---------------------------------------------------------------------------

def collect_observations(db: str, symbols: list[str], horizons,
                         min_days: int) -> tuple[list, dict]:
    ex = CandidateStreamExtractor(horizons_min=horizons)
    all_obs = []
    coverage = {"requested_symbols": list(symbols), "fetched_symbols": [],
                "failed_symbols": [], "per_symbol": []}
    for sym in symbols:
        try:
            candles = load_1m_candles(db, sym)
        except Exception as exc:  # noqa: BLE001 — 종목 격리
            coverage["failed_symbols"].append({"symbol": sym, "reason": f"load_error: {exc!r}"})
            continue
        if not candles:
            coverage["failed_symbols"].append(
                {"symbol": sym, "reason": "no 1m candles in history db (backfill 미수집/실패)"})
            continue
        obs = ex.extract(sym, candles, market="US")
        days = sorted({c.ts_exchange.date().isoformat() for c in candles})
        cand_days = sorted({o.et_date for o in obs})
        entry = {
            "symbol": sym, "tier": TIERS.get(sym, "?"),
            "leverage_group": leverage_group(sym),
            "bars": len(candles), "trading_days": len(days),
            "start": candles[0].ts_utc.isoformat(), "end": candles[-1].ts_utc.isoformat(),
            "missing_bars": 0, "candidates": len(obs),
            "candidate_days": len(cand_days),
        }
        coverage["per_symbol"].append(entry)
        coverage["fetched_symbols"].append(sym)
        if len(days) < min_days:
            coverage["failed_symbols"].append(
                {"symbol": sym,
                 "reason": f"insufficient_history: {len(days)} 거래일 < 요구 {min_days} "
                           f"(수집은 성공했으나 분석 표본 부족 — 집계에는 포함)"})
        all_obs.extend(obs)
    return all_obs, coverage


# ---------------------------------------------------------------------------
# 3) 상관 — 분해축별
# ---------------------------------------------------------------------------

def _corr(obs, feature, horizon) -> dict:
    xs, ys = completed_returns(obs, horizon, feature)
    r = spearman(xs, ys)
    return {"feature": feature, "horizon_min": horizon, **r.to_dict()}


def analyze(obs, horizons, alpha: float) -> dict:
    tests = len(FEATURES) * len(horizons)
    thr = bonferroni_threshold(alpha, tests)

    overall = []
    for f in FEATURES:
        for h in horizons:
            row = _corr(obs, f, h)
            row["bonferroni_pass"] = (row["p"] is not None and row["p"] < thr)
            overall.append(row)

    def grouped(key_fn, key_name):
        out = []
        buckets = defaultdict(list)
        for o in obs:
            buckets[key_fn(o)].append(o)
        for key in sorted(buckets):
            for f in FEATURES:
                for h in horizons:
                    out.append({key_name: key, **_corr(buckets[key], f, h)})
        return out

    by_symbol = grouped(lambda o: o.symbol, "symbol")
    by_tier = grouped(lambda o: TIERS.get(o.symbol, "?"), "tier")
    by_leverage = grouped(lambda o: leverage_group(o.symbol), "group")
    by_date = grouped(lambda o: o.et_date, "date")

    # 부호 안정성 — LOSO(leave-one-day-out)
    sign_stability = []
    for f in FEATURES:
        for h in horizons:
            groups = {}
            per_day = defaultdict(list)
            for o in obs:
                per_day[o.et_date].append(o)
            for day, day_obs in per_day.items():
                xs, ys = completed_returns(day_obs, h, f)
                if xs:
                    groups[day] = (xs, ys)
            st = loso_sign_stability(groups, f, h)
            if st:
                sign_stability.append(st.to_dict())

    survivors = sorted({r["feature"] for r in overall if r["bonferroni_pass"]})
    return {
        "overall": overall, "by_tier": by_tier, "by_symbol": by_symbol,
        "by_leverage": by_leverage, "by_date": by_date,
        "sign_stability": sign_stability,
        "multiple_comparison": {"tests_run": tests, "alpha": alpha,
                                "bonferroni_threshold": thr, "survivors": survivors},
    }


def official_validation(obs, horizons, embargo_min: int, ratio: float = 0.7) -> list:
    """chronological / embargo 홀드아웃. random CV 는 구조적으로 불가능하다."""
    rows = []
    ordered = sorted(obs, key=lambda o: o.bar_ts_utc)
    if len(ordered) < 10:
        return rows
    k = int(len(ordered) * ratio)
    for method in ("chronological", "embargo"):
        assert_official_method(method)
        emb = embargo_min if method == "embargo" else 0
        train = ordered[:k]
        if not train:
            continue
        split_ts = datetime.fromisoformat(train[-1].bar_ts_utc)
        test = [o for o in ordered[k:]
                if (datetime.fromisoformat(o.bar_ts_utc) - split_ts).total_seconds() / 60.0 > emb]
        if not test:
            continue
        for f in H2B_FEATURES:
            for h in horizons:
                tr = _corr(train, f, h)
                te = _corr(test, f, h)
                rows.append({
                    "method": method, "embargo_min": emb, "feature": f, "horizon_min": h,
                    "train": {"n": tr["n"], "rho": tr["rho"], "p": tr["p"]},
                    "test": {"n": te["n"], "rho": te["rho"], "p": te["p"]},
                    "sign_holds": (tr["rho"] is not None and te["rho"] is not None
                                   and (tr["rho"] < 0) == (te["rho"] < 0)),
                })
    return rows


# ---------------------------------------------------------------------------
# 4) 판정
# ---------------------------------------------------------------------------

def verdict(h2b: dict, coverage: dict, parity: dict, horizons,
            min_days: int, min_symbols: int) -> dict:
    limits = []
    usable = [s for s in coverage["per_symbol"] if s["trading_days"] >= min_days]
    n_days = max([s["trading_days"] for s in coverage["per_symbol"]], default=0)
    n_syms = len(usable)

    if parity["status"] != "PASS":
        return {"h2b_replicated": "inconclusive", "confidence": "none",
                "leverage_dependent": "inconclusive",
                "summary": ("패리티가 PASS 가 아니라 재구성을 신뢰할 수 없다. "
                            "H2-b 결론을 내지 않는다(작업지시서 §3)."),
                "limitations": [f"parity_status={parity['status']}: {parity['notes']}"]}

    idx = {(r["feature"], r["horizon_min"]): r for r in h2b["overall"]}
    stab = {(r["feature"], r["horizon_min"]): r for r in h2b["sign_stability"]}
    thr = h2b["multiple_comparison"]["bonferroni_threshold"]

    checks = []
    for f in H2B_FEATURES:
        for h in horizons:
            r = idx.get((f, h))
            if not r or r["rho"] is None:
                checks.append((f, h, False, "표본 부족"))
                continue
            neg = r["rho"] < 0
            sig = r["p"] is not None and r["p"] < thr
            checks.append((f, h, neg and sig,
                           f"rho={r['rho']:+.3f} p={r['p']:.4g} neg={neg} bonf={sig}"))
    n_pass = sum(1 for c in checks if c[2])

    # 레버리지 의존성 — 그룹별 부호가 갈리면 "효과가 레버리지 때문"이라는 신호.
    lev_rows = [r for r in h2b["by_leverage"]
                if r["feature"] in H2B_FEATURES and r["rho"] is not None]
    groups_present = {r["group"] for r in lev_rows}
    if len(groups_present) < 2:
        lev_dep = "inconclusive"
        limits.append(f"레버리지 그룹이 {sorted(groups_present)} 뿐 — 자연실험 비교 불가")
    else:
        signs = defaultdict(set)
        for r in lev_rows:
            signs[r["group"]].add(r["rho"] < 0)
        consistent = all(len(v) == 1 for v in signs.values())
        same_across = len({next(iter(v)) for v in signs.values() if len(v) == 1}) == 1
        lev_dep = "no" if (consistent and same_across) else "yes"

    if n_syms < min_symbols or n_days < min_days:
        rep, conf = "inconclusive", "none"
        limits.append(
            f"커버리지 부족: {min_days}거래일 이상 확보 종목 {n_syms}개(요구 {min_symbols}), "
            f"최대 거래일 {n_days}일(요구 {min_days}). 다른 종목으로의 일반화를 판정할 수 없다.")
    elif n_pass == len(checks):
        rep = "yes"
        ratios = [stab[(f, h)]["same_sign_ratio"] for f in H2B_FEATURES for h in horizons
                  if (f, h) in stab]
        m = min(ratios) if ratios else 0.0
        conf = "strong" if m >= 0.9 else ("moderate" if m >= 0.7 else "weak")
    elif n_pass == 0:
        # "재현 안 됨"의 확신도는 near-miss 유무로 낮춘다. 부호는 s001 과 같은데
        # 보정 전 p 만 유의한 경우를 "강한 부재 증거"라고 부르면 과장이다.
        near = [(f, h) for f, h in ((f, h) for f in H2B_FEATURES for h in horizons)
                if (r := idx.get((f, h))) and r["rho"] is not None and r["rho"] < 0
                and r["p"] is not None and r["p"] < 0.05]
        rep = "no"
        conf = "moderate" if near else "strong"
        if near:
            limits.append(
                "near-miss 존재(부호는 s001 과 같은 음(−), 보정 전 p<0.05 이나 Bonferroni 미통과): "
                + ", ".join(f"{f}@{h}m" for f, h in near)
                + " → 표본이 늘면 판정이 뒤집힐 수 있다.")
    else:
        rep, conf = "inconclusive", "weak"
        limits.append(f"H2-b 4개 검정(2 feature × 2 지평) 중 {n_pass}개만 통과 — 부분 재현")

    # 항상 명시하는 방법론적 한계 — 조용히 넘어가면 리포트가 실제보다 강해 보인다.
    limits.append(
        "추출기는 종목을 개별 스크리닝하므로 candidate.rank 가 항상 1 이다(라이브는 종목 간 "
        "순위 경쟁). 점수·컴포넌트에는 영향이 없어 패리티는 성립하지만, rank 기반 분석에는 쓸 수 없다.")
    limits.append(
        "후보는 스크리너가 min_score(45) 이상으로 뽑은 것만이라 표본이 이미 선택편향돼 있다. "
        "여기의 상관은 '후보 안에서의 서열-수익 관계'이지 '전 구간에서의 예측력'이 아니다.")
    limits.append(
        "1분봉을 5분봉으로 재구성한 뒤의 관측이며, 라이브의 체결/슬리피지/비용은 반영하지 않은 "
        "종가 기준 forward return 이다.")

    detail = "; ".join(f"{f}@{h}m {msg}" for f, h, _, msg in checks)
    summary = (f"패리티 PASS 하에 H2-b 판정 = {rep}. "
               f"vwap_position/volume 4개 검정 중 {n_pass}개가 음(−)+Bonferroni 통과. "
               f"[{detail}]")
    return {"h2b_replicated": rep, "confidence": conf, "leverage_dependent": lev_dep,
            "summary": summary, "limitations": limits}


# ---------------------------------------------------------------------------
# 5) 리포트
# ---------------------------------------------------------------------------

def _fmt(v, nd=3):
    if v is None:
        return "—"
    if isinstance(v, float):
        if abs(v) < 1e-4 and v != 0:
            return f"{v:.2e}"
        return f"{v:.{nd}f}"
    return str(v)


def _sig(p, thr):
    if p is None:
        return ""
    if p < thr:
        return " ★"
    if p < 0.05:
        return " ·"
    return ""


def render_md(payload: dict, cmd: str) -> str:
    meta, cov, par, h2b, ver = (payload["meta"], payload["coverage"],
                                payload["parity_test"], payload["h2b"], payload["verdict"])
    thr = h2b["multiple_comparison"]["bonferroni_threshold"]
    horizons = meta["forward_horizons_min"]
    L = []
    A = L.append
    A("# H2-b Backfill 검증 리포트")
    A("")
    A(f"> 생성: {meta['generated_at']} · 커밋 `{meta['producer_commit'][:12]}` · "
      f"DB `{meta['db']}` · interval {meta['interval']}")
    A(f"> 작업지시서: `reports/WORKORDER_H2B_BACKFILL.md` · 선행: `reports/h2_h3_preliminary_s001.md`")
    A("")
    A("> **read-only 연구 산출물.** 매매 로직·스코어 가중치·runtime config 변경 0건.")
    A("> \"재현됨\"과 \"재현 안 됨\"은 똑같이 성공이며, 결과에 따라 스코어를 고치지 않았다.")
    A("")

    # ① 판정
    A("## ① 판정 요약")
    A("")
    A(f"| 항목 | 값 |")
    A(f"|---|---|")
    A(f"| **H2-b 재현** | **{ver['h2b_replicated'].upper()}** |")
    A(f"| 확신도 | {ver['confidence']} |")
    A(f"| 레버리지 의존성 | {ver['leverage_dependent']} |")
    A(f"| 패리티 | **{par['status']}** |")
    A(f"| Bonferroni 생존 feature | {', '.join(h2b['multiple_comparison']['survivors']) or '없음'} |")
    A("")
    A(ver["summary"])
    A("")

    # ② 패리티
    A("## ② 패리티 (수용 기준)")
    A("")
    A("재구성이 라이브를 재현하지 못하면 재구성으로 만든 어떤 상관도 의미가 없다.")
    A("")
    A("| 항목 | 기준 | 실측 |")
    A("|---|---|---|")
    A(f"| 대상 세션 | — | {par['target_session']} ({par['target_date']}) |")
    A(f"| 라이브 후보 | — | {par['live_candidates']} |")
    A(f"| 재구성 후보 | 라이브와 일치 | {par['reconstructed_candidates']} |")
    A(f"| `candidate_score` 최대오차 | ≤ 1e-6 | **{par['score_max_abs_diff']:.3g}** |")
    A(f"| 컴포넌트 최대오차 | ≤ 1e-6 | **{par['component_max_abs_diff']:.3g}** |")
    A(f"| 불변식 | OK | {'OK' if par['invariant_ok'] else 'FAIL'} |")
    A(f"| **판정** | PASS | **{par['status']}** |")
    A("")
    if par.get("bar_offset_distribution"):
        A(f"봉 오프셋 분포(사이클 예정시각 대비): `{par['bar_offset_distribution']}` — "
          "저널 `cycle_ts` 는 봉 라벨이 아니라 예정 시각이라 루프 지연에 따라 실제 사용 봉이 "
          "달라진다. 시계가 아니라 **지표 지문**으로 정렬해 이를 흡수했다.")
        A("")
    if par["notes"]:
        A(f"비고: {par['notes']}")
        A("")

    # ③ 커버리지
    A("## ③ 커버리지")
    A("")
    A(f"요청 {len(cov['requested_symbols'])}종목 · 수집 {len(cov['fetched_symbols'])}종목")
    A("")
    A("| 종목 | Tier | 레버리지 | 봉 | 거래일 | 후보 | 후보발생일 |")
    A("|---|---|---|---:|---:|---:|---:|")
    for s in sorted(cov["per_symbol"], key=lambda x: (x["tier"], x["symbol"])):
        A(f"| {s['symbol']} | {s['tier']} {TIER_LABEL.get(s['tier'],'')} | {s['leverage_group']} "
          f"| {s['bars']:,} | {s['trading_days']} | {s['candidates']:,} | {s['candidate_days']} |")
    A("")
    if cov["failed_symbols"]:
        A("### 실패·부족 종목 (조용히 빼지 않고 명시)")
        A("")
        A("| 종목 | 사유 |")
        A("|---|---|")
        for f in cov["failed_symbols"]:
            A(f"| {f['symbol']} | {f['reason']} |")
        A("")

    # ④ 상관
    A("## ④ H2-b 상관 — 전체")
    A("")
    A(f"Spearman(feature, forward return). ★ = Bonferroni 통과(p < {thr:.4f}), · = p<0.05 (미보정)")
    A("")
    head = "| feature | " + " | ".join(f"{h}m rho | {h}m p | n" for h in horizons) + " |"
    A(head)
    A("|---" * (1 + 3 * len(horizons)) + "|")
    idx = {(r["feature"], r["horizon_min"]): r for r in h2b["overall"]}
    for f in FEATURES:
        cells = []
        for h in horizons:
            r = idx.get((f, h), {})
            cells += [f"{_fmt(r.get('rho'))}{_sig(r.get('p'), thr)}",
                      _fmt(r.get("p"), 4), str(r.get("n", 0))]
        mark = "**" if f in H2B_FEATURES else ""
        A(f"| {mark}{f}{mark} | " + " | ".join(cells) + " |")
    A("")

    for title, key, col in (("Tier별", "by_tier", "tier"),
                            ("레버리지 그룹별", "by_leverage", "group"),
                            ("종목별", "by_symbol", "symbol")):
        A(f"### {title} (H2-b feature 만)")
        A("")
        A(f"| {col} | feature | " + " | ".join(f"{h}m rho | {h}m p | n" for h in horizons) + " |")
        A("|---" * (2 + 3 * len(horizons)) + "|")
        rows = defaultdict(dict)
        for r in h2b[key]:
            if r["feature"] in H2B_FEATURES:
                rows[(r[col], r["feature"])][r["horizon_min"]] = r
        for (kv, f) in sorted(rows):
            cells = []
            for h in horizons:
                r = rows[(kv, f)].get(h, {})
                cells += [f"{_fmt(r.get('rho'))}{_sig(r.get('p'), thr)}",
                          _fmt(r.get("p"), 4), str(r.get("n", 0))]
            label = f"{kv} {TIER_LABEL.get(kv,'')}" if col == "tier" else kv
            A(f"| {label} | {f} | " + " | ".join(cells) + " |")
        A("")

    # ⑤ 부호 안정성
    A("## ⑤ 부호 안정성 (LOSO, leave-one-day-out)")
    A("")
    A("각 fold = 그 날을 **뺀** 나머지로 rho 재계산. 단일 p값보다 이 일치율이 재현성의 실질 근거다.")
    A("")
    A("| feature | 지평 | folds | 부호일치 | 일치율 | 전체부호 |")
    A("|---|---:|---:|---:|---:|---:|")
    for r in h2b["sign_stability"]:
        sign = "−" if r["overall_sign"] < 0 else ("+" if r["overall_sign"] > 0 else "0")
        A(f"| {r['feature']} | {r['horizon_min']}m | {r['folds']} | {r['same_sign_folds']} "
          f"| {r['same_sign_ratio']:.2f} | {sign} |")
    A("")

    # 공식 검증
    if payload.get("official_validation"):
        A("## ⑤-b 공식 검증 (chronological / embargo)")
        A("")
        A("random CV 는 시계열 누수라 **구조적으로 금지**되어 있다"
          "(`OFFICIAL_SPLIT_METHODS` 강제, 위반 시 예외).")
        A("")
        A("| 방법 | embargo | feature | 지평 | train rho (n) | test rho (n) | 부호유지 |")
        A("|---|---:|---|---:|---|---|---|")
        for r in payload["official_validation"]:
            A(f"| {r['method']} | {r['embargo_min']}m | {r['feature']} | {r['horizon_min']}m "
              f"| {_fmt(r['train']['rho'])} ({r['train']['n']}) "
              f"| {_fmt(r['test']['rho'])} ({r['test']['n']}) "
              f"| {'O' if r['sign_holds'] else 'X'} |")
        A("")

    # ⑥ 다중검정
    mc = h2b["multiple_comparison"]
    A("## ⑥ 다중검정")
    A("")
    A(f"검정 {mc['tests_run']}회({len(FEATURES)} feature × {len(horizons)} 지평), "
      f"α={mc['alpha']} → Bonferroni 임계 **{mc['bonferroni_threshold']:.4f}**")
    A("")
    A(f"- **생존자(재현됨으로 표기 가능):** {', '.join(mc['survivors']) or '없음'}")
    A(f"- 위양성 후보(s001 에서 한쪽 지평만 유의): {', '.join(SUSPECT_FEATURES)} — "
      "생존 못 하면 폐기 대상")
    A("")

    # ⑦ 한계
    A("## ⑦ 한계")
    A("")
    for lim in ver["limitations"] or ["(명시된 한계 없음)"]:
        A(f"- {lim}")
    A("")

    # ⑧ 재현
    A("## ⑧ 재현 커맨드")
    A("")
    A("```bash")
    A("# 1) backfill (gap-skip 필수 — 없으면 하루치에서 멈춘다, #45)")
    A("python scripts/history_backfill.py \\")
    A("  --symbols SOXL,QQQ,NVDA,SOXX,SOXS,AMD,AVGO,MSFT,GOOGL,VST,SPY,XLU \\")
    A("  --interval 1m --max-pages 260 --gap-skip-minutes 200 --spacing 1.2")
    A("")
    A("# 2) 패리티 회귀 (아티팩트 있는 환경에서 강제)")
    A("H2B_REQUIRE_PARITY=1 python -m pytest tests/test_s001_parity.py -q")
    A("")
    A("# 3) 분석 리포트")
    A(cmd)
    A("```")
    A("")
    return "\n".join(L)


# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="H2-b 재현성 분석(read-only)")
    ap.add_argument("--db", default="data/market_history.sqlite")
    ap.add_argument("--symbols", default=",".join(TIERS))
    ap.add_argument("--horizons", default="30,60")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--embargo-min", type=int, default=60,
                    help="embargo 홀드아웃 갭(분) — forward 지평 이상이어야 한다")
    ap.add_argument("--min-days", type=int, default=20, help="종목당 요구 거래일")
    ap.add_argument("--min-symbols", type=int, default=6,
                    help="일반화 판정에 필요한 최소 종목 수")
    ap.add_argument("--parity-journal", default="data/journal_us_phase6_s001.sqlite")
    ap.add_argument("--parity-market", default="data/market_us_phase6_s001.sqlite")
    ap.add_argument("--out-md", default="reports/h2b_backfill_validation.md")
    ap.add_argument("--out-json", default="reports/h2b_backfill_validation.json")
    ap.add_argument("--out-jsonl", default=None)
    args = ap.parse_args(argv)

    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    if args.embargo_min < max(horizons):
        print(f"embargo({args.embargo_min}m) 가 forward 지평({max(horizons)}m)보다 짧다 — "
              f"누수 위험.", file=sys.stderr)
        return 2
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    print(f"[1/5] 패리티 — {args.parity_journal}", file=sys.stderr)
    parity = run_parity(args.parity_journal, args.parity_market, horizons)
    print(f"      status={parity['status']} score_max={parity['score_max_abs_diff']:.3g}",
          file=sys.stderr)

    print(f"[2/5] 후보 스트림 — {len(symbols)}종목", file=sys.stderr)
    obs, coverage = collect_observations(args.db, symbols, horizons, args.min_days)
    print(f"      후보 {len(obs):,}건 / 수집 {len(coverage['fetched_symbols'])}종목",
          file=sys.stderr)

    print("[3/5] 상관 분석", file=sys.stderr)
    h2b = analyze(obs, horizons, args.alpha)
    official = official_validation(obs, horizons, args.embargo_min)

    print("[4/5] 판정", file=sys.stderr)
    ver = verdict(h2b, coverage, parity, horizons, args.min_days, args.min_symbols)

    dates = sorted({o.et_date for o in obs})
    rng = f"{dates[0].replace('-','')}_{dates[-1].replace('-','')}" if dates else "empty"
    meta = {
        "workorder": "WORKORDER_H2B_BACKFILL", "producer_commit": _git_sha(),
        "generated_at": _now_iso(), "db": args.db, "interval": "1m",
        "date_range": {"start": dates[0] if dates else None,
                       "end": dates[-1] if dates else None,
                       "trading_days": len(dates)},
        "forward_horizons_min": list(horizons),
    }
    payload = {"meta": meta, "coverage": coverage, "parity_test": parity,
               "h2b": h2b, "official_validation": official, "verdict": ver}

    print("[5/5] 산출물 기록", file=sys.stderr)
    out_jsonl = args.out_jsonl or f"datasets/h2b_candidates_{rng}.jsonl"
    for path in (args.out_md, args.out_json, out_jsonl):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(out_jsonl, "w", encoding="utf-8") as fh:
        for o in obs:
            fh.write(json.dumps(o.to_dict(), ensure_ascii=False, default=str) + "\n")
    payload["meta"]["dataset"] = out_jsonl
    cmd = ("python scripts/h2b_backfill_analysis.py \\\n"
           f"  --db {args.db} --symbols {args.symbols} \\\n"
           f"  --horizons {args.horizons} --embargo-min {args.embargo_min} "
           f"--min-days {args.min_days}")
    with open(args.out_json, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
    with open(args.out_md, "w", encoding="utf-8") as fh:
        fh.write(render_md(payload, cmd))

    print(f"\nMD   : {args.out_md}\nJSON : {args.out_json}\nJSONL: {out_jsonl} ({len(obs):,}건)")
    print(f"판정 : H2-b={ver['h2b_replicated']} (confidence={ver['confidence']}, "
          f"parity={parity['status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
