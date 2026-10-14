"""★ 수용 기준 — us-phase6-s001(2026-07-15) 라이브 재현 패리티 (CI 회귀 고정).

과거 캔들 재구성이 믿을 만한지 검증할 **정답지**가 이미 있다: 라이브 세션의 후보와
컴포넌트 점수가 저널에 남아 있다. 재구성이 그것을 재현하지 못하면, 재구성으로 만든
어떤 상관도 의미가 없다.

기준(reports/research/h2-h4/WORKORDER_H2B_BACKFILL.md §3):
  - 후보 개수      : 재구성 == 라이브
  - candidate_score: 최대 절대오차 ≤ 1e-6
  - 컴포넌트       : normalized_score/weight/weighted_score 최대 절대오차 ≤ 1e-6
  - 불변식         : pre_clamp_score == Σweighted_score - penalty_score

정렬 규칙(중요): 저널 `cycle_ts` 는 봉 라벨이 아니라 **예정된 wall-clock 시각**이다.
루프 지연 때문에 실제 사용된 5m 버킷이 사이클마다 다를 수 있다(s001: 267건 [T,T+5),
9건 [T-5,T)). 그래서 시계가 아니라 **지표 지문**으로 봉을 찾는다. 지문으로 봉을 특정한
뒤 **점수는 독립적으로 재계산해 비교** 하므로, 정렬이 비교를 순환적으로 만들지 않는다
(지문에 쓰는 값과 검증하는 값이 다르다).

데이터 의존: 이 테스트는 운영 산출물(gitignore 대상)인 세션 DB 2개를 읽는다.
없으면 skip 하되, `H2B_REQUIRE_PARITY=1` 이면 skip 대신 fail — 아티팩트가 있는 CI 에서
회귀를 놓치지 않기 위한 스위치다.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.candidate_stream import (
    CandidateStreamExtractor,
    align_live_outcome,
    load_1m_candles,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL_DB = os.path.join(_ROOT, "data", "journal_us_phase6_s001.sqlite")
MARKET_DB = os.path.join(_ROOT, "data", "market_us_phase6_s001.sqlite")
SESSION = "us-phase6-s001"
TOLERANCE = 1e-6

_missing = [p for p in (JOURNAL_DB, MARKET_DB) if not os.path.exists(p)]
if _missing and os.environ.get("H2B_REQUIRE_PARITY") == "1":
    raise AssertionError(
        f"H2B_REQUIRE_PARITY=1 인데 패리티 아티팩트가 없다: {_missing}")

pytestmark = pytest.mark.skipif(
    bool(_missing),
    reason=(f"{SESSION} 아티팩트 없음(gitignore 운영 산출물): {_missing}. "
            "CI 에서 강제하려면 H2B_REQUIRE_PARITY=1"))


def _live_outcomes() -> list[dict]:
    """저널에서 (cycle_ts, symbol, indicator_snapshot, candidate) 를 모두 뽑는다."""
    con = sqlite3.connect(f"file:{JOURNAL_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT cycle_ts, data FROM journal_cycle ORDER BY cycle_ts").fetchall()
    finally:
        con.close()
    out = []
    for cycle_ts, data in rows:
        d = json.loads(data)
        for o in d.get("outcomes") or []:
            out.append({"cycle_ts": cycle_ts, "symbol": o["symbol"],
                        "snapshot": o.get("indicator_snapshot"),
                        "candidate": o.get("candidate")})
    return out


@pytest.fixture(scope="module")
def reconstruction():
    """세션이 실제로 본 1m 캔들로 5m 결정봉 + 지표를 재구성한다."""
    ex = CandidateStreamExtractor(horizons_min=(30, 60))
    per_symbol = {}
    for sym in ("NVDA", "QQQ", "SOXL"):
        candles = load_1m_candles(MARKET_DB, sym, table="candle")
        bars, ind = ex.prepare(candles, sym)
        per_symbol[sym] = (bars, ind)
    return ex, per_symbol


@pytest.fixture(scope="module")
def matched(reconstruction):
    """라이브 outcome ↔ 재구성 봉 정렬 결과."""
    ex, per_symbol = reconstruction
    live = _live_outcomes()
    rows = []
    for lo in live:
        if not lo["snapshot"]:
            continue
        bars, ind = per_symbol[lo["symbol"]]
        i = align_live_outcome(ind, lo["snapshot"])
        rows.append({**lo, "bar_index": i})
    return ex, per_symbol, rows


def test_every_live_outcome_aligns_to_a_reconstructed_bar(matched):
    """모든 라이브 관측이 재구성 봉 중 하나와 지문 일치해야 한다(미정렬 0)."""
    _, _, rows = matched
    unaligned = [r for r in rows if r["bar_index"] is None]
    assert rows, "라이브 outcome 이 없다 — 저널이 비었는지 확인"
    assert not unaligned, (
        f"{len(unaligned)}/{len(rows)} 건이 어떤 봉과도 지문 일치하지 않는다: "
        f"{[(r['cycle_ts'], r['symbol']) for r in unaligned[:5]]}")


def test_candidate_count_matches_live(matched):
    """재구성 후보 개수 == 라이브 후보 개수."""
    ex, per_symbol, rows = matched
    live_with_candidate = [r for r in rows if r["candidate"]]
    recon = 0
    for r in live_with_candidate:
        bars, ind = per_symbol[r["symbol"]]
        lev = (r["candidate"].get("features") or {}).get("leverage_factor") or 1
        cand = ex.screen_at(r["symbol"], ind, r["bar_index"], lev)
        if cand is not None:
            recon += 1
    assert recon == len(live_with_candidate), (
        f"후보 개수 불일치: 재구성 {recon} vs 라이브 {len(live_with_candidate)}")


def test_scores_and_components_match_within_tolerance(matched):
    """candidate_score 와 컴포넌트 전 필드가 1e-6 이내로 일치."""
    ex, per_symbol, rows = matched
    score_max = 0.0
    comp_max = 0.0
    compared = 0
    worst = None
    for r in rows:
        cand_live = r["candidate"]
        if not cand_live:
            continue
        bars, ind = per_symbol[r["symbol"]]
        feats_live = cand_live.get("features") or {}
        lev = feats_live.get("leverage_factor") or 1
        cand = ex.screen_at(r["symbol"], ind, r["bar_index"], lev)
        assert cand is not None, f"{r['cycle_ts']} {r['symbol']}: 재구성 후보 없음"
        compared += 1
        d = abs(float(cand.score) - float(cand_live["score"]))
        if d > score_max:
            score_max, worst = d, (r["cycle_ts"], r["symbol"],
                                   float(cand.score), float(cand_live["score"]))
        live_comps = feats_live.get("score_components") or {}
        recon_comps = (cand.features or {}).get("score_components") or {}
        assert set(live_comps) == set(recon_comps), (
            f"{r['symbol']}: 컴포넌트 키 불일치 {set(live_comps)} vs {set(recon_comps)}")
        for name, lc in live_comps.items():
            rc = recon_comps[name]
            for fld in ("normalized_score", "weight", "weighted_score"):
                comp_max = max(comp_max, abs(float(rc[fld]) - float(lc[fld])))
    assert compared > 0, "비교된 후보가 0건"
    assert score_max <= TOLERANCE, f"candidate_score 최대오차 {score_max} > {TOLERANCE} (worst={worst})"
    assert comp_max <= TOLERANCE, f"컴포넌트 최대오차 {comp_max} > {TOLERANCE}"


def test_score_invariant_holds_on_reconstruction(matched):
    """불변식: pre_clamp_score == Σweighted_score - penalty_score.

    (작업지시서 표기는 'Σweighted + penalty' 이나, 실제 스크리너는 penalty 를 **뺀다**
     — screener.py `base - penalties`. 여기서는 실제 산술을 고정한다.)
    """
    ex, per_symbol, rows = matched
    checked = 0
    for r in rows:
        if not r["candidate"]:
            continue
        bars, ind = per_symbol[r["symbol"]]
        lev = (r["candidate"].get("features") or {}).get("leverage_factor") or 1
        cand = ex.screen_at(r["symbol"], ind, r["bar_index"], lev)
        f = cand.features or {}
        comps = f.get("score_components") or {}
        total = sum(c["weighted_score"] for c in comps.values())
        assert abs(total - f["weighted_component_sum"]) <= 1e-9
        assert abs((total - f["penalty_score"]) - f["pre_clamp_score"]) <= 1e-9
        assert abs(round(max(0.0, min(100.0, f["pre_clamp_score"])), 1)
                   - float(cand.score)) <= 1e-9, "clamp+round 규칙"
        checked += 1
    assert checked > 0


def test_live_loop_lag_is_reported_not_hidden(matched):
    """진단: 사이클 예정시각 대비 실제 사용 봉의 오프셋 분포를 드러낸다.

    s001 에서는 [T,T+5) 가 다수, [T-5,T) 가 소수였다. 이 분포가 바뀌면 재구성 규칙
    가정이 흔들린 것이므로 조용히 넘어가지 않고 실패시킨다.
    """
    from datetime import datetime

    _, per_symbol, rows = matched
    offsets: dict[int, int] = {}
    for r in rows:
        if r["bar_index"] is None:
            continue
        _, ind = per_symbol[r["symbol"]]
        bar_ts = ind.index[r["bar_index"]].to_pydatetime()
        cts = datetime.fromisoformat(r["cycle_ts"].replace("Z", "+00:00"))
        off = int((bar_ts - cts).total_seconds() // 60)
        offsets[off] = offsets.get(off, 0) + 1
    assert offsets, "오프셋을 계산할 관측이 없다"
    # 라이브 루프는 마지막 완결 5m 버킷을 쓴다 → 오프셋은 -5 또는 0 만 가능.
    assert set(offsets) <= {-5, 0}, f"예상 밖 오프셋 분포: {offsets}"
