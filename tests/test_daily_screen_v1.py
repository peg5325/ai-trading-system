"""Daily same-day screen v1 (PREREG_DAILY_SAMEDAY_SCREEN_V1 §11 required tests). Synthetic data, except the
P&L-free real-data precount check (skipped when the pinned longhist data is absent)."""
from __future__ import annotations

import copy
import json
import math
import re
import subprocess
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.daily_screen_v1 import candidates as CD
from src.daily_screen_v1 import data as D
from src.daily_screen_v1 import features as FT
from src.daily_screen_v1 import pipeline as PL
from src.daily_screen_v1 import returns as RT
from src.daily_screen_v1 import stats as ST
from src.daily_screen_v1.data import StudyRefused
from src.entry_study_v1.stats import block_bootstrap_p, holm_adjust, seed_for
from src.history.oos_guard import OOSLockError
from src.longhist import default_root
from src.longhist import fetch as F
from src.longhist.loader import load_daily, pinned_manifest_sha256

ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "docs" / "research" / "PREREG_DAILY_SAMEDAY_SCREEN_V1.md"
CONFIG = CD.load_config()
EVENTS = CD.load_events()
PRICE_COLS = ["date", "open", "high", "low", "close", "adjclose", "volume"]
SYMS = ("SPY", "QQQ", "^SOX", "SMH", "SPXL", "SPXS", "TQQQ", "SQQQ", "SOXL", "SOXS")


# ------------------------------------------------------------------ synthetic data


def bdays(start: str, end: str) -> list[str]:
    return [d.date().isoformat() for d in pd.bdate_range(start, end)]


def walk(dates: list[str], seed: int, base: float = 100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = base * np.exp(np.cumsum(rng.normal(0, 0.01, len(dates))))
    open_ = np.r_[base, close[:-1]] * np.exp(rng.normal(0, 0.004, len(dates)))
    high = np.maximum(open_, close) * (1 + rng.uniform(0.001, 0.01, len(dates)))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.001, 0.01, len(dates)))
    return pd.DataFrame({"date": dates, "open": open_, "high": high, "low": low, "close": close})


def synth_root(tmp_path: Path, start: str = "2008-01-02", end: str = "2025-07-31") -> tuple[Path, str, list[str]]:
    dates = bdays(start, end)
    man = F.Manifest(tmp_path)

    def put(rel, rows, cols):
        F.write_csv(tmp_path, rel, rows, cols)
        man.add(rel, kind="norm", source="test", source_url=None, fetched_utc="x", rows=len(rows),
                first_date=None, last_date=None)

    for i, s in enumerate(SYMS):
        df = walk(dates, i)
        df["adjclose"], df["volume"] = df["close"], 1
        put(f"norm/yahoo/{s.replace('^', 'IDX_')}_daily.csv", df.to_dict("records"), PRICE_COLS)
        divs = [{"date": dates[300], "amount": 0.5}] if s in ("SPY", "QQQ", "SMH") else []
        put(f"norm/yahoo/{s.replace('^', 'IDX_')}_dividends.csv", divs, ["date", "amount"])
    rng = np.random.default_rng(99)
    for s in ("VIX", "VIX9D", "VIX3M"):
        v = 20 + rng.normal(0, 5, len(dates))
        put(f"norm/cboe/{s}.csv", [{"date": d, "open": x, "high": x, "low": x, "close": x} for d, x in zip(dates, v)],
            ["date", "open", "high", "low", "close"])
    g = rng.normal(1e9, 2e9, len(dates))
    put("norm/squeezemetrics/DIX.csv", [{"date": d, "price": 1.0, "dix": 0.4, "gex": x} for d, x in zip(dates, g)],
        ["date", "price", "dix", "gex"])
    put("norm/calendar/nyse_sessions.csv", [{"date": d} for d in dates], ["date"])
    return tmp_path, man.save(), dates


def synth_config(tmp_path: Path, sha: str, *, cands: int | None = None) -> tuple[Path, str, dict]:
    cfg = copy.deepcopy(CONFIG)
    cfg["data"]["manifest_sha256"] = sha
    for spec in cfg["indices"].values():
        spec["start"] = "2008-01-02"
    if cands is not None:
        cfg["candidates"] = [c for c in cfg["candidates"] if c["k"] < cands]
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(cfg))
    return p, CD.sha256_file(p), cfg


THR = {"vix_t1": 16.0, "vix_t2": 22.0, "mg": 0.003, "qo": 0.011, "prng_q20": 0.007, "prng_q80": 0.019}


# ------------------------------------------------------------------ 1. look-ahead


def _conditions(px, aux, cal_dates, cflags, eflags):
    t = FT.session_table("SPY", px, {}, cal_dates, cal_dates[0], aux)
    out = {f"C{c:02d}": CD.main_condition(c, t, THR, cflags, eflags) for c in range(1, 30)}
    out.update({f"G{c:02d}": CD.gex_condition(c, t, 1e9) for c in range(1, 4)})
    return t, out


def test_conditions_use_no_future_information():
    dates = bdays("2003-01-02", "2004-12-31")
    px = walk(dates, 1)
    rng = np.random.default_rng(5)
    aux = {n: dict(zip(dates, 20 + rng.normal(0, 4, len(dates)))) for n in ("vix", "v9", "v3")}
    aux["gex"] = dict(zip(dates, rng.normal(0, 2e9, len(dates))))
    cflags = CD.calendar_flags(dates, CONFIG["unscheduled_closures"])
    eflags = CD.event_flags(dates, EVENTS)
    _, base = _conditions(px, aux, dates, cflags, eflags)
    for pos in (40, 211, 380):
        d = dates[pos]
        p2 = px.copy()
        later = p2["date"] > d
        for c in ("open", "high", "low", "close"):
            p2.loc[later, c] *= rng.uniform(0.5, 1.5, later.sum())
        on_d = p2["date"] == d
        p2.loc[on_d, ["high", "low", "close"]] = p2.loc[on_d, ["high", "low", "close"]].to_numpy() * [2.0, 0.3, 1.7]
        aux2 = {n: {k: (v * 3 + 7 if k >= d else v) for k, v in m.items()} for n, m in aux.items()}
        _, alt = _conditions(p2, aux2, dates, cflags, eflags)
        for name in base:
            a, b = base[name].iloc[pos], alt[name].iloc[pos]
            assert (pd.isna(a) and pd.isna(b)) or a == b, (name, d)


def test_thresholds_use_dev_rows_only():
    dates = bdays("2010-01-04", "2017-12-29")
    px = walk(dates, 2)
    aux = {"vix": dict(zip(dates, np.linspace(10, 40, len(dates))))}
    t = {i: FT.session_table(i, px, {}, dates, dates[0], aux) for i in ("SPY", "QQQ", "^SOX")}
    th = FT.thresholds(t, "2015-12-31")
    px2 = px.copy()
    late = px2["date"] > "2015-12-31"
    px2.loc[late, ["open", "high", "low", "close"]] *= 3
    aux2 = {"vix": {k: (99.0 if k > "2015-12-31" else v) for k, v in aux["vix"].items()}}
    t2 = {i: FT.session_table(i, px2, {}, dates, dates[0], aux2) for i in ("SPY", "QQQ", "^SOX")}
    assert FT.thresholds(t2, "2015-12-31") == th


# ------------------------------------------------------------------ 2. seal and code cut-offs


def test_stage_cutoffs_and_seal(tmp_path):
    root, sha, _ = synth_root(tmp_path)
    kw = {"root": root, "expected_manifest_sha256": sha}
    for stage, bad in (("main_dev", "2016-01-01"), ("gex_dev", "2019-01-01"), ("holdout", "2025-07-19")):
        with pytest.raises(StudyRefused):
            D.load_prices("SPY", stage, bad, **kw)
        ok = D.load_prices("SPY", stage, **kw)
        assert ok["date"].max() == D.CUTOFFS[stage].isoformat()
    with pytest.raises(StudyRefused):
        D.check_window("main_dev", None)
    with pytest.raises(StudyRefused):
        D.check_window("swing", "2010-01-01")
    # calendar/dividends are not cut off
    assert max(D.load_calendar(**kw)) == "2025-07-31"
    with pytest.raises(OOSLockError):
        load_daily("SPY", None, "2025-07-21", purpose="performance", **kw)
    assert D.holdout_dates("SPY", "main", **kw)[-1] == "2025-07-18"


def test_holdout_refused_without_addendum(tmp_path):
    root, sha, _ = synth_root(tmp_path / "d")
    cfg_path, cfg_sha, _ = synth_config(tmp_path, sha)
    pc = tmp_path / "pc.json"
    pc.write_text(json.dumps({"family": "main", "reproduction": {"config_sha256": cfg_sha}}))
    dev = tmp_path / "dev.json"
    dev.write_text(json.dumps({"family": "main", "inputs": {"precount_sha256": CD.sha256_file(pc)},
                               "reproduction": {"config_sha256": cfg_sha}, "verdict": "SELECTED", "selected": [0]}))
    kw = {"config_path": cfg_path, "config_sha256": cfg_sha, "precount_path": pc, "precount_sha256": CD.sha256_file(pc),
              "dev_result_path": dev, "dev_result_sha256": CD.sha256_file(dev), "out_dir": tmp_path / "out",
              "allow_dirty": True, "root": root, "expected_manifest_sha256": sha}
    with pytest.raises(StudyRefused, match="approval"):
        PL.holdout_step("main", addendum_commit="HEAD", approved=False, **kw)
    with pytest.raises(StudyRefused, match="addendum"):
        PL.holdout_step("main", addendum_commit=None, approved=True, **kw)
    with pytest.raises(StudyRefused, match="not in commit"):  # the addendum file does not exist on this branch
        PL.holdout_step("main", addendum_commit="HEAD", approved=True, **kw)


# ------------------------------------------------------------------ 3. costs and returns


def test_cost_math():
    assert round(-float(RT.net(0.0, "base")) * 100, 4) == 0.2021
    assert round(-float(RT.net(0.0, "stress")) * 100, 4) == 0.3021
    assert round(-float(RT.net(0.0, "sensitivity")) * 100, 4) == 0.1021
    f = 0.0000206
    for r, c, cost in ((0.03, 0.001, "base"), (-0.05, 0.0015, "stress"), (0.10, 0.0005, "sensitivity")):
        assert float(RT.net(r, cost)) == pytest.approx((1 + r) * (1 - c - f) - (1 + c), abs=1e-15)
    assert float(RT.net(0.03, "base")) == pytest.approx(1.03 * 0.9989794 - 1.001, abs=1e-12)
    # R* below -1 is clipped
    assert float(RT.r_star(-0.4, 0.0, "long")) == -1.0
    assert float(RT.r_star(0.4, 0.0, "short")) == -1.0
    assert float(RT.simple_3x(-0.4, "long")) == -1.0


def test_r_star_formulas():
    i, g = 0.01, 0.02
    assert float(RT.r_star(i, g, "long")) == pytest.approx(3 * 1.02 * 0.01 / 1.06)
    assert float(RT.r_star(i, g, "short")) == pytest.approx(-3 * 1.02 * 0.01 / 0.94)
    assert float(RT.r_star(-0.02, -0.01, "long")) == pytest.approx(3 * 0.99 * -0.02 / 0.97)
    assert float(RT.r_star(-0.02, -0.01, "short")) == pytest.approx(-3 * 0.99 * -0.02 / 1.03)
    for side, sign in (("long", 3), ("short", -3)):
        assert float(RT.r_star(0.013, 0.0, side)) == pytest.approx(sign * 0.013)
        assert float(RT.simple_3x(0.013, side)) == pytest.approx(sign * 0.013)
    # R* uses the dividend-adjusted g: prev close 100, dividend 1, open 99 -> g = 0 -> R* = 3i
    df = pd.DataFrame({"date": ["2020-01-02", "2020-01-03"], "open": [100.0, 99.0], "high": [101, 100.0],
                       "low": [99, 98.0], "close": [100.0, 99.99]})
    g = FT.gaps(df, {"2020-01-03": 1.0})
    assert g.iloc[1] == 0.0
    assert float(RT.r_star(99.99 / 99 - 1, g.iloc[1], "long")) == pytest.approx(3 * (99.99 / 99 - 1))


def test_cost_constants_match_config():
    assert CONFIG["costs"]["one_way_commission"] == RT.COSTS
    assert CONFIG["costs"]["sec_fee_sell"] == RT.SEC_FEE


# ------------------------------------------------------------------ 4. candidate list vs the document


def test_candidates_match_document():
    doc = PREREG.read_text(encoding="utf-8")
    rows = re.findall(r"^\| (C\d\d) \| .+? \| (F\d) \|", doc, flags=re.MULTILINE)
    rows += [(f"C{n}", "F3") for n in (17, 18, 19, 20, 21)]
    doc_conds = dict(rows)
    assert sorted(doc_conds) == [f"C{i:02d}" for i in range(1, 30)]
    assert [(c["id"], c["family"]) for c in CONFIG["conditions"]] == sorted(doc_conds.items())
    cands = CONFIG["candidates"]
    assert len(cands) == 174 and [c["k"] for c in cands] == list(range(174))
    etf = {"SPY": ("SPXL", "SPXS"), "QQQ": ("TQQQ", "SQQQ"), "^SOX": ("SOXL", "SOXS")}
    for c in cands:
        n = int(c["condition"][1:])
        assert c["k"] == 58 * ("SPY", "QQQ", "^SOX").index(c["index"]) + 2 * (n - 1) + ("long", "short").index(c["side"])
        assert c["etf"] == etf[c["index"]][("long", "short").index(c["side"])]
        assert c["family"] == doc_conds[c["condition"]]
    g = CONFIG["gex_candidates"]
    assert len(g) == 18 and [c["kg"] for c in g] == list(range(18))
    for c in g:
        n = int(c["condition"][1:])
        assert c["kg"] == 6 * (n - 1) + 2 * ("SPY", "QQQ", "^SOX").index(c["index"]) + ("long", "short").index(c["side"])
    pre = CONFIG["prefilter"]
    assert sorted(map(tuple, pre["expected_prefiltered"])) == sorted(
        [("SPY", "C25"), ("QQQ", "C25")] + [("^SOX", f"C{i}") for i in range(23, 30)])
    assert pre["expected_k"] == 29 * 3 * 2 - 9 * 2 == 156
    assert "K = 29 × 3 × 2 − 9 × 2 = 156" in doc and "`K_G = 3 × 3 × 2 = 18`" in doc


def test_config_thresholds_round_to_document():
    th = CONFIG["thresholds"]
    doc = th["doc_rounded_percent"]
    assert round(th["vix_t1"], 2) == doc["vix_t1"] and round(th["vix_t2"], 2) == doc["vix_t2"]
    for index, vals in th["per_index"].items():
        for key in ("mg", "qo", "prng_q20", "prng_q80"):
            assert round(vals[key] * 100, 3) == doc[index][key], (index, key)
    text = PREREG.read_text(encoding="utf-8")
    assert "| VIX 3분위 `t1 / t2` (공통) | SPY DEV 적격 세션의 `VIX_p` (n = 5,139) | 16.00 / 22.26 |" in text
    assert "| `|g|` 중앙값 `mg` (배당 보정 g) | 지수별 DEV 적격 세션 | 0.289% | 0.394% | 0.400% |" in text
    assert "| `|poc|` 80% 분위 `qo` | 같음 | 1.109% | 1.684% | 1.661% |" in text
    assert "| `prng` 20% / 80% 분위 | 같음 | 0.715 / 1.923% | 0.963 / 3.034% | 1.077 / 2.657% |" in text
    assert th["vix_n"] == 5139
    assert {i: s["start"] for i, s in CONFIG["indices"].items()} == {
        "SPY": "1995-01-03", "QQQ": "1999-03-10", "^SOX": "2009-01-02"}
    assert CONFIG["half_split"] == {
        "SPY": {"default": "2005-08-17", "C06": "2012-11-07", "C05": "2013-07-08"},
        "QQQ": {"default": "2007-08-17", "C06": "2012-11-06", "C05": "2013-07-05"},
        "^SOX": {"default": "2012-06-27", "C06": "2012-11-09", "C05": "2013-07-12"}}


def _real_data_ok() -> bool:
    try:
        return F.sha256_file(default_root() / "manifest.json") == pinned_manifest_sha256()
    except OSError:
        return False


@pytest.mark.skipif(not _real_data_ok(), reason="pinned longhist data not present")
def test_real_precount_matches_config_and_document():
    """P&L-free: recomputes start dates, thresholds, split dates and the prefilter (K = 156) from DEV data."""
    ctx = PL.load_context(CONFIG, "main_dev", gex=False, with_etf=False)
    smh = (D.load_prices("SMH", "main_dev"), D.load_dividends("SMH"))
    pc = PL.precount_main(CONFIG, ctx, smh, {i: [] for i in CONFIG["indices"]})
    assert pc["k_active"] == 156 and pc["k_total"] == 174
    n = {(c["index"], c["condition"]): c["n"] for c in pc["cells"]}
    assert (n[("SPY", "C01")], n[("QQQ", "C01")], n[("^SOX", "C01")]) == (5142, 4089, 1632)
    assert (n[("SPY", "C06")], n[("QQQ", "C06")], n[("^SOX", "C06")]) == (121, 119, 117)
    assert {i: v["exclusions"]["any"] for i, v in pc["per_index"].items()} == {"SPY": 144, "QQQ": 140, "^SOX": 128}
    assert all("mean" not in json.dumps(c) for c in pc["cells"])


# ------------------------------------------------------------------ 5. calendar and event boundaries


def test_calendar_and_event_boundaries():
    holidays = {"2003-04-18", "2004-06-11", "2007-01-01", "2007-01-02", "2006-12-25", "2003-09-01",
                "2003-12-25", "2004-01-01", "2004-01-19", "2004-02-16", "2004-04-09", "2004-05-31"}
    cal = [d for d in bdays("2003-01-02", "2007-02-28") if d not in holidays]
    flags = CD.calendar_flags(cal, CONFIG["unscheduled_closures"])
    assert CD.third_friday(2003, 4) == date(2003, 4, 18)
    assert flags.loc["2003-04-17", "opex"] and not flags.loc["2003-04-16", "opex"]  # Good Friday -> session before
    assert flags.loc["2003-05-16", "opex"]
    assert not flags.loc["2004-06-10", "holiday_eve"]  # only an unscheduled closure follows
    assert flags.loc["2006-12-29", "holiday_eve"]  # 2007-01-01 is a scheduled holiday
    assert flags.loc["2003-04-17", "holiday_eve"] and not flags.loc["2003-04-16", "holiday_eve"]
    assert not flags.loc["2003-01-03", "holiday_eve"]  # a weekend is not a closure
    assert flags.loc["2003-03-31", "quarter_end"] and flags.loc["2003-03-31", "month_edge"]
    assert flags.loc["2003-04-03", "month_edge"] and not flags.loc["2003-04-04", "month_edge"]
    ev = CD.event_flags(cal, EVENTS)
    assert pd.isna(ev.loc["2003-09-15", "fomc"]) and ev.loc["2003-09-16", "fomc"]
    assert pd.isna(ev.loc["2003-09-12", "fomc_eve"]) and ev.loc["2003-09-15", "fomc_eve"]
    assert min(EVENTS["FOMC"]) >= "1994-02-04"
    assert "1993-12-21" not in EVENTS["FOMC"]
    early = CD.event_flags(bdays("1993-12-01", "1994-02-03"), EVENTS)
    assert not early["fomc"].any()
    assert CD.event_flags(["1994-02-03", "1994-02-04"], EVENTS)["fomc"].tolist() == [False, True]
    # unresolved CPI/NFP months: every session in that month is unknown (<NA>), never "no event"
    cpi_month = EVENTS["CPI_unknown_months"][0]
    cal2 = bdays(f"{cpi_month}-01", f"{cpi_month}-28")
    ev2 = CD.event_flags(cal2, EVENTS)
    assert ev2["cpi"].isna().all()
    # an event on a non-session (holiday) day is ignored: it has no row and no neighbouring row is set
    fomc_hol = CD.event_flags([d for d in bdays("2004-01-26", "2004-01-30") if d != "2004-01-28"], EVENTS)
    assert not fomc_hol["fomc"].fillna(False).any() and not fomc_hol["fomc_eve"].fillna(False).any()


# ------------------------------------------------------------------ 6. feature boundaries, exclusions, start rule


def _table(rows, index="SPY", divs=None, cal=None, aux=None):
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close"])
    cal = cal or list(df["date"])
    return FT.session_table(index, df, divs or {}, cal, cal[0], aux)


def test_gap_poc_boundaries():
    dates = bdays("2020-01-06", "2020-01-10")
    thr = dict(THR, mg=0.01)
    rows = [(dates[0], 100, 101, 99, 100), (dates[1], 100, 101, 99, 100.5),     # g = 0
            (dates[2], 101.505, 102, 101, 101.505),                               # g = +1% = mg
            (dates[3], 101.505 * 0.99, 101, 100, 100.5),                          # poc = 0
            (dates[4], 100.5, 101, 100, 100.6)]
    t = _table(rows)
    cal = CD.calendar_flags(dates, [])
    ev = CD.event_flags(dates, EVENTS)
    c = {n: CD.main_condition(n, t, thr, cal, ev) for n in range(1, 30)}
    assert t.loc[1, "g"] == 0 and not any(bool(c[n][1]) for n in (7, 8, 9, 10))
    assert t.loc[2, "g"] == pytest.approx(0.01)
    t.loc[2, "g"] = 0.01  # exactly mg
    c7, c8 = CD.main_condition(7, t, thr, cal, ev), CD.main_condition(8, t, thr, cal, ev)
    assert bool(c7[2]) and not bool(c8[2])
    t.loc[3, "g"] = -0.01
    assert bool(CD.main_condition(10, t, thr, cal, ev)[3]) and not bool(CD.main_condition(9, t, thr, cal, ev)[3])
    assert t.loc[3, "poc"] == 0 and not bool(c[11][3]) and not bool(c[12][3])
    # VIX missing -> undefined (no trade, counted)
    t2 = _table(rows, aux={"vix": {d: 15.0 for d in dates if d != dates[2]}, "v9": {}, "v3": {}})
    v = CD.main_condition(2, t2, thr, cal, ev)
    assert pd.isna(v[3]) and bool(v[2]) and int(v.isna().sum()) == 2  # row 0 has no p(d) value either


def test_exclusions_on_d_and_previous_row():
    dates = bdays("2020-02-03", "2020-02-21")
    base = [(d, 100 + i, 101 + i, 99 + i, 100.5 + i) for i, d in enumerate(dates)]
    rows = [list(r) for r in base]
    rows[3][1] = rows[2][4]                         # (a) open = previous close on dates[3]
    rows[6] = [dates[6], 110, 110, 110, 110]        # (b) flat bar
    rows[11][1] = rows[10][4] * 1.2                 # (d) |g| > 15%
    cal = list(dates)
    gap_rows = [r for i, r in enumerate(rows) if i != 8]  # (c) dates[8] missing from the data
    t = _table(gap_rows, cal=cal).set_index("date")
    assert t.loc[dates[3], "a_open_eq_prev_close"] and t.loc[dates[4], "a_open_eq_prev_close"]
    assert t.loc[dates[6], "b_flat_bar"] and t.loc[dates[7], "b_flat_bar"]
    assert t.loc[dates[9], "c_data_gap"] and t.loc[dates[10], "c_data_gap"]
    assert t.loc[dates[11], "d_gap_gt_15pct"] and t.loc[dates[12], "d_gap_gt_15pct"]
    assert not t.loc[dates[13], "excluded"] and not t["e_sox_tiny_gap"].any()
    # first LOOKBACK_SKIP sessions with a gap value are skipped
    assert list(t["in_range"])[:4] == [False, False, False, True]
    # (e) ^SOX only: |g| < 0.02%
    sox = [list(r) for r in base]
    sox[5][1] = sox[4][4] * 1.0001
    ts = _table(sox, index="^SOX").set_index("date")
    assert ts.loc[dates[5], "e_sox_tiny_gap"] and ts.loc[dates[6], "e_sox_tiny_gap"]
    assert not _table(sox, index="SPY")["e_sox_tiny_gap"].any()


def _year_frame(spec: list[tuple[int, int, int]]) -> pd.DataFrame:
    """spec: (year, rows, rows with g == 0). One extra first row in the first year (excluded from stats)."""
    out, price = [], 100.0
    for j, (y, n, zeros) in enumerate(spec):
        days = bdays(f"{y}-01-02", f"{y}-12-31")[: n + (1 if j == 0 else 0)]
        for i, d in enumerate(days):
            idx = i - (1 if j == 0 else 0)
            o = price if 0 <= idx < zeros else price * 1.003
            price = o * 1.001
            out.append((d, o, max(o, price) * 1.001, min(o, price) * 0.999, price))
    return pd.DataFrame(out, columns=["date", "open", "high", "low", "close"])


def test_start_rule_boundaries():
    df = _year_frame([(2001, 100, 30), (2002, 252, 12)])
    ys = FT.year_stats(df, {})
    assert ys.loc[2002, "zero_ratio"] == pytest.approx(0.047619, abs=1e-6)
    assert FT.start_date("SPY", df, {}, 2002) == df[df["date"] >= "2002"]["date"].iloc[0]
    exact = _year_frame([(2001, 100, 30), (2002, 20, 1)])
    assert FT.year_stats(exact, {}).loc[2002, "zero_ratio"] == 0.05
    assert FT.start_date("SPY", exact, {}, 2002) is None  # 0.05 exactly fails (strict <)
    # ^SOX: tiny-gap share < 10% and median |g| >= 50% of SMH's that year
    sox = _year_frame([(2001, 100, 0), (2002, 100, 0)])
    smh_hi = pd.DataFrame({"median_abs_g": [0.001, 0.01]}, index=[2001, 2002])
    smh_lo = pd.DataFrame({"median_abs_g": [0.001, 0.001]}, index=[2001, 2002])
    assert FT.start_date("^SOX", sox, {}, 2002, smh_lo) == sox["date"].iloc[0]
    assert FT.start_date("^SOX", sox, {}, 2002, smh_hi) is None
    assert FT.start_date("^SOX", sox, {}, 2002, None) == sox["date"].iloc[0]  # no SMH that year -> ratio rules only


# ------------------------------------------------------------------ 7. statistics


def test_bh_by_holm_hand_examples():
    p = {0: 0.001, 1: 0.006, 2: 0.039, 3: 0.041, 4: 0.042, 5: 0.065, 6: 0.074, 7: 0.205, 8: None, 9: 0.6}
    # BH q=0.10, K=10: thresholds i*0.01 -> p_(5)=0.042 <= 0.05 is the largest (step-up past 0.039 > 0.03)
    assert ST.bh_reject(p) == {0, 1, 2, 3, 4}
    # BY: q / H_10 (H_10 = 2.928968...) -> thresholds i*0.003414 -> only p_(1), p_(2)
    assert ST.by_reject(p) == {0, 1}
    # an exact boundary p_(i) = i q / K is rejected
    assert ST.bh_reject({0: 0.05, 1: 0.5}) == {0} and ST.bh_reject({0: 0.06, 1: 0.1}) == {0, 1}
    # ties broken by candidate number; None counts as 1
    assert ST.bh_reject({3: 0.02, 1: 0.02, 2: None}) == {1, 3}
    assert ST.bh_reject({0: None, 1: None}) == set()
    adj = holm_adjust({0: 0.01, 1: 0.04, 2: 0.03, 3: None}, 4)
    assert adj == {0: 0.04, 2: pytest.approx(0.09), 1: pytest.approx(0.09), 3: 1.0}


def test_bootstrap_determinism_and_rerun():
    rng = np.random.default_rng(3)
    x = rng.normal(0.0, 0.02, 300)
    a, b = ST.bootstrap_p(x, 0, 7), ST.bootstrap_p(x, 0, 7)
    assert a == b and a["B"] == 10_000 and a["seed"] == seed_for(0, 7) == 20261012 + 7 and not a["rerun"]
    assert a["p"] == block_bootstrap_p(x, ["all"] * 300, seed_for(0, 7), 10_000)
    assert ST.exceed_count(a["p"], 10_000) > 50
    # strongly positive mean -> e <= 50 -> rerun with B = 200,000 and variant + 4
    y = rng.normal(0.01, 0.01, 40)
    r = ST.bootstrap_p(y, 0, 3)
    assert r["rerun"] and r["B"] == 200_000 and r["seed"] == seed_for(4, 3) == 20261012 + 4000 + 3
    assert r["p"] == block_bootstrap_p(y, ["all"] * 40, seed_for(4, 3), 200_000)
    assert ST.exceed_count(block_bootstrap_p(y, ["all"] * 40, seed_for(0, 3), 10_000), 10_000) <= 50
    # zero variance / n < 2 -> None
    assert ST.bootstrap_p([0.01] * 30, 0, 1)["p"] is None
    assert ST.bootstrap_p([0.01], 0, 1)["p"] is None
    assert ST.bootstrap_p([], 0, 1)["p"] is None
    # 2 <= n <= 5 uses the same draws as the single-segment bootstrap
    small = ST.bootstrap_p([0.01, -0.02, 0.005], 0, 1, b_count=1000)
    assert small["p"] is not None and 0 < small["p"] <= 1


def test_survivor_rank_and_t():
    assert ST.t_stat([1.0, 3.0]) == pytest.approx(2 / (math.sqrt(2) / math.sqrt(2)))
    ok = {"bh_rejected": True, "n": 100, "stress_mean": 0.001, "half_means": (0.001, 0.002), "is_sox": False,
              "sox_etf_mean": None}
    assert ST.dev_survivor(**ok)["survivor"]
    for k, v in (("bh_rejected", False), ("n", 99), ("stress_mean", 0.0), ("half_means", (0.001, 0.0)),
                 ("half_means", (None, 0.1))):
        assert not ST.dev_survivor(**{**ok, k: v})["survivor"], k
    ranked = ST.rank_survivors([{"k": 5, "stress_t": 2.0, "n": 100}, {"k": 1, "stress_t": 2.0, "n": 100},
                                {"k": 9, "stress_t": 2.0, "n": 300}, {"k": 2, "stress_t": 3.0, "n": 10},
                                {"k": 0, "stress_t": None, "n": 500}], 4)
    assert ranked == [2, 9, 1, 5]


# ------------------------------------------------------------------ 8. data binding, write-once


def test_binding_and_write_once(tmp_path):
    root, sha, _ = synth_root(tmp_path / "d")
    cfg_path, cfg_sha, cfg = synth_config(tmp_path, sha)
    kw = {"out_dir": tmp_path / "out", "allow_dirty": True, "root": root, "expected_manifest_sha256": sha}
    with pytest.raises(StudyRefused, match="config"):
        PL.precount_step("main", config_path=cfg_path, config_sha256="0" * 64, **kw)
    with pytest.raises(StudyRefused, match="required"):
        PL.precount_step("main", config_path=cfg_path, config_sha256=None, **kw)
    bad = copy.deepcopy(cfg)
    bad["data"]["manifest_sha256"] = "1" * 64
    bp = tmp_path / "bad.json"
    bp.write_text(json.dumps(bad))
    with pytest.raises(StudyRefused, match="manifest"):
        PL.precount_step("main", config_path=bp, config_sha256=CD.sha256_file(bp), **kw)
    bad["data"]["manifest_sha256"] = sha
    bad["data"]["events_yaml_sha256"] = "2" * 64
    bp.write_text(json.dumps(bad))
    with pytest.raises(StudyRefused, match="events yaml"):
        PL.precount_step("main", config_path=bp, config_sha256=CD.sha256_file(bp), **kw)
    with pytest.raises(StudyRefused, match="manifest"):  # real pinned SHA vs this synthetic manifest
        PL.check_binding(cfg)
    # synthetic prices give other start dates/thresholds than the fixed config -> precount stops
    with pytest.raises(StudyRefused, match="differs from the fixed config"):
        PL.precount_step("main", config_path=cfg_path, config_sha256=cfg_sha, **kw)
    out = tmp_path / "out" / "main" / "precount.json"
    out.parent.mkdir(parents=True)
    out.write_text("{}")
    with pytest.raises(StudyRefused, match="write-once"):
        PL.precount_step("main", config_path=cfg_path, config_sha256=cfg_sha, **kw)
    with pytest.raises(StudyRefused, match="write-once"):
        PL.write_result(out, {"a": 1})


# ------------------------------------------------------------------ 9. run order for the GEX family


def _git_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*a):
        return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "t")
    (repo / "README").write_text("x")
    g("add", ".")
    g("commit", "-qm", "init")
    g("checkout", "-qb", "side")
    p = repo / PL.ADDENDUM["main"]
    p.parent.mkdir(parents=True)
    p.write_text("addendum")
    g("add", ".")
    g("commit", "-qm", "addendum")
    g("rev-parse", "HEAD")
    g("checkout", "-q", "main")
    g("merge", "-q", "--no-ff", "side", "-m", "merge")
    merged = g("rev-parse", "HEAD")
    g("checkout", "-qb", "other", "HEAD~1")
    g("commit", "-q", "--allow-empty", "-m", "unmerged")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("addendum2")
    g("add", ".")
    g("commit", "-qm", "unmerged addendum")
    return repo, merged, g("rev-parse", "HEAD")


def test_gex_run_order(tmp_path):
    repo, merged, unmerged = _git_repo(tmp_path)
    res = tmp_path / "dev_result.json"
    res.write_text(json.dumps({"family": "main", "verdict": "SELECTED", "selected": [3]}))
    sha = CD.sha256_file(res)
    kw = {"repo": repo, "main_ref": "main"}
    with pytest.raises(StudyRefused, match="needs the main dev_result"):
        PL.require_main_done(None, None, None, **kw)
    with pytest.raises(StudyRefused, match="hashes to"):
        PL.require_main_done(res, "0" * 64, merged, **kw)
    with pytest.raises(StudyRefused, match="required"):
        PL.require_main_done(res, None, merged, **kw)
    with pytest.raises(StudyRefused, match="addendum commit is required"):
        PL.require_main_done(res, sha, None, **kw)
    with pytest.raises(StudyRefused, match="not an ancestor"):
        PL.require_main_done(res, sha, unmerged, **kw)
    with pytest.raises(StudyRefused, match="is not in commit"):
        PL.require_main_done(res, sha, merged + "~1", **kw)
    assert PL.require_main_done(res, sha, merged, **kw).startswith("main addendum merged")
    nopass = tmp_path / "nopass.json"
    nopass.write_text(json.dumps({"family": "main", "verdict": "NO_PASS", "selected": []}))
    assert PL.require_main_done(nopass, CD.sha256_file(nopass), None, **kw) == "main NO_PASS"
    gexres = tmp_path / "gexres.json"
    gexres.write_text(json.dumps({"family": "gex", "verdict": "NO_PASS"}))
    with pytest.raises(StudyRefused, match="not the main"):
        PL.require_main_done(gexres, CD.sha256_file(gexres), None, **kw)


def test_gex_precount_and_dev_refuse_before_reading_prices(tmp_path, monkeypatch):
    root, sha, _ = synth_root(tmp_path / "d")
    cfg_path, cfg_sha, _ = synth_config(tmp_path, sha)
    monkeypatch.setattr(PL, "load_context", lambda *a, **k: pytest.fail("prices read before the order check"))
    kw = {"config_path": cfg_path, "config_sha256": cfg_sha, "out_dir": tmp_path / "out", "allow_dirty": True, "root": root,
              "expected_manifest_sha256": sha}
    with pytest.raises(StudyRefused, match="needs the main dev_result"):
        PL.precount_step("gex", **kw)
    pc = tmp_path / "pc.json"
    pc.write_text(json.dumps({"family": "gex", "reproduction": {"config_sha256": cfg_sha}}))
    with pytest.raises(StudyRefused, match="approval"):
        PL.dev_step("gex", precount_path=pc, precount_sha256=CD.sha256_file(pc), approved=False, **kw)
    with pytest.raises(StudyRefused, match="needs the main dev_result"):
        PL.dev_step("gex", precount_path=pc, precount_sha256=CD.sha256_file(pc), approved=True, **kw)
    with pytest.raises(StudyRefused, match="another family"):
        PL.dev_step("main", precount_path=pc, precount_sha256=CD.sha256_file(pc), approved=True, **kw)


# ------------------------------------------------------------------ 10. actual-ETF conditions


def test_actual_etf_conditions():
    base = {"bh_rejected": True, "n": 150, "stress_mean": 0.002, "half_means": (0.001, 0.003), "is_sox": True}
    assert ST.dev_survivor(**base, sox_etf_mean=0.0005)["survivor"]
    for v in (0.0, -0.001, None):
        r = ST.dev_survivor(**base, sox_etf_mean=v)
        assert not r["survivor"] and not r["conditions"]["5_sox_actual_etf_mean_gt_0"]
    assert "5_sox_actual_etf_mean_gt_0" not in ST.dev_survivor(**{**base, "is_sox": False},
                                                              sox_etf_mean=None)["conditions"]
    ok = ST.holdout_verdict(n=60, holm_adj=0.01, stress_mean=0.001, etf_mean=0.002)
    assert ok["verdict"] == "HOLDOUT_PASS"
    fid = ST.holdout_verdict(n=60, holm_adj=0.01, stress_mean=0.001, etf_mean=0.0)
    assert (fid["verdict"], fid["reason"]) == ("HOLDOUT_FAIL", "FIDELITY")
    assert ST.holdout_verdict(n=60, holm_adj=0.05, stress_mean=0.001, etf_mean=0.002)["verdict"] == "HOLDOUT_FAIL"
    assert ST.holdout_verdict(n=49, holm_adj=0.01, stress_mean=0.001, etf_mean=0.002)["verdict"] == \
        "HOLDOUT_INCONCLUSIVE"
    assert ST.holdout_verdict(n=60, holm_adj=0.01, stress_mean=0.001, etf_mean=None)["reason"] == "FIDELITY"


def test_sox_etf_mean_uses_trades_from_2010_03_11(tmp_path):
    """Hand example through run_dev: the ^SOX ⑤ mean covers actual-ETF trade days on/after 2010-03-11 only."""
    root, sha, dates = synth_root(tmp_path / "d", start="2009-06-01", end="2016-01-29")
    _, _, cfg = synth_config(tmp_path, sha)
    cfg["candidates"] = [c for c in cfg["candidates"] if c["index"] == "^SOX" and c["condition"] == "C01"]
    ctx = PL.load_context(cfg, "main_dev", gex=False, with_etf=True, root=root, expected_manifest_sha256=sha)
    # SOXL "ETF" return is +1% before 2010-03-11 and -1% from then on
    ctx["etf"]["SOXL"] = {d: (0.01 if d < "2010-03-11" else -0.01) for d in dates}
    res = PL.run_dev(cfg, ctx, "main", {"prefiltered": [], "family": "main"})
    row = next(r for r in res["candidates"] if r["side"] == "long")
    assert row["sox_etf_mean_from_2010_03_11"] == pytest.approx(float(RT.net(-0.01, "base")))
    assert row["actual_etf"]["n"] == row["n"]
    assert row["dev"]["conditions"]["5_sox_actual_etf_mean_gt_0"] is False
    assert res["verdict"] in ("SELECTED", "NO_PASS") and res["k_active"] == 2


def test_holdout_run_end_to_end_on_synthetic_data(tmp_path):
    root, sha, _ = synth_root(tmp_path / "d", start="2015-06-01")
    _, _, cfg = synth_config(tmp_path, sha)
    ctx = PL.load_context(cfg, "holdout", gex=False, with_etf=True, root=root, expected_manifest_sha256=sha)
    assert max(t["date"].max() for t in ctx["tables"].values()) == "2025-07-18"
    dev = {"family": "main", "verdict": "SELECTED", "selected": [32, 0]}
    res = PL.run_holdout(cfg, ctx, "main", {}, dev)
    assert res["m"] == 2 and [c["k"] for c in res["candidates"]] == [32, 0]
    for c in res["candidates"]:
        assert c["bootstrap"]["seed"] in (seed_for(50, c["k"]), seed_for(54, c["k"]))
        assert c["holdout"]["verdict"].startswith("HOLDOUT_")
        assert c["base"]["n"] == c["n"] and min(c["actual_etf"]["n"], c["n"]) == c["actual_etf"]["n"]
    with pytest.raises(StudyRefused, match="NO_PASS"):
        PL.run_holdout(cfg, ctx, "main", {}, {"family": "main", "verdict": "NO_PASS", "selected": []})
