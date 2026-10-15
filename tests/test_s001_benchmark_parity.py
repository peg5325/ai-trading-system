"""★ 수용 기준 — us-phase6-s001 라이브 `benchmark_context` 재현 패리티 (H3).

왜 이 테스트가 먼저인가: H3 는 "국면이 수익을 예측하는가"를 묻는다. 그런데 분석에 쓰는
국면이 라이브가 실제로 본 국면과 다르면, 상관이 나오든 안 나오든 **무엇에 대한 결론인지
알 수 없다.** 그래서 국면 계산은 `src.market_context.builder` 를 재사용하고, 그 재사용이
라이브를 정말 재현하는지를 여기서 고정한다.

기준(reports/research/h2-h4/WORKORDER_H3_REGIME.md §2-1): 라이브 저널의 benchmark_context 를
오차 ≤ 1e-6 으로 재현.

--------------------------------------------------------------------------------
정렬 규칙 — 왜 `benchmark_source_bar_end` 로 맞추나 (순환 아님)
--------------------------------------------------------------------------------
라이브는 `build_benchmark_context(candles_5m[QQQ], decision_time=newest, ...)` 를 부른다.
여기서 `newest` 는 **유니버스 전체**의 최신 5m 봉 라벨이다. QQQ 데이터 도착이 한 봉 늦으면
라이브가 실제로 마지막으로 본 QQQ 봉은 `newest` 가 아니라 그 직전 봉이 된다
(s001 92 사이클 중 3 건. `candidate_stream` 이 기록한 루프 지연 현상과 같은 뿌리다).

재구성은 세션 DB 전체를 읽으므로 그 늦게 도착한 봉까지 갖고 있다. 그래서 정렬 기준을
`context_asof`(=`newest`) 로 두면 그 3 건은 라이브보다 **한 봉 더 아는 상태**로 비교하게 된다.
이는 계산 불일치가 아니라 데이터 도착 시점 차이다.

따라서 "라이브가 어느 봉을 봤는가"(= `source_bar_end`, 입력 선택 사실 1개)만 라이브에서
받고, 그 봉에서 **파생되는 값 전부**(close·ema·vs_ema·slope·trailing 30m/60m·trend·
timeframe)는 우리가 독립적으로 재계산해 비교한다. 리샘플·EMA·추세 판정 중 하나라도
어긋나면 이 테스트는 깨진다.

두 정렬을 **모두** 검증한다:
  - `test_bar_aligned_parity_is_exact`      — 라이브가 본 봉 기준 전건 일치(수용 기준)
  - `test_asof_aligned_parity_matches_except_data_arrival_lag`
        — as-of 정렬로도 대부분 일치하고, 불일치는 오직 '벤치마크 봉이 한 봉 늦은' 경우뿐임을 고정

데이터 의존: 운영 산출물(gitignore) 세션 DB 2 개를 읽는다. 없으면 skip 하되,
`H3_REQUIRE_PARITY=1` 이면 skip 대신 fail — 아티팩트가 있는 환경에서 회귀를 놓치지 않는다.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.history.candidate_stream import build_decision_bars, load_1m_candles
from src.market_context.builder import build_benchmark_context

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL_DB = os.path.join(_ROOT, "data", "journal_us_phase6_s001.sqlite")
MARKET_DB = os.path.join(_ROOT, "data", "market_us_phase6_s001.sqlite")
SESSION = "us-phase6-s001"
TOLERANCE = 1e-6

# builder 가 계산해 내는 값 — 전부 독립 재계산해 비교한다.
NUMERIC_FIELDS = ("benchmark_close", "benchmark_ema20", "benchmark_vs_ema20",
                  "benchmark_ema20_slope", "benchmark_return_trailing_30m",
                  "benchmark_return_trailing_60m")
EXACT_FIELDS = ("benchmark_symbol", "benchmark_trend", "benchmark_timeframe",
                "benchmark_ema_period", "benchmark_source_bar_end",
                "market_context_version")

_missing = [p for p in (JOURNAL_DB, MARKET_DB) if not os.path.exists(p)]
if _missing and os.environ.get("H3_REQUIRE_PARITY") == "1":
    raise AssertionError(f"H3_REQUIRE_PARITY=1 인데 패리티 아티팩트가 없다: {_missing}")

pytestmark = pytest.mark.skipif(
    bool(_missing),
    reason=(f"{SESSION} 아티팩트 없음(gitignore 운영 산출물): {_missing}. "
            "CI 에서 강제하려면 H3_REQUIRE_PARITY=1"))


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def live_contexts() -> list[dict]:
    """저널의 사이클별 benchmark_context."""
    con = sqlite3.connect(f"file:{JOURNAL_DB}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT cycle_ts, data FROM journal_cycle ORDER BY cycle_ts").fetchall()
    finally:
        con.close()
    out = []
    for cycle_ts, data in rows:
        ctx = json.loads(data).get("benchmark_context")
        if ctx:
            out.append({"cycle_ts": cycle_ts, "ctx": ctx})
    return out


@pytest.fixture(scope="module")
def benchmark_bars(live_contexts):
    """세션이 실제로 본 1m 캔들 → live 와 동일 규칙의 5m 결정봉."""
    symbols = {r["ctx"]["benchmark_symbol"] for r in live_contexts}
    assert len(symbols) == 1, f"벤치마크가 여럿이다: {symbols}"
    sym = symbols.pop()
    return sym, build_decision_bars(load_1m_candles(MARKET_DB, sym, table="candle"), sym)


def _rebuild(bars, sym: str, live: dict, at: str):
    """라이브가 준 decision_time 기준으로 국면을 재계산한다."""
    return build_benchmark_context(
        bars, decision_time=_parse(live[at]), symbol=sym,
        timeframe="5m", ema_period=live["benchmark_ema_period"])


def _diff(rebuilt, live: dict) -> list[str]:
    """재계산 ↔ 라이브 불일치 필드 목록."""
    got = rebuilt.to_dict()
    bad = []
    for f in NUMERIC_FIELDS:
        a, b = got[f], live[f]
        if a is None or b is None:
            if a is not b:
                bad.append(f"{f}: {a!r} vs {b!r} (결측 불일치)")
            continue
        if abs(a - b) > TOLERANCE:
            bad.append(f"{f}: {a!r} vs {b!r} (차={abs(a - b):.3e})")
    for f in EXACT_FIELDS:
        if got[f] != live[f]:
            bad.append(f"{f}: {got[f]!r} vs {live[f]!r}")
    return bad


def test_live_session_has_benchmark_contexts(live_contexts):
    """전제 — 저널에 국면 관측이 실제로 남아 있어야 한다."""
    assert live_contexts, "저널에 benchmark_context 가 없다 — market_context 가 꺼진 세션인지 확인"


def test_bar_aligned_parity_is_exact(live_contexts, benchmark_bars):
    """★ 수용 기준 — 라이브가 본 봉에서 재계산한 국면이 전건 오차 ≤ 1e-6."""
    sym, bars = benchmark_bars
    failures = []
    for r in live_contexts:
        rebuilt = _rebuild(bars, sym, r["ctx"], "benchmark_source_bar_end")
        if rebuilt is None:
            failures.append(f"{r['cycle_ts']}: 재구성 None")
            continue
        bad = _diff(rebuilt, r["ctx"])
        if bad:
            failures.append(f"{r['cycle_ts']}: " + "; ".join(bad))
    assert not failures, (
        f"{len(failures)}/{len(live_contexts)} 사이클 불일치:\n" + "\n".join(failures[:5]))


def test_asof_aligned_parity_matches_except_data_arrival_lag(live_contexts, benchmark_bars):
    """as-of 정렬 불일치는 **오직** 벤치마크 봉 도착 지연 때문이어야 한다.

    불일치가 그 밖의 이유로 생기면(리샘플·EMA·추세 판정 차이) 이 테스트가 잡는다.
    """
    sym, bars = benchmark_bars
    lagged, unexplained = 0, []
    for r in live_contexts:
        ctx = r["ctx"]
        rebuilt = _rebuild(bars, sym, ctx, "benchmark_context_asof")
        assert rebuilt is not None, f"{r['cycle_ts']}: as-of 재구성 None"
        bad = _diff(rebuilt, ctx)
        if not bad:
            continue
        # 라이브가 본 봉이 as-of 보다 이전이면 = 도착 지연. 그 경우만 허용한다.
        if _parse(ctx["benchmark_source_bar_end"]) < _parse(ctx["benchmark_context_asof"]):
            lagged += 1
        else:
            unexplained.append(f"{r['cycle_ts']}: " + "; ".join(bad))
    assert not unexplained, (
        "도착 지연으로 설명되지 않는 불일치:\n" + "\n".join(unexplained[:5]))
    assert lagged < len(live_contexts), "전 사이클이 지연이면 정렬 규칙 자체를 의심해야 한다"


def test_live_invariant_source_bar_not_after_decision(live_contexts):
    """라이브 자체의 look-ahead 불변식 — source_bar_end <= context_asof."""
    viol = [r["cycle_ts"] for r in live_contexts
            if _parse(r["ctx"]["benchmark_source_bar_end"])
            > _parse(r["ctx"]["benchmark_context_asof"])]
    assert not viol, f"라이브 국면이 미래 봉을 참조했다: {viol[:5]}"
