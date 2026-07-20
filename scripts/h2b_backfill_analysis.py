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

# 선행 관측(reports/h2_h3_preliminary_s001.md §1) — 재현 여부는 이 값과의 **방향 비교**다.
# 이 기준선이 없으면 "유의함"이 곧 "재현됨"으로 오독된다.
S001_BASELINE = {
    ("vwap_position", 30): -0.329, ("vwap_position", 60): -0.285,
    ("volume", 30): -0.162, ("volume", 60): -0.249,
    ("candidate_score", 30): 0.116, ("candidate_score", 60): 0.107,
    ("trend", 30): 0.128, ("trend", 60): 0.106,
    ("momentum", 30): 0.178, ("momentum", 60): 0.071,
    ("volatility", 30): 0.074, ("volatility", 60): 0.196,
    ("volume_ratio", 30): -0.179, ("volume_ratio", 60): -0.255,
}

# 실용적 유의미성 하한. n 이 7만이면 rho=0.02 도 p<1e-8 이 된다 — 통계적 유의성만으로
# "재현됨"을 말하면 안 된다. |rho|=0.05 는 설명력(rho^2) 0.25% 로, s001 최소 효과(0.162)의
# 약 1/3 이다. 이 아래는 "부호는 있으나 크기는 사실상 0" 으로 분류한다.
MIN_PRACTICAL_RHO = 0.05

PARITY_TOLERANCE = 1e-6

# s001(발견 표본)의 세션 날짜. 같은 날을 재현 표본에 넣으면 발견과 재현이 겹쳐
# 순환 검증이 된다 → 기본적으로 제외하고, 포함 여부를 meta 에 기록한다.
DISCOVERY_DATES = ("2026-07-15",)


def rho_fmt(v) -> str:
    return "—" if v is None else f"{v:+.3f}"


def leverage_group(symbol: str) -> str:
    return LEVERAGE_GROUP.get(symbol.upper(), "1x")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_sha() -> str:
    """HEAD sha. 워킹트리가 더티면 -dirty 접미 — 산출물의 재현 근거를 정확히 남긴다."""
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                               text=True, check=True).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
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
    try:
        return _run_parity_inner(journal_db, market_db, horizons, out)
    except Exception as exc:  # noqa: BLE001 — 계약: 예외로 죽지 않고 상태를 반환
        out["status"] = "ERROR"
        out["notes"] = f"패리티 실행 중 예외: {exc!r}"
        return out


def _run_parity_inner(journal_db: str, market_db: str, horizons, out: dict) -> dict:
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
    comp_comparisons = 0        # 0 이면 컴포넌트 게이트가 공허하게 통과한 것
    unmatched_components = 0    # 라이브에 있는데 재구성에 없는 컴포넌트
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
                unmatched_components += 1     # inf 대신 계수(Infinity 는 비표준 JSON)
                continue
            for fld in ("normalized_score", "weight", "weighted_score"):
                comp_max = max(comp_max, abs(float(rv[fld]) - float(lv[fld])))
                comp_comparisons += 1
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
        "component_comparisons": comp_comparisons,
        "unmatched_components": unmatched_components,
    })
    # comp_max 는 0.0 에서 시작하므로 비교가 한 번도 없으면 공허하게 통과한다.
    # H2-b 는 전적으로 컴포넌트에 대한 가설이라 그 게이트가 비면 PASS 로 볼 수 없다.
    ok = (unaligned == 0 and recon == len(live_cands) and invariant_ok
          and comp_comparisons > 0 and unmatched_components == 0
          and score_max <= PARITY_TOLERANCE and comp_max <= PARITY_TOLERANCE)
    out["status"] = "PASS" if ok else "FAIL"
    if ok:
        out["notes"] = (f"후보 {recon}건 전건 일치. 봉 오프셋 분포={out['bar_offset_distribution']} "
                        f"(라이브 루프 지연에 따라 [T,T+5)/[T-5,T) 가 섞인다 — 지문 정렬로 흡수).")
    else:
        out["notes"] = (f"불일치: unaligned={unaligned}, recon={recon} vs live={len(live_cands)}, "
                        f"score_max={score_max}, comp_max={comp_max}, invariant_ok={invariant_ok}, "
                        f"component_comparisons={comp_comparisons}"
                        + (" (컴포넌트 비교 0건 — 게이트가 공허하다)" if comp_comparisons == 0 else ""))
    return out


# ---------------------------------------------------------------------------
# 2) 후보 스트림
# ---------------------------------------------------------------------------

def _session_mix(obs) -> dict:
    """관측의 세션 구성 — 정규장/시간외가 섞인 비율을 드러낸다."""
    mix: dict = defaultdict(int)
    for o in obs:
        mix[o.session] += 1
    return dict(sorted(mix.items()))


def _missing_minutes(candles) -> int:
    """각 거래일의 [첫 봉, 마지막 봉] 구간에서 비어 있는 1분 슬롯 수(상한 추정)."""
    by_day: dict = defaultdict(list)
    for c in candles:
        by_day[c.ts_exchange.date()].append(c.ts_utc)
    total = 0
    for _, ts in by_day.items():
        ts.sort()
        span = int((ts[-1] - ts[0]).total_seconds() // 60) + 1
        total += max(0, span - len(ts))
    return total


def collect_observations(db: str, symbols: list[str], horizons,
                         min_days: int, *, exclude_dates: tuple = (),
                         sessions: tuple | None = None) -> tuple[list, dict]:
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
        if exclude_dates:
            obs = [o for o in obs if o.et_date not in exclude_dates]
        if sessions:
            obs = [o for o in obs if o.session in sessions]
        days = sorted({c.ts_exchange.date().isoformat() for c in candles})
        cand_days = sorted({o.et_date for o in obs})
        entry = {
            "symbol": sym, "tier": TIERS.get(sym, "?"),
            "leverage_group": leverage_group(sym),
            "bars": len(candles), "trading_days": len(days),
            "start": candles[0].ts_utc.isoformat(), "end": candles[-1].ts_utc.isoformat(),
            # 거래일 안에서 관측된 1분 격자 대비 결측(휴장/비거래 시간은 제외할 수 없어
            # 상한 추정치다). 0 을 하드코딩하면 "결측이 검증됐다"는 잘못된 신호를 준다.
            "missing_bars_within_days": _missing_minutes(candles),
            "candidates": len(obs),
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
    # 세션별 층화 — 정규장/시간외는 유동성이 전혀 달라 한 상관에 섞으면 오염된다.
    by_session = grouped(lambda o: o.session, "session")

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
        "by_leverage": by_leverage, "by_date": by_date, "by_session": by_session,
        "sign_stability": sign_stability,
        "multiple_comparison": {"tests_run": tests, "alpha": alpha,
                                "bonferroni_threshold": thr, "survivors": survivors},
    }


def official_validation(obs, horizons, embargo_min: int, ratio: float = 0.7) -> list:
    """chronological / embargo 홀드아웃. random CV 는 구조적으로 불가능하다."""
    rows = []
    ordered = sorted(obs, key=lambda o: datetime.fromisoformat(o.bar_ts_utc))
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

def daily_reproduction(h2b: dict, idx: dict, horizons) -> dict:
    """일별 재현율 — **그 날 하루 안에서** pooled 와 같은 부호가 나온 날의 비율.

    LOSO 부호 일치율을 확신도 근거로 쓰면 안 되는 이유: fold 가 "한 날을 뺀 나머지 전부"라
    D일이면 각 fold 가 (D-1)/D 를 공유한다. 105일이면 하루를 빼도 rho 는 소수 3자리에서나
    움직여 일치율이 거의 항상 1.0 이 된다(모든 날이 반대 부호여도 1.0 이 가능하다).
    그건 **한 날의 영향력(jackknife)** 이지 재현성이 아니다.

    이미 계산해 둔 `by_date` 를 쓰면 "날마다 같은 방향이 나오는가"를 직접 볼 수 있다.
    무작위라면 0.5 근처, 진짜 재현되는 효과라면 유의하게 높아야 한다.
    """
    out: dict = {}
    for f in H2B_FEATURES:
        for h in horizons:
            pooled = idx.get((f, h))
            if not pooled or pooled["rho"] is None:
                continue
            pooled_neg = pooled["rho"] < 0
            days = [r for r in h2b["by_date"]
                    if r["feature"] == f and r["horizon_min"] == h and r["rho"] is not None]
            if not days:
                continue
            same = sum(1 for r in days if (r["rho"] < 0) == pooled_neg)
            out[f"{f}@{h}"] = {"feature": f, "horizon_min": h, "days": len(days),
                               "same_sign_days": same, "ratio": same / len(days),
                               "pooled_sign": "-" if pooled_neg else "+"}
    return out


def verdict(h2b: dict, coverage: dict, parity: dict, horizons,
            min_days: int, min_symbols: int) -> dict:
    limits = []
    # 판정에 필요한 건 "캔들이 있는 날"이 아니라 **후보가 실제 발생한 날**이다.
    # 또 n_days 를 max 로 잡으면 한 종목만 길어도 게이트를 통과하므로 중앙값을 쓴다.
    usable = [s for s in coverage["per_symbol"] if s["candidate_days"] >= min_days]
    cand_days = sorted(s["candidate_days"] for s in coverage["per_symbol"])
    n_days = cand_days[len(cand_days) // 2] if cand_days else 0     # 중앙값
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

    # 각 검정을 s001 기준선과 **방향 비교**해 분류한다.
    #   replicated : 같은 부호 + Bonferroni 통과 + 실용적 크기(|rho|>=MIN_PRACTICAL_RHO)
    #   negligible : 같은 부호지만 크기가 사실상 0 (n 이 커서 유의하기만 함)
    #   reversed   : 반대 부호 + Bonferroni 통과 → 재현 실패를 넘어 **적극적 반증**
    #   null       : 유의하지 않음
    checks = []
    classes = {}
    for f in H2B_FEATURES:
        for h in horizons:
            r = idx.get((f, h))
            base = S001_BASELINE.get((f, h))
            if not r or r["rho"] is None:
                classes[(f, h)] = "insufficient"
                checks.append((f, h, False, "표본 부족"))
                continue
            if base is None:
                # 기준선 없는 지평(--horizons 로 30/60 외를 준 경우) — 방향 비교 불가.
                classes[(f, h)] = "no_baseline"
                checks.append((f, h, False,
                               f"rho={rho_fmt(r['rho'])} — s001 기준선 없음(판정 제외)"))
                continue
            rho, p = r["rho"], r["p"]
            sig = p is not None and p < thr
            same_sign = (rho < 0) == (base < 0)
            big = abs(rho) >= MIN_PRACTICAL_RHO
            # ★ 크기 하한은 **부호와 무관하게** 적용한다. reversed 에만 빼면 |rho| 가
            #   똑같은 0.023 이 부호에 따라 "적극적 반증"과 "사실상 0"으로 갈린다 —
            #   MIN_PRACTICAL_RHO 를 정의한 취지(유의성≠효과크기)와 정면으로 어긋난다.
            if not big:
                cls = "negligible"          # 방향 불문, 크기가 사실상 0
            elif sig and not same_sign:
                cls = "reversed"            # 크고 + 유의하고 + 방향이 반대 → 적극적 반증
            elif sig and same_sign:
                cls = "replicated"
            else:
                cls = "null"
            classes[(f, h)] = cls
            ratio = (abs(rho) / abs(base)) if base else float("nan")
            checks.append((f, h, cls == "replicated",
                           f"rho={rho:+.3f} (s001 {base:+.3f}, 크기 {ratio:.0%}) "
                           f"p={p:.3g} → {cls}"))
    n_pass = sum(1 for c in checks if c[2])
    n_reversed = sum(1 for v in classes.values() if v == "reversed")
    n_negligible = sum(1 for v in classes.values() if v == "negligible")
    judged = [k for k, v in classes.items() if v not in ("no_baseline", "insufficient")]

    # 레버리지 의존성 — 그룹별 부호가 갈리면 "효과가 레버리지 때문"이라는 신호.
    lev_rows = [r for r in h2b["by_leverage"]
                if r["feature"] in H2B_FEATURES and r["rho"] is not None]
    groups_present = {r["group"] for r in lev_rows}
    if len(groups_present) < 2:
        lev_dep = "inconclusive"
        limits.append(f"레버리지 그룹이 {sorted(groups_present)} 뿐 — 자연실험 비교 불가")
    else:
        # ★ 같은 (feature, 지평) 안에서 그룹 간 부호가 갈리는지를 본다.
        #   group 만으로 키를 잡으면 한 그룹 안의 서로 다른 feature 부호차(vwap +, volume −)
        #   가 곧바로 "불일치"로 잡혀 레버리지와 무관하게 항상 yes 가 된다.
        by_cell = defaultdict(dict)
        for r in lev_rows:
            by_cell[(r["feature"], r["horizon_min"])][r["group"]] = r["rho"] < 0
        split_cells = [cell for cell, gm in by_cell.items()
                       if len(gm) >= 2 and len(set(gm.values())) > 1]
        lev_dep = "yes" if split_cells else "no"
        if split_cells:
            limits.append(
                "레버리지 그룹 간 부호가 갈리는 (feature, 지평): "
                + ", ".join(f"{f}@{h}m" for f, h in sorted(split_cells)))

    # LOSO 부호 일치율은 **jackknife 영향력**이지 "날마다 재현되는가"가 아니다.
    # fold 가 (D-1)/D 데이터를 공유해 하루 빼도 rho 가 거의 안 움직이기 때문이다
    # (모든 날이 반대 부호여도 일치율 1.0 이 나올 수 있다).
    # 그래서 확신도는 **일별 재현율**(그 날 안에서 pooled 부호를 보인 날의 비율)로 잰다.
    ratios = [stab[(f, h)]["same_sign_ratio"] for f in H2B_FEATURES for h in horizons
              if (f, h) in stab]
    min_stab = min(ratios) if ratios else 0.0
    day_repro = daily_reproduction(h2b, idx, horizons)
    min_day = min([v["ratio"] for v in day_repro.values()], default=0.0)

    # ★ 중첩 표본 경고 — p값은 iid 를 가정하지만 관측은 심하게 겹친다.
    #   결정봉 5분 간격 × forward 30/60분 → 한 관측의 창이 다음 6~12개 관측과 겹치고,
    #   분석 종목 상당수가 같은 반도체 베타라 동시점 수익률도 교차상관돼 있다.
    #   유효표본은 n 보다 수십~수백 배 작아 p<1e-8 은 그 산술의 산물이다.
    limits.append(
        "p값은 독립 표본(iid)을 가정하지만 관측이 심하게 중첩된다 — 결정봉 5분 간격에 "
        "forward 지평 30/60분이라 한 관측의 창이 다음 6~12개와 겹치고, 종목 간에도 "
        "동시점 수익률이 교차상관돼 있다. **유효표본은 n 보다 훨씬 작고 p값은 과대추정** "
        "이다. 따라서 판정은 p 가 아니라 부호·효과크기·일별 재현율에 근거한다. "
        "정식 보정(일별 블록 부트스트랩/Newey-West)은 후속 과제.")

    if n_syms < min_symbols or n_days < min_days:
        rep, conf = "inconclusive", "none"
        limits.append(
            f"커버리지 부족: 후보발생일 {min_days}일 이상인 종목 {n_syms}개(요구 {min_symbols}), "
            f"후보발생일 중앙값 {n_days}일(요구 {min_days}). 일반화를 판정할 수 없다.")
    elif not judged:
        # 판정 가능한 검정이 하나도 없다(기준선 없는 지평만 주어진 경우 등).
        # 근거 없이 "재현 안 됨"이라고 말하지 않는다.
        rep, conf = "inconclusive", "none"
        limits.append(
            "판정 가능한 검정이 0건 — s001 기준선이 있는 지평(30/60m)이 필요하다. "
            "상관 자체는 계산·보고되지만 재현 여부는 판정하지 않는다.")
    elif n_pass == len(judged):
        rep = "yes"
        # 중첩 표본으로 p 가 과대추정이므로 confidence 를 strong 으로 올리지 않는다.
        conf = "moderate" if min_day >= 0.6 else "weak"
    elif n_reversed > 0:
        # 부호가 뒤집혔는데 유의하기까지 하면 "판단 보류"가 아니라 **반증**이다.
        # 이걸 inconclusive 라고 부르면 결론을 실제보다 열어두는 것이 된다.
        rep = "no"
        conf = "moderate"
        limits.append(
            f"부호 역전 {n_reversed}건: "
            + ", ".join(f"{f}@{h}m(s001 {S001_BASELINE[(f,h)]:+.3f} → 현재 "
                        f"{idx[(f,h)]['rho']:+.3f})"
                        for (f, h), c in classes.items() if c == "reversed")
            + " — s001 의 음(−)의 예측력은 재현되지 않았고, 방향이 반대로 관측된다.")
    elif n_pass == 0:
        rep = "no"
        conf = "moderate"
    else:
        rep, conf = "inconclusive", "weak"
        limits.append(f"H2-b {len(judged)}개 검정 중 {n_pass}개만 재현 — 부분 재현")

    if n_negligible:
        n_max = max((idx[(f, h)]["n"] for f in H2B_FEATURES for h in horizons
                     if (f, h) in idx), default=0)
        limits.append(
            f"부호는 유지되나 크기가 실용적 하한(|rho|<{MIN_PRACTICAL_RHO}) 미만인 검정 "
            f"{n_negligible}건 — n≈{n_max:,} 에서는 rho=0.02 도 p<1e-8 이 된다. "
            "**통계적 유의성을 효과 크기로 읽지 말 것.**")

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

    checks_out = []
    for f, h, _ok, _msg in checks:
        r = idx.get((f, h))
        base = S001_BASELINE.get((f, h))
        if not r or r["rho"] is None or base is None:
            continue
        checks_out.append({
            "feature": f, "horizon_min": h, "rho": r["rho"], "p": r["p"],
            "s001_rho": base, "class": classes.get((f, h)),
            "size_ratio": f"{abs(r['rho'])/abs(base):.0%}" if base else "—"})

    detail = "; ".join(f"{f}@{h}m {msg}" for f, h, _, msg in checks)
    summary = (f"패리티 PASS 하에 H2-b 판정 = {rep}. "
               f"vwap_position/volume {len(judged)}개 검정 중 {n_pass}개가 재현(replicated). "
               f"[{detail}]")
    return {"h2b_replicated": rep, "confidence": conf, "leverage_dependent": lev_dep,
            "summary": summary, "limitations": limits,
            "checks": checks_out, "daily_reproduction": day_repro,
            "classes": {f"{f}@{h}": c for (f, h), c in classes.items()}}


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
    A(f"| Bonferroni 통과 feature | {', '.join(h2b['multiple_comparison']['survivors']) or '없음'} "
      f"<br>*(통과 = 유의함일 뿐, 방향·크기는 아래 참조)* |")
    A("")
    A(ver["summary"])
    A("")
    # 판정 한 줄 해석 — 표만 보고 반대로 읽는 것을 막는다.
    if ver["h2b_replicated"] == "no" and ver.get("checks"):
        # 실제 실행 결과로 생성한다(하드코딩 금지 — 다른 데이터로 돌리면 거짓말이 된다).
        A("> **읽는 법:** " + " / ".join(
            f"`{c['feature']}`@{c['horizon_min']}m: s001 {c['s001_rho']:+.3f} → "
            f"현재 {c['rho']:+.3f} (크기 {c['size_ratio']}) → **{c['class']}**"
            for c in ver["checks"]))
        A(">")
        A(f"> n 이 크면 작은 rho 도 유의해지므로 ★ 를 효과 크기로 읽으면 안 된다. "
          f"실용적 하한은 |rho| ≥ {MIN_PRACTICAL_RHO}.")
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
    A(f"- 세션 구성(관측 수): `{meta.get('session_mix')}` · 필터: `{meta.get('session_filter')}`")
    A(f"- 발견 표본 제외일: `{meta.get('discovery_dates_excluded') or '없음(포함됨 — 순환 주의)'}`"
      " — s001 과 같은 날을 재현 표본에 넣으면 발견/재현이 겹친다.")
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
    A("> ⚠ **n 이 7만 규모라 통계적 유의성(★)은 효과 크기를 뜻하지 않는다.** rho=0.02 도 "
      f"p<1e-8 이 된다(설명력 0.04%). 재현 여부는 p 가 아니라 **s001 대비 부호와 크기**로 읽어야 "
      f"한다. 실용적 하한은 |rho| ≥ {MIN_PRACTICAL_RHO}.")
    A("")
    head = ("| feature | s001 30m | 30m rho | 30m p | n | s001 60m | 60m rho | 60m p | n |")
    A(head)
    A("|---" * 9 + "|")
    idx = {(r["feature"], r["horizon_min"]): r for r in h2b["overall"]}
    for f in FEATURES:
        cells = []
        for h in horizons:
            r = idx.get((f, h), {})
            base = S001_BASELINE.get((f, h))
            cells += [f"{base:+.3f}" if base is not None else "—",
                      f"{_fmt(r.get('rho'))}{_sig(r.get('p'), thr)}",
                      _fmt(r.get("p"), 4), f"{r.get('n', 0):,}"]
        mark = "**" if f in H2B_FEATURES else ""
        A(f"| {mark}{f}{mark} | " + " | ".join(cells) + " |")
    A("")
    A("s001 열은 선행 관측(하루·후보 156/140건)이다. 부호가 다르면 **재현 실패를 넘어 반증**이다.")
    A("")

    for title, key, col in (("Tier별", "by_tier", "tier"),
                            ("레버리지 그룹별", "by_leverage", "group"),
                            ("세션별", "by_session", "session"),
                            ("종목별", "by_symbol", "symbol")):
        A(f"### {title} (H2-b feature 만)")
        A("")
        A(f"> 이 표는 **탐색적(미보정)** 이다. 전체 표의 ★ 임계는 14검정 기준이라 "
          f"하위그룹(종목 {len(cov['per_symbol'])}×14, 날짜별 등 수천 검정)에는 맞지 않는다. "
          f"여기서는 ★ 를 붙이지 않는다 — 우연히 유의해 보이는 칸을 보정된 결과로 "
          f"읽지 않기 위해서다.")
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
                cells += [_fmt(r.get("rho")), _fmt(r.get("p"), 4), f"{r.get('n', 0):,}"]
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
        A("## ⑤-b 구간 분할 안정성 (chronological / embargo)")
        A("")
        A("train 에서 모형을 적합하지 않고 두 구간의 rho 를 각각 계산해 비교한다 — "
          "홀드아웃 **검증**이 아니라 **구간 분할 안정성 점검**이다. "
          "random CV 는 시계열 누수라 구조적으로 금지되어 있다"
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
    A(f"- **Bonferroni 통과(유의성만 — 재현 판정이 아니다):** "
      f"{', '.join(mc['survivors']) or '없음'}")
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
    ap.add_argument("--sessions", default="",
                    help="쉼표 구분 세션 필터(예: REGULAR). 비우면 전 세션 — "
                         "s001 과 동일 조건이나 유동성이 다른 구간이 섞인다(리포트에 층화 표기)")
    ap.add_argument("--include-discovery-date", action="store_true",
                    help=f"발견 표본 {DISCOVERY_DATES} 를 재현 표본에 포함(기본은 제외 — "
                         "포함하면 발견과 재현이 겹쳐 순환 검증이 된다)")
    ap.add_argument("--parity-journal", default="data/journal_us_phase6_s001.sqlite")
    ap.add_argument("--parity-market", default="data/market_us_phase6_s001.sqlite")
    ap.add_argument("--out-md", default="reports/h2b_backfill_validation.md")
    ap.add_argument("--out-json", default="reports/h2b_backfill_validation.json")
    ap.add_argument("--out-jsonl", default=None)
    args = ap.parse_args(argv)

    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    if not horizons:
        print("--horizons 가 비었다.", file=sys.stderr)
        return 2
    # S001_BASELINE 이 없는 지평은 방향 비교(재현 판정)를 할 수 없다. 조용히 reversed 로
    # 분류되거나 리포트 포맷에서 죽지 않도록, 판정 대상 feature 기준으로 미리 경고한다.
    missing_base = [h for h in horizons
                    if any((f, h) not in S001_BASELINE for f in H2B_FEATURES)]
    if missing_base:
        print(f"경고: 지평 {missing_base} 는 s001 기준선이 없어 재현 판정에서 제외된다"
              f"(상관 자체는 계산·보고된다).", file=sys.stderr)
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
    excluded = () if args.include_discovery_date else DISCOVERY_DATES
    sessions = tuple(x.strip().upper() for x in args.sessions.split(",") if x.strip()) or None
    obs, coverage = collect_observations(args.db, symbols, horizons, args.min_days,
                                         exclude_dates=excluded, sessions=sessions)
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
        # 발견/재현 표본 분리와 세션 구성을 명시 — 나중에 "겹쳤나?"를 되물을 필요가 없게.
        "discovery_dates_excluded": list(excluded),
        "session_filter": list(sessions) if sessions else "all",
        "session_mix": _session_mix(obs),
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
        # allow_nan=False: Infinity/NaN 은 RFC 8259 위반이라 jq/엄격 파서에서 리포트
        # 전체가 깨진다. 조용히 쓰지 말고 조기에 실패시킨다.
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str, allow_nan=False)
    with open(args.out_md, "w", encoding="utf-8") as fh:
        fh.write(render_md(payload, cmd))

    print(f"\nMD   : {args.out_md}\nJSON : {args.out_json}\nJSONL: {out_jsonl} ({len(obs):,}건)")
    print(f"판정 : H2-b={ver['h2b_replicated']} (confidence={ver['confidence']}, "
          f"parity={parity['status']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
