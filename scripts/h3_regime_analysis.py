#!/usr/bin/env python
"""H3 국면 예측력 분석 — 시장 국면이 후보 forward return 을 **거래당 비용을 넘는 크기로**
예측하는가.

작업지시서: reports/WORKORDER_H3_REGIME.md · 선행: reports/h2b_backfill_validation.md
입력: datasets/h3_regime_joined.jsonl (scripts/h3_regime_join.py 산출)

read-only 연구 산출물. 매매 로직·스코어 가중치·runtime config 를 일절 건드리지 않고,
국면 게이트를 **활성화하지 않는다**. "쓸 만한 국면 신호가 있다"도 "없다"도 똑같이 성공이며,
결과에 따라 가중치나 진입 규칙을 고치지 않는다.

--------------------------------------------------------------------------------
판정 원칙 — 왜 p 값으로 결론 내지 않는가
--------------------------------------------------------------------------------
표본이 5만 규모라 **아무 의미 없는 크기도 p<1e-8 이 된다.** H2-b 가 정확히 그 함정을
기록했다(rho=0.02, p<1e-8, 설명력 0.04%). 게다가 관측이 심하게 중첩돼(5분 간격 결정봉에
30~60분·수일 지평) 유효표본은 n 보다 훨씬 작고 p 는 과대추정된다.

그래서 판정 기준은 **효과 크기 대 거래당 비용**이다:
    UP 국면 평균 − DOWN 국면 평균  vs  거래당 비용(~0.34%p)
국면으로 진입을 거르는 것이 이익이 되려면, 두 국면의 수익률 차이가 최소한 그 필터를
적용해 발생하는 거래 비용을 넘어야 한다. 넘지 못하면 "통계적으로 유의하지만 쓸 수 없다".

보조 근거: Bonferroni(다중검정), 일별 부호 재현율(하루가 통째로 달라도 방향이 유지되는가),
chronological/embargo 구간 분할(random CV 는 시계열 누수라 구조적으로 금지).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from src.history.correlation import (
    assert_official_method,
    bonferroni_threshold,
    mann_whitney_u,
    sign_test,
    spearman,
)

# ---------------------------------------------------------------------------
# 판정 상수
# ---------------------------------------------------------------------------

# 거래당 왕복 마찰비용(%p). 작업지시서 §5 기준값. 국면 차이가 이보다 작으면
# 그 국면으로 진입을 거르는 것이 비용을 회수하지 못한다.
COST_PER_TRADE_PCT = 0.34

# 일별 부호 재현율 하한 — 이보다 낮으면 "며칠이 끌고 간 평균"이다.
MIN_DAY_SIGN_RATIO = 0.60

# 그룹당 최소 표본. 이하이면 그 (정의×지평) 칸은 판정하지 않는다.
MIN_GROUP_N = 200

# 일별 재현율 계산 시 그 날을 세기 위한 그룹당 최소 표본.
MIN_DAY_GROUP_N = 5

# 하루 블록 검정에 필요한 최소 '날' 수. 국면 관측의 **유효표본은 관측 수가 아니라 날 수**라
# 이 값 미만이면 어떤 p 값도 신뢰하지 않는다(판정 게이트).
MIN_BLOCK_DAYS = 8
# 이 값 이상이면 p 를 **계산해서 보여는 준다**(underpowered 로 표시). 감추는 것보다
# "이만큼밖에 없다"를 보여주는 편이 독자가 스스로 판단할 수 있다.
MIN_BLOCK_DAYS_REPORT = 5

# 발견 표본 날짜(예비 H2/H3 관측이 나온 라이브 세션 날) — 재현 표본에서 제외한다.
# 같은 날을 넣으면 발견과 재현이 겹쳐 순환 검증이 된다.
DISCOVERY_DATES = ("2026-07-15",)

LIVE_SESSIONS = ("PRE", "REGULAR", "POST")

# ⚠ 음성 대조군 키 — 당일 마감을 쓴 **오염된** 하루 국면. CATEGORICAL_DEFS 에 넣지 않는다.
LOOKAHEAD_CONTROL_KEY = "daily_trend_same_day"

# 대조군 판정 — 오염 라벨의 효과가 정상 라벨의 이 배수를 넘으면 "차단 장치가 실제로
# 일을 하고 있다"는 증거다. 예비 관측의 실측 부풀림은 약 15배였다.
CONTROL_INFLATION_ALERT = 3.0

# 국면 정의(이산) — (키, 라벨, UP 쪽 값, DOWN 쪽 값, 설명)
CATEGORICAL_DEFS = (
    ("intraday_trend", "A 장중 5m EMA20 추세", "UP", "DOWN",
     "라이브 기본 정의(builder). 예비 관측: 효과 없음"),
    ("daily_trend", "B 하루 국면(전날까지)", "UP", "DOWN",
     "★ 예비에서 당일 종가를 쓰면 +0.120%p, 전날까지면 +0.006%p 로 붕괴한 정의"),
    ("long_trend", "C 장기 5m EMA200 추세", "UP", "DOWN",
     "더 긴 창(5m 200봉 ≈ 1 거래일)"),
    ("breadth_regime", "D breadth(유니버스 EMA20 위 비율)", "UP", "DOWN",
     "시장 폭 — 벤치마크 1종목이 아닌 유니버스 전체"),
    ("vol_regime", "F 변동성 국면", "HIGH", "LOW",
     "실현변동성 / 자기 과거 중앙값(인과적 기준선)"),
)

# 국면 정의(연속) — Spearman 용
CONTINUOUS_DEFS = (
    ("intraday_vs_ema", "E 연속 vs_ema20 (장중)"),
    ("long_vs_ema", "E' 연속 vs_ema200 (장기)"),
    ("daily_vs_ema", "E'' 연속 일간 vs_ema20"),
    ("breadth", "D' 연속 breadth"),
    ("vol_ratio", "F' 연속 변동성 비율"),
)

# 지평 — (키, 라벨, embargo 분). embargo 는 지평 이상이어야 한다.
HORIZONS = (
    ("30", "30m", 30),
    ("60", "60m", 60),
    ("eod", "EOD", 390),        # 정규장 1일
    ("1d", "익일", 1440),
    ("2d", "2일", 2880),
    ("3d", "3일", 4320),
)


def _repo_path(rel: str) -> str:
    return rel if os.path.isabs(rel) else os.path.join(_REPO_ROOT, rel)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_sha() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, check=True, cwd=_REPO_ROOT).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                               text=True, check=True, cwd=_REPO_ROOT).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# 관측 로드
# ---------------------------------------------------------------------------

class Obs:
    """분석에 필요한 것만 담은 경량 관측(전체 후보 dict 를 들고 있으면 메모리가 튄다)."""
    __slots__ = ("symbol", "et_date", "session", "ts", "regime", "rets")

    def __init__(self, symbol, et_date, session, ts, regime, rets):
        self.symbol = symbol
        self.et_date = et_date
        self.session = session
        self.ts = ts
        self.regime = regime
        self.rets = rets          # horizon key -> return_pct (완결된 것만)


def load_observations(path: str, sessions, exclude_dates) -> tuple[list[Obs], dict]:
    """조인 데이터셋 → 관측 리스트 + 커버리지 통계.

    미완결 forward 는 **담지 않는다**(0 대입/조용한 drop 금지 — 애초에 키를 만들지 않아
    다운스트림이 실수로 0 으로 세지 못하게 한다).
    """
    obs: list[Obs] = []
    cov = {"rows": 0, "kept": 0, "excluded_session": 0, "excluded_date": 0,
           "by_session": defaultdict(int), "by_symbol": defaultdict(int),
           "dates": set(), "horizon_complete": defaultdict(int),
           "regime_present": defaultdict(int)}
    with open(path) as f:
        for line in f:
            row = json.loads(line)
            cov["rows"] += 1
            if row["session"] not in sessions:
                cov["excluded_session"] += 1
                continue
            if row["et_date"] in exclude_dates:
                cov["excluded_date"] += 1
                continue
            rets = {}
            for key, fwd in (row.get("forwards") or {}).items():
                if fwd.get("complete") and fwd.get("return_pct") is not None:
                    rets[key] = float(fwd["return_pct"])
            for key, fwd in (row.get("forwards_ext") or {}).items():
                if fwd.get("complete") and fwd.get("return_pct") is not None:
                    rets[key] = float(fwd["return_pct"])
            for k in rets:
                cov["horizon_complete"][k] += 1
            regime = dict(row.get("regime") or {})
            # ⚠ 음성 대조군 라벨을 같은 dict 에 넣되, CATEGORICAL_DEFS 에는 절대 넣지 않는다.
            #    오직 lookahead_control_report() 만 이 키를 읽는다.
            ctl = row.get("lookahead_control") or {}
            if ctl.get("daily_trend_same_day"):
                regime[LOOKAHEAD_CONTROL_KEY] = ctl["daily_trend_same_day"]
            for k, v in (row.get("regime") or {}).items():
                if v is not None and not k.endswith("_asof") and k not in (
                        "decision_ts_utc", "et_date"):
                    cov["regime_present"][k] += 1
            cov["kept"] += 1
            cov["by_session"][row["session"]] += 1
            cov["by_symbol"][row["symbol"]] += 1
            cov["dates"].add(row["et_date"])
            obs.append(Obs(row["symbol"], row["et_date"], row["session"],
                           row["bar_ts_utc"], regime, rets))
    cov["by_session"] = dict(cov["by_session"])
    cov["by_symbol"] = dict(cov["by_symbol"])
    cov["horizon_complete"] = dict(cov["horizon_complete"])
    cov["regime_present"] = dict(cov["regime_present"])
    cov["n_dates"] = len(cov["dates"])
    cov["date_min"] = min(cov["dates"]) if cov["dates"] else None
    cov["date_max"] = max(cov["dates"]) if cov["dates"] else None
    cov["dates"] = sorted(cov["dates"])
    return obs, cov


# ---------------------------------------------------------------------------
# 기술통계
# ---------------------------------------------------------------------------

def _mean(v):
    return sum(v) / len(v) if v else None


def _median(v):
    if not v:
        return None
    s = sorted(v)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def _winrate(v):
    return (sum(1 for x in v if x > 0) / len(v)) if v else None


def split_groups(obs: list[Obs], key: str, up_val: str, down_val: str,
                 horizon: str) -> tuple[list[float], list[float]]:
    """(UP 쪽 수익률, DOWN 쪽 수익률). 라벨 없음/NEUTRAL/미완결은 자동 제외."""
    up, down = [], []
    for o in obs:
        r = o.rets.get(horizon)
        if r is None:
            continue
        v = o.regime.get(key)
        if v == up_val:
            up.append(r)
        elif v == down_val:
            down.append(r)
    return up, down


def day_sign_ratio(obs: list[Obs], key: str, up_val: str, down_val: str,
                   horizon: str, overall_diff: float) -> dict:
    """일별 (UP평균 − DOWN평균) 부호가 전체 부호와 일치하는 비율.

    LOSO 는 n 이 크면 fold 마다 거의 같은 값이 나와 '1.00' 으로 포화된다(H2-b 에서 실측).
    그래서 하루를 **독립 표본**으로 보고 그 날 안에서만 차이를 계산한다 — 훨씬 엄격하다.
    """
    by_day_up = defaultdict(list)
    by_day_down = defaultdict(list)
    for o in obs:
        r = o.rets.get(horizon)
        if r is None:
            continue
        v = o.regime.get(key)
        if v == up_val:
            by_day_up[o.et_date].append(r)
        elif v == down_val:
            by_day_down[o.et_date].append(r)
    sign = 1 if overall_diff > 0 else (-1 if overall_diff < 0 else 0)
    days, same = 0, 0
    for d in sorted(set(by_day_up) & set(by_day_down)):
        u, dn = by_day_up[d], by_day_down[d]
        if len(u) < MIN_DAY_GROUP_N or len(dn) < MIN_DAY_GROUP_N:
            continue
        diff = _mean(u) - _mean(dn)
        days += 1
        if (1 if diff > 0 else (-1 if diff < 0 else 0)) == sign:
            same += 1
    return {"days": days, "same_sign_days": same,
            "ratio": (same / days) if days else None}


def day_block_test(obs: list[Obs], key: str, up_val: str, down_val: str,
                   horizon: str) -> dict:
    """★ 하루를 **블록 1개**로 묶은 정직한 검정 — 이 리포트 판정의 실제 축.

    왜 관측 단위 p 를 믿으면 안 되는가(실측):
      - 하루 국면(B)은 라벨이 **하루에 하나**다. 관측 22,500건처럼 보이지만 독립 단위는
        **16일**이다. 그 상태의 관측 단위 p 가 1.57e-45 로 나왔다 — 순수한 의사복제
        (pseudo-replication)다.
      - 다지평(1~3일) forward 는 5분 간격 결정봉끼리 창이 거의 완전히 겹치고, 같은 날
        12 종목이 같은 시장 움직임을 공유한다. 관측을 독립으로 세면 안 된다.

    두 모드:
      paired   — 국면이 하루 안에서 변하는 정의(A/C/D/F). 그 날 안에서만 UP−DOWN 을 구해
                 (시장 공통 요인이 상쇄된다) 날짜별 차이의 부호를 부호검정한다.
      unpaired — 국면이 하루에 하나인 정의(B). UP 인 날들의 일평균 vs DOWN 인 날들의
                 일평균을 날짜 단위로 비교한다.
    """
    up_by_day = defaultdict(list)
    down_by_day = defaultdict(list)
    for o in obs:
        r = o.rets.get(horizon)
        if r is None:
            continue
        v = o.regime.get(key)
        if v == up_val:
            up_by_day[o.et_date].append(r)
        elif v == down_val:
            down_by_day[o.et_date].append(r)

    paired_days = sorted(set(up_by_day) & set(down_by_day))
    paired_days = [d for d in paired_days
                   if len(up_by_day[d]) >= MIN_DAY_GROUP_N
                   and len(down_by_day[d]) >= MIN_DAY_GROUP_N]

    if len(paired_days) >= MIN_BLOCK_DAYS_REPORT:
        diffs = [_mean(up_by_day[d]) - _mean(down_by_day[d]) for d in paired_days]
        pos = sum(1 for x in diffs if x > 0)
        n = sum(1 for x in diffs if x != 0)
        return {"mode": "paired", "n_days": len(diffs),
                "n_up_days": None, "n_down_days": None,
                "mean_diff_pp": _mean(diffs), "median_diff_pp": _median(diffs),
                "positive_days": pos, "tested_days": n,
                "p": sign_test(pos, n) if n else None,
                # 적정성은 **실제 검정에 쓰인 n**(차이가 0 이 아닌 날)으로 판단한다.
                # len(diffs) 로 보면 '차이 0' 인 날이 많을 때 검정력이 없는데도 ok 가 되어
                # 판정 게이트가 실제보다 후하게 열린다.
                "status": "ok" if n >= MIN_BLOCK_DAYS else "underpowered"}

    # 하루에 라벨이 하나뿐인 정의 — 날짜 단위 비표본 비교.
    up_days, down_days = [], []
    for d, vals in up_by_day.items():
        if len(vals) >= MIN_DAY_GROUP_N and d not in down_by_day:
            up_days.append(_mean(vals))
    for d, vals in down_by_day.items():
        if len(vals) >= MIN_DAY_GROUP_N and d not in up_by_day:
            down_days.append(_mean(vals))
    if len(up_days) < MIN_BLOCK_DAYS_REPORT or len(down_days) < MIN_BLOCK_DAYS_REPORT:
        return {"mode": "unpaired", "n_days": len(up_days) + len(down_days),
                "n_up_days": len(up_days), "n_down_days": len(down_days),
                "mean_diff_pp": (_mean(up_days) - _mean(down_days))
                if (up_days and down_days) else None,
                "p": None, "status": "insufficient_days"}
    mw = mann_whitney_u(up_days, down_days)
    enough = min(len(up_days), len(down_days)) >= MIN_BLOCK_DAYS
    return {"mode": "unpaired", "n_days": len(up_days) + len(down_days),
            "n_up_days": len(up_days), "n_down_days": len(down_days),
            "mean_diff_pp": _mean(up_days) - _mean(down_days),
            "median_diff_pp": (_median(up_days) - _median(down_days)),
            "positive_days": None, "p": mw.p,
            "status": "ok" if enough else "underpowered"}


def chronological_check(obs: list[Obs], key: str, up_val: str, down_val: str,
                        horizon: str, embargo_min: int, ratio: float = 0.7) -> dict:
    """구간 분할 안정성 — train/test 각각의 UP−DOWN 차이 부호가 유지되는가.

    random CV 는 시계열 누수라 **구조적으로 금지**되어 있다(`OFFICIAL_SPLIT_METHODS`).
    모형을 적합하지 않으므로 홀드아웃 '검증'이 아니라 **구간 분할 안정성 점검**이다.
    embargo 는 지평 이상으로 둬서 train 마지막 관측의 forward 창이 test 로 새지 않게 한다.
    """
    method = assert_official_method("embargo")
    ordered = sorted(obs, key=lambda o: o.ts)
    k = int(len(ordered) * ratio)
    if k < 2 or k >= len(ordered):
        return {"method": method, "embargo_min": embargo_min, "status": "insufficient"}
    boundary = datetime.fromisoformat(ordered[k].ts.replace("Z", "+00:00"))
    train = ordered[:k]
    # embargo: 경계로부터 embargo_min 분 안에 시작하는 test 관측을 버린다.
    test = [o for o in ordered[k:]
            if (datetime.fromisoformat(o.ts.replace("Z", "+00:00"))
                - boundary).total_seconds() / 60.0 >= embargo_min]

    def diff_of(sub):
        u, d = split_groups(sub, key, up_val, down_val, horizon)
        if len(u) < MIN_DAY_GROUP_N or len(d) < MIN_DAY_GROUP_N:
            return None, len(u), len(d)
        return _mean(u) - _mean(d), len(u), len(d)

    tr, tru, trd = diff_of(train)
    te, teu, ted = diff_of(test)
    preserved = None
    if tr is not None and te is not None:
        preserved = (tr > 0) == (te > 0)
    return {"method": method, "embargo_min": embargo_min, "status": "ok",
            "train_diff_pp": tr, "train_n_up": tru, "train_n_down": trd,
            "test_diff_pp": te, "test_n_up": teu, "test_n_down": ted,
            "sign_preserved": preserved}


# ---------------------------------------------------------------------------
# 분석
# ---------------------------------------------------------------------------

def analyze(obs: list[Obs], alpha: float) -> dict:
    n_tests = len(CATEGORICAL_DEFS) * len(HORIZONS) + len(CONTINUOUS_DEFS) * len(HORIZONS)
    thr = bonferroni_threshold(alpha, n_tests)

    results = []
    for key, label, up_val, down_val, note in CATEGORICAL_DEFS:
        for hkey, hlabel, embargo in HORIZONS:
            up, down = split_groups(obs, key, up_val, down_val, hkey)
            row = {"regime_def": key, "regime_label": label, "regime_note": note,
                   "up_label": up_val, "down_label": down_val,
                   "horizon": hkey, "horizon_label": hlabel,
                   "n_up": len(up), "n_down": len(down)}
            if len(up) < MIN_GROUP_N or len(down) < MIN_GROUP_N:
                row.update({"status": "insufficient_sample", "diff_pp": None,
                            "p": None, "bonferroni_pass": False,
                            "cost_floor_pass": False})
                results.append(row)
                continue
            mu_up, mu_dn = _mean(up), _mean(down)
            diff = mu_up - mu_dn
            mw = mann_whitney_u(up, down)
            ds = day_sign_ratio(obs, key, up_val, down_val, hkey, diff)
            blk = day_block_test(obs, key, up_val, down_val, hkey)
            sp = chronological_check(obs, key, up_val, down_val, hkey, embargo)
            row.update({
                "status": "ok",
                "mean_up_pct": mu_up, "mean_down_pct": mu_dn,
                "median_up_pct": _median(up), "median_down_pct": _median(down),
                "winrate_up": _winrate(up), "winrate_down": _winrate(down),
                "diff_pp": diff,
                "abs_diff_pp": abs(diff),
                "mwu_u": mw.u, "p": mw.p, "rank_biserial": mw.rank_biserial,
                "bonferroni_threshold": thr,
                "bonferroni_pass": bool(mw.p is not None and mw.p < thr),
                # ★ 판정의 축 — 효과 크기가 거래당 비용을 넘는가.
                "cost_floor_pct": COST_PER_TRADE_PCT,
                "cost_floor_pass": bool(abs(diff) >= COST_PER_TRADE_PCT),
                "cost_ratio": abs(diff) / COST_PER_TRADE_PCT,
                "day_sign": ds,
                # ★ 판정의 유의성 축 — 관측 단위 p 가 아니라 하루 블록 단위 p.
                "day_block": blk,
                "block_bonferroni_pass": bool(
                    blk.get("p") is not None and blk["p"] < thr),
                "split": sp,
            })
            results.append(row)

    continuous = []
    for key, label in CONTINUOUS_DEFS:
        for hkey, hlabel, embargo in HORIZONS:
            xs, ys = [], []
            for o in obs:
                r = o.rets.get(hkey)
                v = o.regime.get(key)
                if r is None or v is None:
                    continue
                xs.append(float(v))
                ys.append(r)
            res = spearman(xs, ys)
            continuous.append({
                "regime_def": key, "regime_label": label,
                "horizon": hkey, "horizon_label": hlabel,
                "n": res.n, "rho": res.rho, "p": res.p,
                "bonferroni_threshold": thr,
                "bonferroni_pass": bool(res.p is not None and res.p < thr),
            })

    return {"n_tests": n_tests, "alpha": alpha, "bonferroni_threshold": thr,
            "categorical": results, "continuous": continuous}


def lookahead_control_report(obs: list[Obs]) -> dict:
    """★ 음성 대조군 — look-ahead 차단이 실제로 일을 하는가.

    같은 데이터·같은 지평에서 하루 국면을 두 가지로 라벨링해 효과 크기를 비교한다:
      clean        전날 마감까지만 (분석에 쓴 것)
      contaminated 당일 마감 포함  (결정 시점엔 알 수 없는 값 — 예비 관측이 쓴 것)

    예비 관측에서 이 차이 하나가 +0.006%p → +0.120%p (~15배)를 만들었다. 여기서도
    오염 쪽 효과가 크게 부풀면, 우리 차단 장치가 막고 있는 것이 **실재하는 오염**임이
    확인된다. 반대로 둘이 비슷하면 차단 장치는 이 데이터에서 무해했다는 뜻이다.
    """
    rows = []
    for hkey, hlabel, _emb in HORIZONS:
        clean_up, clean_dn = split_groups(obs, "daily_trend", "UP", "DOWN", hkey)
        c_up, c_dn = split_groups(obs, LOOKAHEAD_CONTROL_KEY, "UP", "DOWN", hkey)
        clean_diff = (_mean(clean_up) - _mean(clean_dn)) if (clean_up and clean_dn) else None
        cont_diff = (_mean(c_up) - _mean(c_dn)) if (c_up and c_dn) else None
        infl = None
        if clean_diff is not None and cont_diff is not None and abs(clean_diff) > 1e-9:
            infl = abs(cont_diff) / abs(clean_diff)
        rows.append({
            "horizon": hkey, "horizon_label": hlabel,
            "clean_diff_pp": clean_diff, "clean_n": len(clean_up) + len(clean_dn),
            "contaminated_diff_pp": cont_diff, "contaminated_n": len(c_up) + len(c_dn),
            "inflation_x": infl,
            "alert": bool(infl is not None and infl >= CONTROL_INFLATION_ALERT),
        })
    infls = [r["inflation_x"] for r in rows if r["inflation_x"] is not None]
    return {"rows": rows,
            "max_inflation_x": max(infls) if infls else None,
            "guard_is_load_bearing": bool(any(r["alert"] for r in rows)),
            "note": ("오염 라벨(당일 마감 포함)이 정상 라벨보다 효과를 크게 부풀리면 "
                     "차단 장치가 실재하는 오염을 막고 있다는 뜻이다.")}


def build_verdict(analysis: dict, cov: dict) -> dict:
    """판정 — 기준을 코드에 명시해 사후 해석 여지를 없앤다.

    'usable' 은 네 조건을 **모두** 만족할 때만이다:
      1. |UP−DOWN| >= 거래당 비용            (효과 크기 — 쓸 만한가)
      2. 하루 블록 검정 통과 (Bonferroni)     (우연 배제 — **관측 단위 p 가 아니다**)
      3. 하루 블록 검정이 성립할 만큼 날이 있다
      4. chronological/embargo 분할 부호 유지  (구간 안정성)

    ★ 2 번이 관측 단위 p 가 아닌 이유가 이 리포트의 핵심이다. 관측은 5분 간격 결정봉에
    30분~3일 지평이라 창이 겹치고, 같은 날 12 종목이 같은 시장을 본다. 하루 국면(B)은
    아예 라벨이 하루에 하나라 관측 22,500건이 실제로는 **16일**이다. 그 상태의 관측 단위
    p 는 1.57e-45 까지 내려간다 — 크기가 아니라 의사복제가 만든 숫자다.
    """
    usable, near_miss = [], []
    ok_rows = [r for r in analysis["categorical"] if r.get("status") == "ok"]
    for r in ok_rows:
        blk = r.get("day_block") or {}
        sign_ok = (r.get("split") or {}).get("sign_preserved")
        conds = {
            "cost_floor": bool(r["cost_floor_pass"]),
            "block_adequate": bool(blk.get("status") == "ok"),
            "block_significant": bool(r.get("block_bonferroni_pass")),
            "split_sign": bool(sign_ok),
        }
        entry = {"regime_def": r["regime_def"], "horizon": r["horizon"],
                 "diff_pp": r["diff_pp"], "cost_ratio": r["cost_ratio"],
                 "obs_p": r["p"], "block_p": blk.get("p"),
                 "block_mode": blk.get("mode"), "block_n_days": blk.get("n_days"),
                 "conditions": conds}
        if all(conds.values()):
            usable.append(entry)
        elif conds["cost_floor"]:
            near_miss.append(entry)

    ranked = sorted((r for r in ok_rows if r["diff_pp"] is not None),
                    key=lambda r: -abs(r["diff_pp"]))
    best = ranked[0] if ranked else None

    # 관측 단위 p 와 하루 블록 p 가 얼마나 벌어지는지 — 의사복제의 크기를 수치로 남긴다.
    inflation = []
    for r in ok_rows:
        blk = r.get("day_block") or {}
        if r.get("p") and blk.get("p"):
            inflation.append({"regime_def": r["regime_def"], "horizon": r["horizon"],
                              "obs_p": r["p"], "block_p": blk["p"],
                              "block_mode": blk.get("mode"),
                              "block_n_days": blk.get("n_days")})
    inflation.sort(key=lambda x: x["obs_p"])

    # 지평별 결론 — "전체 no/inconclusive" 하나로 뭉뚱그리면 어디는 확실히 없고 어디는
    # 표본이 모자란 것뿐인지가 사라진다. 그 구분이 다음 행동을 가른다.
    by_horizon = {}
    for hkey, hlabel, _emb in HORIZONS:
        cells = [r for r in ok_rows if r["horizon"] == hkey]
        if not cells:
            by_horizon[hkey] = {"conclusion": "no_data"}
            continue
        top = max(abs(c["diff_pp"]) for c in cells)
        over_cost = [c for c in cells if c["cost_floor_pass"]]
        blocked = [c for c in cells if c.get("block_bonferroni_pass")]
        # ★ 두 게이트는 **같은 칸**에서 동시에 성립해야 한다. 리스트를 각각 필터해
        #   `blocked and over_cost` 로 보면, 칸 X 가 비용만 통과하고 칸 Y 가 블록검정만
        #   통과할 때 동시 통과 칸이 0개인데도 signal 이 된다. 이 표는 "국면 게이트를
        #   켤 가치가 있는가"를 사람이 훑는 곳이라 가짜 signal 은 잘못된 행동이 된다.
        if any(c["cost_floor_pass"] and c.get("block_bonferroni_pass") for c in cells):
            concl = "signal"
        elif not over_cost:
            # 가장 큰 효과조차 비용 밑 — 표본이 더 있어도 쓸 수 없다.
            concl = "no"
        else:
            # 효과는 비용을 넘지만 하루 블록 단위로는 노이즈와 구분되지 않는다.
            concl = "underpowered"
        by_horizon[hkey] = {
            "conclusion": concl, "max_abs_diff_pp": top,
            "max_cost_ratio": top / COST_PER_TRADE_PCT,
            "cells_over_cost": len(over_cost),
            "cells_block_significant": len(blocked),
        }

    if usable:
        signal, confidence = "yes", ("moderate" if len(usable) >= 2 else "low")
    elif near_miss:
        signal, confidence = "inconclusive", "low"
    elif not ok_rows:
        signal, confidence = "inconclusive", "low"
    else:
        signal = "no"
        top = abs(best["diff_pp"]) if best else 0.0
        confidence = "moderate" if top < COST_PER_TRADE_PCT * 0.5 else "low"

    limitations = [
        "★ 유효표본은 관측 수가 아니라 **날 수**다. 관측이 심하게 중첩되고(5분 간격 결정봉에 "
        "30분~3일 지평) 같은 날 12 종목이 같은 시장을 공유한다. 하루 국면(B)은 라벨이 하루에 "
        "하나라 관측 22,500건의 독립 단위가 16일뿐이다. 그래서 판정은 관측 단위 p 가 아니라 "
        "하루 블록 검정에 근거한다 — 표 ⑤ 의 `관측p` 열은 참고용이며 과대추정이다.",
        "다지평(1~3일) 결과는 특히 취약하다. 국면 라벨이 며칠씩 지속되므로 UP/DOWN 그룹이 "
        "사실상 '몇 개의 시장 국면 구간'이고, 표본 기간이 짧으면 한두 번의 시장 스윙이 "
        "전체 차이를 만든다.",
        "후보는 스크리너가 min_score 이상으로 뽑은 것만이라 표본이 이미 선택편향돼 있다. "
        "여기의 결과는 '후보 안에서 국면이 수익을 가르는가'이지 '전 구간 예측력'이 아니다.",
        "체결/슬리피지 미반영 — 봉 종가 기준 forward return 이다. 실제 필터 적용 이익은 "
        "여기 수치보다 나쁠 가능성이 높다.",
        f"거래당 비용 기준값 {COST_PER_TRADE_PCT}%p 는 작업지시서 제시값이다. 이 값이 바뀌면 "
        "cost_floor 판정도 바뀐다.",
        "표본 기간이 짧다 — 벤치마크(QQQ) 백필이 2026-05-22 이후 47 거래일뿐이라 국면 정의가 "
        "붙는 구간이 제한된다. 여러 시장 국면(강세장/약세장/횡보)을 가로지르는 검증이 아니다.",
        "일간 EMA(50) 장기 국면은 계산하지 못했다 — 벤치마크 백필이 47 거래일뿐이라 50일 EMA 가 "
        "성립하지 않는다. 대신 5m EMA200(≈1 거래일)으로 '더 긴 창'을 대신했다. 진짜 다개월 "
        "국면 검증은 벤치마크 장기 백필이 선행되어야 한다.",
    ]
    dt_present = cov.get("regime_present", {}).get("daily_trend", 0)
    if dt_present < cov.get("kept", 1) * 0.5:
        limitations.insert(1, (
            f"하루 국면(B)은 벤치마크 일봉 EMA warm-up 때문에 표본의 일부에만 붙는다 "
            f"(daily_trend 부착 {dt_present:,d} / {cov['kept']:,d} 행, 16 거래일). "
            f"그 16일 구간의 관측일 뿐이며 일반화할 수 없다."))

    return {
        "usable_regime_signal": signal,
        "best_definition": (None if best is None else {
            "regime_def": best["regime_def"], "regime_label": best["regime_label"],
            "horizon": best["horizon"], "diff_pp": best["diff_pp"],
            "cost_ratio": best["cost_ratio"],
            "cost_floor_pass": best["cost_floor_pass"],
            "obs_p": best["p"],
            "block_p": (best.get("day_block") or {}).get("p"),
            "block_significant": bool(best.get("block_bonferroni_pass"))}),
        "confidence": confidence,
        "usable_cells": usable,
        "near_miss_cells": near_miss,
        "by_horizon": by_horizon,
        "pseudo_replication_evidence": inflation[:8],
        "criteria": {
            "cost_floor_pct": COST_PER_TRADE_PCT,
            "min_block_days": MIN_BLOCK_DAYS,
            "min_group_n": MIN_GROUP_N,
            "significance_axis": "day_block (NOT observation-level p)",
            "requires": ["cost_floor", "block_adequate", "block_significant",
                         "split_sign"],
        },
        "limitations": limitations,
    }


# ---------------------------------------------------------------------------
# 렌더
# ---------------------------------------------------------------------------

def _f(v, nd=3, suffix=""):
    return "—" if v is None else f"{v:+.{nd}f}{suffix}"


def _pct(v, nd=1):
    return "—" if v is None else f"{v * 100:.{nd}f}%"


def _p(v):
    if v is None:
        return "—"
    return f"{v:.2e}" if v < 1e-4 else f"{v:.4f}"


def render_md(payload: dict, cmd: str) -> str:
    meta, cov, an, vd = payload["meta"], payload["coverage"], payload["analysis"], payload["verdict"]
    par = payload["parity_test"]
    L: list[str] = []
    a = L.append

    a("# H3 국면 예측력 검증 리포트")
    a("")
    a(f"> 생성: {meta['generated_at']} · 커밋 `{meta['git_sha'][:12]}` · "
      f"입력 `{os.path.basename(meta['input'])}`")
    a(f"> 작업지시서: `reports/WORKORDER_H3_REGIME.md` · 선행: `reports/h2b_backfill_validation.md`")
    a("")
    a("> **read-only 연구 산출물.** 매매 로직·스코어 가중치·runtime config 변경 0건, "
      "국면 게이트 활성화 0건.")
    a("> \"쓸 만한 국면 신호가 있다\"와 \"없다\"는 똑같이 성공이며, 결과에 따라 아무것도 고치지 않았다.")
    a("")

    # ① 판정
    a("## ① 판정 요약")
    a("")
    a("| 항목 | 값 |")
    a("|---|---|")
    a(f"| **쓸 만한 국면 신호** | **{vd['usable_regime_signal'].upper()}** |")
    a(f"| 확신도 | {vd['confidence']} |")
    a(f"| 패리티 | **{par['status']}** |")
    best = vd["best_definition"]
    if best:
        a(f"| 최대 효과 | `{best['regime_def']}` @ {best['horizon']} = "
          f"{best['diff_pp']:+.4f}%p (비용의 {best['cost_ratio']:.2f}배) |")
        a(f"| 최대 효과의 블록 p | {_p(best.get('block_p'))} "
          f"(관측 단위 p {_p(best.get('obs_p'))} — 과대) |")
    a(f"| 거래당 비용 기준 | {COST_PER_TRADE_PCT}%p |")
    a(f"| 비용 넘는 (정의×지평) | {len(vd['usable_cells'])}개 |")
    a("")
    if vd["usable_regime_signal"] == "no":
        a("**결론: 검증한 어떤 국면 정의도 거래당 비용을 넘는 예측력을 보이지 않는다.** "
          "일부 칸이 통계적으로 유의하더라도(n 이 크면 당연하다) 효과 크기가 비용 밑이라 "
          "국면 필터로 쓸 수 없다.")
    elif vd["usable_regime_signal"] == "yes":
        a("**결론: 비용을 넘는 국면 정의가 발견됐다.** 단 이는 오프라인 관측이며, "
          "shadow A/B 재확인 전에는 매매에 반영하지 않는다(이번 작업 범위 밖).")
    else:
        a("**결론: 판정 불가(inconclusive).** 효과 크기가 비용 근처인 칸이 있으나 "
          "재현성·구간 안정성 조건을 모두 만족하지 못했다.")
    a("")
    a(f"판정 기준(코드 고정): |UP−DOWN| ≥ {COST_PER_TRADE_PCT}%p **그리고** "
      f"**하루 블록 검정** Bonferroni 통과(관측 단위 p 아님) **그리고** 블록이 성립할 만큼 "
      f"날이 있음(≥{MIN_BLOCK_DAYS}일) **그리고** embargo 분할 부호 유지.")
    a("")

    a("### 지평별 결론")
    a("")
    a("| 지평 | 결론 | 최대 \\|차이\\| (%p) | 비용대비 | 비용 넘는 칸 | 블록 유의 칸 |")
    a("|---|---|---:|---:|---:|---:|")
    _CONCL = {"no": "**없음** (최대 효과조차 비용 밑)",
              "underpowered": "표본 부족 (효과는 비용 이상이나 노이즈와 구분 불가)",
              "signal": "**신호 있음**", "no_data": "데이터 없음"}
    for hkey, hlabel, _e in HORIZONS:
        h = vd.get("by_horizon", {}).get(hkey, {})
        if h.get("conclusion") == "no_data":
            a(f"| {hlabel} | 데이터 없음 | — | — | — | — |")
            continue
        a(f"| {hlabel} | {_CONCL.get(h.get('conclusion'), '—')} | "
          f"{h.get('max_abs_diff_pp', 0):.4f} | {h.get('max_cost_ratio', 0):.2f}x | "
          f"{h.get('cells_over_cost', 0)} | {h.get('cells_block_significant', 0)} |")
    a("")
    a("짧은 지평(30m/60m/EOD)은 **표본이 충분한데도 효과가 비용 밑**이라 결론이 확정적이다. "
      "긴 지평(1~3일)은 효과 크기가 비용을 넘지만 독립 단위(날)가 16~36개뿐이라 "
      "노이즈와 구분되지 않는다 — '신호가 없다'가 아니라 **'이 표본으로는 판별할 수 없다'** 다.")
    a("")

    # ② 패리티
    a("## ② 패리티 (수용 기준)")
    a("")
    a("국면 계산이 라이브를 재현하지 못하면, 그 국면으로 만든 어떤 상관도 의미가 없다.")
    a("")
    a("| 항목 | 기준 | 실측 |")
    a("|---|---|---|")
    a(f"| 대상 세션 | — | {par['target_session']} |")
    a(f"| 국면 관측 사이클 | — | {par['live_cycles']} |")
    a(f"| 봉 정렬 일치 | 전건 | {par['matched_cycles']} |")
    a(f"| 수치 최대오차 | ≤ 1e-6 | **{par['max_abs_diff']}** |")
    a(f"| 불변식 `source_bar ≤ asof` | 위반 0 | {par['invariant_violations']} |")
    a(f"| **판정** | PASS | **{par['status']}** |")
    a("")
    a(f"비고: {par['notes']}")
    a("")

    # ③ look-ahead 음성 대조군
    ctl = payload.get("lookahead_control") or {}
    a("## ③ look-ahead 음성 대조군 — 차단 장치는 일을 했는가")
    a("")
    a("이 작업의 성패는 look-ahead 차단이다. 그래서 같은 데이터에 하루 국면을 **두 가지로** "
      "라벨링해 비교한다 — `clean`(전날 마감까지, 분석에 쓴 것) vs `contaminated`"
      "(당일 마감 포함, 결정 시점엔 알 수 없는 값).")
    a("")
    a("| 지평 | clean 차이(%p) | 오염 차이(%p) | 부풀림 |")
    a("|---|---:|---:|---:|")
    for r in ctl.get("rows", []):
        infl = r.get("inflation_x")
        mark = " ⚠" if r.get("alert") else ""
        a(f"| {r['horizon_label']} | {_f(r.get('clean_diff_pp'), 4)} | "
          f"{_f(r.get('contaminated_diff_pp'), 4)} | "
          f"{'—' if infl is None else f'{infl:.1f}x'}{mark} |")
    a("")
    if ctl.get("guard_is_load_bearing"):
        a(f"**차단 장치는 실제로 일을 했다** — 당일 마감을 넣는 것만으로 효과가 최대 "
          f"{ctl['max_inflation_x']:.1f}배 부풀었다. 예비 관측의 +0.006%p → +0.120%p(~15배)와 "
          f"같은 종류의 오염이며, 이 리포트의 수치는 그 오염 없이 얻은 것이다.")
    else:
        a("이 표본에서는 오염 라벨과 정상 라벨의 효과 차이가 크지 않았다. 차단 장치가 "
          "불필요했다는 뜻이 아니라, 이 구간에서는 그 경로로 새어들 신호 자체가 작았다는 뜻이다.")
    a("")

    # ④ 커버리지
    a("## ④ 커버리지")
    a("")
    a(f"- 입력 {cov['rows']:,d} 행 → 분석 {cov['kept']:,d} 행 "
      f"(세션 제외 {cov['excluded_session']:,d}, 발견일 제외 {cov['excluded_date']:,d})")
    a(f"- 기간 {cov['date_min']} ~ {cov['date_max']} ({cov['n_dates']} 거래일) · "
      f"세션 {cov['by_session']}")
    a("")
    a("**지평별 완결 관측 수** — 지평이 길수록 데이터 끝에서 잘려 표본이 준다.")
    a("")
    a("| 지평 | 완결 n |")
    a("|---|---:|")
    for hkey, hlabel, _ in HORIZONS:
        a(f"| {hlabel} | {cov['horizon_complete'].get(hkey, 0):,d} |")
    a("")
    a("**국면 라벨 부착 수** — 벤치마크 warm-up/커버리지에 따라 다르다.")
    a("")
    a("| 라벨 | 부착 n |")
    a("|---|---:|")
    for k in sorted(cov["regime_present"]):
        a(f"| `{k}` | {cov['regime_present'][k]:,d} |")
    a("")

    # ④ 본 표
    a("## ⑤ 국면 × 지평 — UP vs DOWN 효과 크기")
    a("")
    a(f"검정 {an['n_tests']}회, α={an['alpha']} → Bonferroni 임계 **{an['bonferroni_threshold']:.2e}**")
    a("")
    a("> ⚠ **★(유의)를 효과 크기로 읽지 말 것.** n 이 수만이면 무의미한 차이도 p<1e-8 이 된다. "
      f"판정 축은 `차이` 열이 거래당 비용({COST_PER_TRADE_PCT}%p)을 넘는가다(`비용대비` 열).")
    a("")
    a("| 국면 정의 | 지평 | n(UP/DOWN) | UP 평균 | DOWN 평균 | **차이(%p)** | **비용대비** | 승률 UP/DOWN | 관측p<br>(과대) | **블록p** | 블록 날수 |")
    a("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in an["categorical"]:
        if r.get("status") != "ok":
            a(f"| {r['regime_label']} | {r['horizon_label']} | "
              f"{r['n_up']:,d}/{r['n_down']:,d} | — | — | — | — | — | — | — | 표본부족 |")
            continue
        cost = f"**{r['cost_ratio']:.2f}x**" if r["cost_floor_pass"] else f"{r['cost_ratio']:.2f}x"
        blk = r.get("day_block") or {}
        bstar = " ★" if r.get("block_bonferroni_pass") else ""
        bp = _p(blk.get("p")) if blk.get("status") == "ok" else "—"
        nd = blk.get("n_days")
        mode = {"paired": "쌍", "unpaired": "비쌍"}.get(blk.get("mode"), "?")
        a(f"| {r['regime_label']} | {r['horizon_label']} | "
          f"{r['n_up']:,d}/{r['n_down']:,d} | {r['mean_up_pct']:+.4f} | "
          f"{r['mean_down_pct']:+.4f} | **{r['diff_pp']:+.4f}** | {cost} | "
          f"{_pct(r['winrate_up'])}/{_pct(r['winrate_down'])} | {_p(r['p'])} | "
          f"{bp}{bstar} | {'—' if nd is None else f'{nd}({mode})'} |")
    a("")
    a("`비용대비` = |차이| / 거래당 비용. **1.00x 미만이면 국면으로 걸러도 비용을 회수하지 못한다.**")
    a("")
    a("> ★ 는 **블록p** 에만 붙는다. `관측p` 는 중첩된 관측을 독립으로 세므로 과대추정이며 "
      "판정에 쓰지 않는다 — 아래 ⑥ 참조.")
    a("")

    # ⑤ 의사복제
    a("## ⑥ 왜 관측 단위 p 를 믿으면 안 되는가 (의사복제)")
    a("")
    a("국면 관측은 독립이 아니다. (1) 결정봉이 5분 간격인데 지평은 30분~3일이라 한 관측의 "
      "forward 창이 다음 여러 관측과 거의 완전히 겹친다. (2) 같은 날 12 종목이 같은 시장 "
      "움직임을 공유한다. (3) 결정적으로, **국면 라벨 자체가 하루(또는 며칠) 단위로 상수**다.")
    a("")
    a("하루 국면(B)이 극단적 사례다 — 라벨이 하루에 하나뿐이라 UP/DOWN 이 같은 날 안에 "
      "공존하지 않는다(실측: 16 일 전부 단일 라벨). 관측 22,500건처럼 보이지만 **독립 단위는 "
      "16일**이다. 그런데도 관측 단위 p 는 1e-45 까지 내려간다.")
    a("")
    a("| 국면 정의 | 지평 | 관측 p (과대) | 블록 p (정직) | 블록 모드 | 날 수 |")
    a("|---|---|---:|---:|---:|---:|")
    for e in vd.get("pseudo_replication_evidence", []):
        a(f"| `{e['regime_def']}` | {e['horizon']} | {_p(e['obs_p'])} | "
          f"{_p(e['block_p'])} | {e['block_mode']} | {e['block_n_days']} |")
    a("")
    a("그래서 이 리포트의 판정 축은 **블록 p** 다. 하루를 블록 1개로 묶어, 국면이 하루 안에서 "
      "변하는 정의는 그 날 안의 UP−DOWN 차이를 날짜별로 부호검정하고(시장 공통 요인 상쇄), "
      "하루에 라벨이 하나인 정의는 날짜 단위 평균끼리 비교한다.")
    a("")

    # ⑥ 연속 지표
    a("## ⑦ 연속 국면 지표 — Spearman")
    a("")
    a("이산화(UP/DOWN)가 정보를 버렸을 가능성을 배제하기 위해 연속값으로도 본다.")
    a("")
    a("| 지표 | 지평 | n | rho | p |")
    a("|---|---|---:|---:|---:|")
    for r in an["continuous"]:
        star = " ★" if r["bonferroni_pass"] else ""
        a(f"| {r['regime_label']} | {r['horizon_label']} | {r['n']:,d} | "
          f"{_f(r['rho'])} | {_p(r['p'])}{star} |")
    a("")
    a("> 실용적 하한은 |rho| ≥ 0.05(H2-b 와 동일 기준). 그 아래는 부호가 있어도 크기는 사실상 0 이다.")
    a("")

    # ⑥ 구간 분할
    a("## ⑧ 구간 분할 안정성 (chronological / embargo)")
    a("")
    a("random CV 는 시계열 누수라 구조적으로 금지되어 있다(`OFFICIAL_SPLIT_METHODS` 강제). "
      "모형을 적합하지 않으므로 홀드아웃 '검증'이 아니라 **구간 분할 안정성 점검**이다.")
    a("")
    a("| 국면 정의 | 지평 | embargo | train 차이 | test 차이 | 부호유지 |")
    a("|---|---|---:|---:|---:|---:|")
    for r in an["categorical"]:
        sp = r.get("split") or {}
        if sp.get("status") != "ok":
            continue
        keep = "O" if sp.get("sign_preserved") else ("X" if sp.get("sign_preserved") is False else "—")
        a(f"| {r['regime_label']} | {r['horizon_label']} | {sp['embargo_min']}m | "
          f"{_f(sp.get('train_diff_pp'), 4)} | {_f(sp.get('test_diff_pp'), 4)} | {keep} |")
    a("")

    # ⑦ 일별 부호 재현
    a("## ⑨ 일별 부호 재현율")
    a("")
    a("하루를 독립 표본으로 보고 그 날 안에서 UP−DOWN 차이를 계산해, 전체 부호와 일치하는 "
      "날의 비율을 센다. LOSO 는 n 이 크면 fold 마다 같은 값이 나와 포화되므로 쓰지 않는다.")
    a("")
    a("| 국면 정의 | 지평 | 날 수 | 부호일치 | 비율 |")
    a("|---|---|---:|---:|---:|")
    for r in an["categorical"]:
        ds = r.get("day_sign")
        if not ds or not ds.get("days"):
            continue
        a(f"| {r['regime_label']} | {r['horizon_label']} | {ds['days']} | "
          f"{ds['same_sign_days']} | {ds['ratio']:.2f} |")
    a("")

    # ⑧ 한계
    a("## ⑩ 한계")
    a("")
    for lim in vd["limitations"]:
        a(f"- {lim}")
    a("")

    # ⑨ 재현
    a("## ⑪ 재현 커맨드")
    a("")
    a("```bash")
    a("# 1) 국면 조인 (look-ahead 차단 + 확장 지평)")
    a("python scripts/h3_regime_join.py")
    a("")
    a("# 2) 패리티 회귀 (아티팩트 있는 환경에서 강제)")
    a("H3_REQUIRE_PARITY=1 python -m pytest tests/test_s001_benchmark_parity.py "
      "tests/test_regime_join.py -q")
    a("")
    a("# 3) 분석 리포트")
    a(cmd)
    a("```")
    a("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 패리티 (리포트용 요약 — 상세 회귀는 tests/test_s001_benchmark_parity.py)
# ---------------------------------------------------------------------------

def run_parity(journal_db: str, market_db: str) -> dict:
    """라이브 benchmark_context 재현 요약. 실패해도 예외로 죽지 않고 상태를 반환한다."""
    out = {"target_session": "us-phase6-s001", "target_date": "2026-07-15",
           "live_cycles": 0, "matched_cycles": 0, "max_abs_diff": None,
           "invariant_violations": 0, "status": "SKIPPED", "notes": "",
           "tolerance": 1e-6}
    if not (os.path.exists(journal_db) and os.path.exists(market_db)):
        out["notes"] = "세션 아티팩트 없음(gitignore 운영 산출물) — 패리티 미실행"
        return out
    try:
        import sqlite3

        from src.history.candidate_stream import build_decision_bars, load_1m_candles
        from src.market_context.builder import build_benchmark_context

        con = sqlite3.connect(f"file:{journal_db}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT cycle_ts, data FROM journal_cycle ORDER BY cycle_ts").fetchall()
        finally:
            con.close()
        live = []
        for cts, data in rows:
            ctx = json.loads(data).get("benchmark_context")
            if ctx:
                live.append((cts, ctx))
        out["live_cycles"] = len(live)
        if not live:
            out["notes"] = "저널에 benchmark_context 없음"
            return out
        sym = live[0][1]["benchmark_symbol"]
        bars = build_decision_bars(load_1m_candles(market_db, sym, table="candle"), sym)

        def parse(s):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))

        numeric = ("benchmark_close", "benchmark_ema20", "benchmark_vs_ema20",
                   "benchmark_ema20_slope", "benchmark_return_trailing_30m",
                   "benchmark_return_trailing_60m")
        exact = ("benchmark_trend", "benchmark_timeframe", "benchmark_source_bar_end")
        maxd, matched, lagged = 0.0, 0, 0
        for cts, ctx in live:
            if parse(ctx["benchmark_source_bar_end"]) < parse(ctx["benchmark_context_asof"]):
                lagged += 1
            if parse(ctx["benchmark_source_bar_end"]) > parse(ctx["benchmark_context_asof"]):
                out["invariant_violations"] += 1
            got = build_benchmark_context(
                bars, decision_time=parse(ctx["benchmark_source_bar_end"]), symbol=sym,
                timeframe="5m", ema_period=ctx["benchmark_ema_period"]).to_dict()
            bad = False
            for f in numeric:
                a, b = got[f], ctx[f]
                if a is None or b is None:
                    bad = bad or (a is not b)
                    continue
                maxd = max(maxd, abs(a - b))
                bad = bad or abs(a - b) > out["tolerance"]
            for f in exact:
                bad = bad or got[f] != ctx[f]
            if not bad:
                matched += 1
        out["matched_cycles"] = matched
        out["max_abs_diff"] = maxd
        out["status"] = ("PASS" if matched == len(live) and out["invariant_violations"] == 0
                         else "FAIL")
        out["notes"] = (
            f"{matched}/{len(live)} 사이클 전 필드 일치(오차 {maxd}). "
            f"라이브 벤치마크 봉 도착 지연 {lagged}건은 as-of 대신 라이브가 실제 본 봉으로 "
            f"정렬해 흡수했다 — 계산 차이가 아니라 데이터 도착 시점 차이다.")
    except Exception as e:      # 패리티 실패가 분석 전체를 죽이지 않게(상태로 보고)
        out["status"] = "ERROR"
        out["notes"] = f"{type(e).__name__}: {e}"
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="H3 국면 예측력 분석(read-only)")
    ap.add_argument("--joined", default="datasets/h3_regime_joined.jsonl")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--sessions", default=",".join(LIVE_SESSIONS))
    ap.add_argument("--include-discovery-date", action="store_true",
                    help="발견 표본일(2026-07-15)을 포함(기본 제외)")
    ap.add_argument("--parity-journal", default="data/journal_us_phase6_s001.sqlite")
    ap.add_argument("--parity-market", default="data/market_us_phase6_s001.sqlite")
    ap.add_argument("--out-md", default="reports/h3_regime_validation.md")
    ap.add_argument("--out-json", default="reports/h3_regime_validation.json")
    args = ap.parse_args(argv)

    joined = _repo_path(args.joined)
    sessions = tuple(s.strip().upper() for s in args.sessions.split(",") if s.strip())
    exclude = () if args.include_discovery_date else DISCOVERY_DATES

    print(f"[1/4] 관측 로드 — {joined}")
    obs, cov = load_observations(joined, sessions, exclude)
    print(f"      {cov['kept']:,d} / {cov['rows']:,d} 행 · {cov['n_dates']} 거래일")

    print("[2/4] 패리티")
    parity = run_parity(_repo_path(args.parity_journal), _repo_path(args.parity_market))
    print(f"      {parity['status']} — {parity['notes'][:90]}")

    print("[3/4] 국면 × 지평 분석")
    analysis = analyze(obs, args.alpha)

    control = lookahead_control_report(obs)
    infl = control["max_inflation_x"]
    print(f"      look-ahead 대조군: 최대 부풀림 "
          f"{'—' if infl is None else f'{infl:.1f}x'}")

    verdict = build_verdict(analysis, cov)
    print(f"      verdict = {verdict['usable_regime_signal']} "
          f"({verdict['confidence']}) · 비용 넘는 칸 {len(verdict['usable_cells'])}개")

    payload = {
        "meta": {
            "generated_at": _now_iso(), "git_sha": _git_sha(),
            "input": joined, "sessions": list(sessions),
            "excluded_dates": list(exclude),
            "cost_per_trade_pct": COST_PER_TRADE_PCT,
            "workorder": "reports/WORKORDER_H3_REGIME.md",
            "predecessor": "reports/h2b_backfill_validation.md",
            "read_only": True, "trading_path_changes": 0,
            "regime_gate_enabled": False,
        },
        "parity_test": parity,
        "coverage": cov,
        "analysis": analysis,
        "lookahead_control": control,
        "verdict": verdict,
    }

    cmd = ("python scripts/h3_regime_analysis.py "
           f"--joined {args.joined} --alpha {args.alpha}")
    md_path, js_path = _repo_path(args.out_md), _repo_path(args.out_json)
    os.makedirs(os.path.dirname(md_path), exist_ok=True)
    os.makedirs(os.path.dirname(js_path), exist_ok=True)
    with open(md_path, "w") as f:
        f.write(render_md(payload, cmd))
    with open(js_path, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"[4/4] 리포트 → {md_path}\n            {js_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
