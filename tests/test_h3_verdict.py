"""H3 판정 로직 검증 — 리뷰에서 지적된 결함을 회귀로 고정한다.

여기서 막는 것:
- 지평별 결론이 두 게이트를 **서로 다른 칸**에서 만족해도 `signal` 로 나오던 문제.
  칸 X 가 비용만 통과하고 칸 Y 가 블록검정만 통과하면, 두 게이트를 동시에 만족하는
  칸이 0개인데도 "신호 있음"이 됐다. 이 표는 "국면 게이트를 켤 가치가 있는가"를
  사람이 실제로 훑는 곳이라, 가짜 `signal` 은 잘못된 행동으로 이어진다.
- 하루 블록 검정의 **적정성 판정과 실제 검정 n 이 어긋나던** 문제.
- 헤드라인 판정이 네 조건 AND 인지(하나라도 빠지면 usable 아님).
- 유의성 축이 관측 단위 p 가 아니라 **블록 p** 인지 — 이게 뒤집히면 의사복제가
  다시 판정을 지배한다(실측: 관측 p 1.57e-45 ↔ 블록 p 0.60).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.h3_regime_analysis import (
    COST_PER_TRADE_PCT,
    MIN_BLOCK_DAYS,
    build_verdict,
    day_block_test,
)


def _cell(horizon="1d", *, diff, cost_pass, block_pass, split=True,
          regime_def="x", block_status="ok", obs_p=1e-30):
    """판정에 필요한 최소 필드만 갖춘 (정의×지평) 칸."""
    return {
        "status": "ok", "regime_def": regime_def, "regime_label": f"L-{regime_def}",
        "horizon": horizon, "diff_pp": diff, "abs_diff_pp": abs(diff),
        "cost_ratio": abs(diff) / COST_PER_TRADE_PCT,
        "cost_floor_pass": cost_pass, "bonferroni_pass": True, "p": obs_p,
        "block_bonferroni_pass": block_pass,
        "day_block": {"status": block_status, "p": 1e-4 if block_pass else 0.4,
                      "mode": "paired", "n_days": 30},
        "split": {"status": "ok", "sign_preserved": split},
    }


def _analysis(cells):
    return {"n_tests": 60, "alpha": 0.05, "bonferroni_threshold": 0.05 / 60,
            "categorical": cells, "continuous": []}


_COV = {"kept": 50000, "regime_present": {"daily_trend": 22500}}


# ---------------------------------------------------------------------------
# ★ 리뷰 지적 결함 — 지평별 signal 이 칸을 가로질러 성립하면 안 된다
# ---------------------------------------------------------------------------

def test_horizon_signal_requires_both_gates_in_the_same_cell():
    """비용만 통과한 칸 + 블록검정만 통과한 칸 → `signal` 이면 안 된다.

    두 게이트를 **동시에** 만족하는 칸이 0개인데 "신호 있음"이 나오면, 리포트를 읽는
    사람은 켤 가치가 있다고 오독한다.
    """
    cells = [
        _cell(diff=0.90, cost_pass=True, block_pass=False, regime_def="a"),
        _cell(diff=0.01, cost_pass=False, block_pass=True, regime_def="b"),
    ]
    vd = build_verdict(_analysis(cells), _COV)
    h = vd["by_horizon"]["1d"]
    assert h["conclusion"] != "signal", (
        "두 게이트를 동시에 만족하는 칸이 0개인데 signal 로 판정했다")
    assert h["conclusion"] == "underpowered", (
        "비용은 넘었으나 블록검정을 통과한 칸이 없으므로 underpowered 여야 한다")


def test_horizon_signal_when_one_cell_passes_both():
    """같은 칸이 두 게이트를 모두 통과하면 그때는 `signal` 이 맞다."""
    cells = [
        _cell(diff=0.90, cost_pass=True, block_pass=True, regime_def="a"),
        _cell(diff=0.01, cost_pass=False, block_pass=False, regime_def="b"),
    ]
    assert build_verdict(_analysis(cells), _COV)["by_horizon"]["1d"]["conclusion"] == "signal"


def test_horizon_no_when_nothing_clears_cost():
    """최대 효과조차 비용 밑이면 표본이 더 있어도 쓸 수 없다 — 확정적 `no`."""
    cells = [_cell(diff=0.02, cost_pass=False, block_pass=True, regime_def="a")]
    assert build_verdict(_analysis(cells), _COV)["by_horizon"]["1d"]["conclusion"] == "no"


def test_horizon_counts_are_reported_per_gate():
    """진단용 카운트는 게이트별로 그대로 보여준다(합쳐서 감추지 않는다)."""
    cells = [
        _cell(diff=0.90, cost_pass=True, block_pass=False, regime_def="a"),
        _cell(diff=0.01, cost_pass=False, block_pass=True, regime_def="b"),
    ]
    h = build_verdict(_analysis(cells), _COV)["by_horizon"]["1d"]
    assert h["cells_over_cost"] == 1 and h["cells_block_significant"] == 1


def test_horizons_without_cells_are_no_data():
    vd = build_verdict(_analysis([_cell(diff=0.9, cost_pass=True, block_pass=True)]), _COV)
    assert vd["by_horizon"]["30"]["conclusion"] == "no_data"


# ---------------------------------------------------------------------------
# 헤드라인 판정 — 네 조건 AND
# ---------------------------------------------------------------------------

def test_usable_requires_all_four_conditions():
    full = _cell(diff=0.90, cost_pass=True, block_pass=True, split=True)
    assert build_verdict(_analysis([full]), _COV)["usable_regime_signal"] == "yes"

    for drop in ("cost_floor_pass", "block_bonferroni_pass"):
        c = dict(full)
        c[drop] = False
        vd = build_verdict(_analysis([c]), _COV)
        assert vd["usable_regime_signal"] != "yes", f"{drop} 없이 usable 로 판정됐다"

    c = dict(full)
    c["split"] = {"status": "ok", "sign_preserved": False}
    assert build_verdict(_analysis([c]), _COV)["usable_regime_signal"] != "yes"

    c = dict(full)
    c["day_block"] = {"status": "underpowered", "p": 1e-4, "mode": "paired", "n_days": 6}
    assert build_verdict(_analysis([c]), _COV)["usable_regime_signal"] != "yes", (
        "블록이 성립할 만큼 날이 없는데 usable 로 판정됐다")


def test_significance_axis_is_block_p_not_observation_p():
    """★ 관측 단위 p 가 아무리 작아도 블록 p 가 못 넘으면 usable 이 아니다.

    실측에서 관측 p 1.57e-45 ↔ 블록 p 0.60 이었다. 축이 뒤집히면 의사복제가
    판정을 지배한다.
    """
    c = _cell(diff=0.90, cost_pass=True, block_pass=False, obs_p=1.57e-45)
    vd = build_verdict(_analysis([c]), _COV)
    assert vd["usable_regime_signal"] == "inconclusive"
    assert vd["criteria"]["significance_axis"].startswith("day_block")


def test_no_verdict_when_every_effect_is_below_cost():
    cells = [_cell(horizon=h, diff=0.01, cost_pass=False, block_pass=False)
             for h in ("30", "60", "eod")]
    vd = build_verdict(_analysis(cells), _COV)
    assert vd["usable_regime_signal"] == "no"


# ---------------------------------------------------------------------------
# 하루 블록 검정 — 적정성 판정과 실제 검정 n 이 일치해야 한다
# ---------------------------------------------------------------------------

class _Obs:
    __slots__ = ("symbol", "et_date", "session", "ts", "regime", "rets")

    def __init__(self, et_date, trend, ret):
        self.symbol = "T"
        self.et_date = et_date
        self.session = "REGULAR"
        self.ts = f"{et_date}T14:00:00+00:00"
        self.regime = {"k": trend}
        self.rets = {"1d": ret}


def _paired_day(day, up_ret, down_ret, n=5):
    out = []
    for _ in range(n):
        out.append(_Obs(day, "UP", up_ret))
        out.append(_Obs(day, "DOWN", down_ret))
    return out


def test_block_status_reflects_the_n_actually_tested():
    """차이가 0 인 날은 부호검정 n 에서 빠진다 — 적정성도 그 n 으로 판단해야 한다.

    MIN_BLOCK_DAYS 일을 채웠어도 대부분이 '차이 0' 이면 실제 검정력은 그만큼 없다.
    status 가 ok 라고 말하면 판정 게이트가 실제보다 후하게 열린다.
    """
    obs = []
    # 차이가 0 인 날을 MIN_BLOCK_DAYS 이상 만든다.
    for i in range(MIN_BLOCK_DAYS + 2):
        obs += _paired_day(f"2026-03-{i + 1:02d}", 1.0, 1.0)
    # 실제로 차이가 있는 날은 2일뿐.
    obs += _paired_day("2026-04-01", 2.0, 1.0)
    obs += _paired_day("2026-04-02", 2.0, 1.0)

    blk = day_block_test(obs, "k", "UP", "DOWN", "1d")
    assert blk["mode"] == "paired"
    assert blk["status"] == "underpowered", (
        f"차이 0 인 날을 제외하면 검정 n 이 2 뿐인데 status={blk['status']}")


def test_block_result_keys_are_the_same_in_both_modes():
    """산출 JSON 은 기계용이다 — 모드에 따라 키가 있다 없다 하면 소비자가 깨진다."""
    paired = []
    for i in range(MIN_BLOCK_DAYS + 1):
        paired += _paired_day(f"2026-03-{i + 1:02d}", 2.0, 1.0)
    p = day_block_test(paired, "k", "UP", "DOWN", "1d")

    # 하루에 라벨이 하나뿐인 정의 → 비쌍 모드
    unpaired = []
    for i in range(MIN_BLOCK_DAYS + 1):
        for _ in range(5):
            unpaired.append(_Obs(f"2026-03-{i + 1:02d}", "UP", 2.0))
    for i in range(MIN_BLOCK_DAYS + 1):
        for _ in range(5):
            unpaired.append(_Obs(f"2026-04-{i + 1:02d}", "DOWN", 1.0))
    u = day_block_test(unpaired, "k", "UP", "DOWN", "1d")

    assert u["mode"] == "unpaired" and p["mode"] == "paired"
    assert set(p) == set(u), f"키 구성이 다르다: {set(p) ^ set(u)}"


def test_block_status_ok_when_enough_nonzero_days():
    obs = []
    for i in range(MIN_BLOCK_DAYS + 1):
        obs += _paired_day(f"2026-03-{i + 1:02d}", 2.0, 1.0)
    blk = day_block_test(obs, "k", "UP", "DOWN", "1d")
    assert blk["status"] == "ok"
    assert blk["n_days"] == MIN_BLOCK_DAYS + 1
    assert blk["positive_days"] == MIN_BLOCK_DAYS + 1
