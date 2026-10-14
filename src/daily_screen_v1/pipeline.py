"""Run steps (PREREG §6-§8, §11): fixed-input and run-order checks, precount (no P&L), DEV, HOLDOUT.

Every step refuses unless the config SHA matches, the config's data binding (manifest + events yaml SHA) holds,
and the output file does not exist yet. Outputs are write-once JSON with their SHA-256 printed by the scripts.
"""
from __future__ import annotations

import json
import math
import platform
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

from src.daily_screen_v1 import candidates as CD
from src.daily_screen_v1 import data as D
from src.daily_screen_v1 import features as FT
from src.daily_screen_v1 import returns as RT
from src.daily_screen_v1 import stats as ST
from src.daily_screen_v1.data import StudyRefused
from src.entry_study_v1.mask import canonical_bytes, write_once
from src.entry_study_v1.stats import holm_adjust, power_delta
from src.longhist.loader import pinned_manifest_sha256
from src.output_layout import research_dir

ROOT = CD.ROOT
OUT_DIR = research_dir(ROOT, "daily_screen_v1")
FAMILIES = ("main", "gex")
ADDENDUM = {"main": "docs/research/PREREG_DAILY_SAMEDAY_SCREEN_V1_HOLDOUT_ADDENDUM.md",
            "gex": "docs/research/PREREG_DAILY_SAMEDAY_SCREEN_V1_GEX_ADDENDUM.md"}
USD_NOTIONAL = 700
COST_SETS = ("base", "stress", "sensitivity")


# ------------------------------------------------------------------ fixed inputs and run order


def check_sha(path: Path, expected: str | None, what: str) -> None:
    if not expected:
        raise StudyRefused(f"refusing: the {what} SHA-256 argument is required")
    if not Path(path).exists():
        raise StudyRefused(f"refusing: {what} {path} does not exist")
    got = CD.sha256_file(path)
    if got != expected:
        raise StudyRefused(f"refusing: {what} {path} hashes to {got}, expected {expected}")


def check_binding(config: dict, *, events_path: Path = CD.EVENTS_PATH,
                  expected_manifest_sha256: str | None = None) -> None:
    """§5.1 / test 8: the config's manifest SHA must be the pinned one and the events yaml must hash as recorded."""
    pinned = expected_manifest_sha256 or pinned_manifest_sha256()
    if config["data"]["manifest_sha256"] != pinned:
        raise StudyRefused("refusing: config manifest SHA-256 differs from the pinned longhist manifest")
    if CD.sha256_file(events_path) != config["data"]["events_yaml_sha256"]:
        raise StudyRefused("refusing: events yaml SHA-256 differs from the config")


def git(*args: str, repo: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)


MAIN_REF = "origin/main"  # ancestry is checked against the fetched remote main (LOW d)
GEX_SKIP_MARKER = "skipped_by_user.json"  # <out>/gex/: the user chose not to run the GEX family (§12 step 5)


def addendum_merged(commit: str | None, family: str, *, repo: Path = ROOT,
                    main_ref: str = MAIN_REF) -> tuple[str, dict]:
    """The addendum file exists in `commit` and `commit` is an ancestor of `main_ref` (fetched first when it is
    a remote ref). Returns (file text, {"commit", "ref", "ref_head"})."""
    if not commit:
        raise StudyRefused(f"refusing: the merged {family} addendum commit is required")
    shown = git("show", f"{commit}:{ADDENDUM[family]}", repo=repo)
    if shown.returncode != 0:
        raise StudyRefused(f"refusing: {ADDENDUM[family]} is not in commit {commit}")
    if "/" in main_ref:
        remote, branch = main_ref.split("/", 1)
        if git("fetch", "-q", remote, branch, repo=repo).returncode != 0:
            raise StudyRefused(f"refusing: could not fetch {main_ref} to check the addendum merge")
    if git("merge-base", "--is-ancestor", commit, main_ref, repo=repo).returncode != 0:
        raise StudyRefused(f"refusing: commit {commit} is not an ancestor of {main_ref}")
    head = git("rev-parse", main_ref, repo=repo).stdout.strip()
    return shown.stdout, {"commit": commit, "ref": main_ref, "ref_head": head}


def selected_in_addendum(text: str) -> list[int]:
    """The addendum's machine line `selected: [k, ...]` (JSON list of candidate numbers)."""
    lines = [ln for ln in text.splitlines() if ln.strip().startswith("selected:")]
    if len(lines) != 1:
        raise StudyRefused("refusing: the addendum needs exactly one line `selected: [k, ...]`")
    try:
        ks = json.loads(lines[0].split(":", 1)[1])
    except json.JSONDecodeError as exc:
        raise StudyRefused("refusing: the addendum `selected:` line is not a JSON list") from exc
    if not isinstance(ks, list) or not all(isinstance(k, int) for k in ks):
        raise StudyRefused("refusing: the addendum `selected:` line is not a list of candidate numbers")
    return ks


def require_main_done(main_dev_result: Path | None, main_dev_result_sha256: str | None,
                      main_addendum_commit: str | None, *, config_sha256: str | None, out_dir: Path = OUT_DIR,
                      repo: Path = ROOT, main_ref: str = MAIN_REF) -> dict:
    """§3 run order for `--family gex` precount/dev: no main HOLDOUT result and no GEX-skip marker exist yet,
    the main dev_result SHA and its config SHA match, and the main result is NO_PASS or the main addendum
    (recording that dev_result SHA) is merged into main. Returns which condition held."""
    for p in (Path(out_dir) / "main" / "holdout_result.json", Path(out_dir) / "gex" / GEX_SKIP_MARKER):
        if p.exists():
            raise StudyRefused(f"refusing: {p} exists; the GEX family can no longer run (§3 order)")
    if main_dev_result is None:
        raise StudyRefused("refusing: --family gex needs the main dev_result.json and its SHA-256")
    check_sha(main_dev_result, main_dev_result_sha256, "main dev_result.json")
    result = read_json(main_dev_result)
    if result.get("family") != "main":
        raise StudyRefused("refusing: the given dev_result.json is not the main family's")
    if result.get("reproduction", {}).get("config_sha256") != config_sha256:
        raise StudyRefused("refusing: the main dev_result.json was made with another config")
    if result.get("verdict") == "NO_PASS":
        return {"condition": "main NO_PASS", "main_dev_result_sha256": main_dev_result_sha256}
    text, info = addendum_merged(main_addendum_commit, "main", repo=repo, main_ref=main_ref)
    if main_dev_result_sha256 not in text:
        raise StudyRefused("refusing: the main addendum does not record the main dev_result SHA-256")
    return {"condition": "main addendum merged", "main_dev_result_sha256": main_dev_result_sha256, **info}


def require_gex_done(out_dir: Path, gex_dev_result: Path | None, gex_dev_result_sha256: str | None,
                     gex_addendum_commit: str | None, *, config_sha256: str | None, repo: Path = ROOT,
                     main_ref: str = MAIN_REF) -> dict:
    """§3 order for the main HOLDOUT: GEX DEV is finished (NO_PASS, or its addendum recording the GEX
    dev_result SHA is merged), or the user's write-once GEX-skip marker exists."""
    marker = Path(out_dir) / "gex" / GEX_SKIP_MARKER
    if gex_dev_result is None:
        if marker.exists():
            return {"condition": "GEX skipped by user", "marker_sha256": CD.sha256_file(marker)}
        raise StudyRefused("refusing: the main HOLDOUT needs the GEX dev_result (NO_PASS or merged GEX addendum) "
                           f"or the user's GEX-skip marker {marker} (§3 order)")
    check_sha(gex_dev_result, gex_dev_result_sha256, "GEX dev_result.json")
    result = read_json(gex_dev_result)
    if result.get("family") != "gex":
        raise StudyRefused("refusing: the given GEX dev_result.json is not the GEX family's")
    if result.get("reproduction", {}).get("config_sha256") != config_sha256:
        raise StudyRefused("refusing: the GEX dev_result.json was made with another config")
    if result.get("verdict") == "NO_PASS":
        return {"condition": "GEX NO_PASS", "gex_dev_result_sha256": gex_dev_result_sha256}
    text, info = addendum_merged(gex_addendum_commit, "gex", repo=repo, main_ref=main_ref)
    if gex_dev_result_sha256 not in text:
        raise StudyRefused("refusing: the GEX addendum does not record the GEX dev_result SHA-256")
    return {"condition": "GEX addendum merged", "gex_dev_result_sha256": gex_dev_result_sha256, **info}


def record_gex_skip(*, out_dir: Path = OUT_DIR, approved: bool, reason: str) -> tuple[str, Path]:
    """Write-once marker that the user decided not to run the GEX family; refused once GEX has started."""
    _approval(approved)
    if not reason.strip():
        raise StudyRefused("refusing: a reason for skipping the GEX family is required")
    gex = Path(out_dir) / "gex"
    for name in ("precount.json", "dev_result.json"):
        if (gex / name).exists():
            raise StudyRefused(f"refusing: {gex / name} exists; the GEX family has already started")
    marker = gex / GEX_SKIP_MARKER
    return write_result(marker, {"family": "gex", "decision": "GEX skipped by user", "user_approval": True,
                                 "reason": reason, "git": git_state()}), marker


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def git_state(repo: Path = ROOT) -> dict:
    head = git("rev-parse", "HEAD", repo=repo)
    status = git("status", "--porcelain", "--untracked-files=no", repo=repo)
    return {"commit": head.stdout.strip() if head.returncode == 0 else None,
            "dirty_tracked": None if status.returncode != 0 else bool(status.stdout.strip())}


def reproduction(config_path: Path, config: dict) -> dict:
    return {"git": git_state(), "config_sha256": CD.sha256_file(config_path),
            "manifest_sha256": config["data"]["manifest_sha256"],
            "events_yaml_sha256": config["data"]["events_yaml_sha256"], "python": platform.python_version(),
            "numpy": np.__version__, "pandas": pd.__version__, "base_seed": config["stats"]["base_seed"],
            "seed_variants": {**config["stats"]["variants"],
                              # report-only reruns of variants 1/2/3 follow SEED(variant + 4, k)
                              "main_dev_stress_rerun": 5, "main_dev_sensitivity_rerun": 6,
                              "main_dev_actual_etf_rerun": 7}}


def start_checks(config_path: Path, config_sha256: str | None, out_path: Path, *, allow_dirty: bool,
                 expected_manifest_sha256: str | None = None) -> dict:
    check_sha(config_path, config_sha256, "config")
    config = CD.load_config(config_path)
    check_binding(config, expected_manifest_sha256=expected_manifest_sha256)
    if Path(out_path).exists():
        raise StudyRefused(f"refusing: {out_path} already exists (results are write-once)")
    if not allow_dirty and git_state()["dirty_tracked"]:
        raise StudyRefused("refusing: tracked files are modified; commit first or pass --allow-dirty")
    return config


def write_result(path: Path, obj: dict) -> str:
    try:
        return write_once(Path(path), canonical_bytes(obj))
    except FileExistsError as exc:
        raise StudyRefused(f"refusing: {path} already exists (results are write-once)") from exc


# ------------------------------------------------------------------ data -> session tables


def thresholds_for(config: dict, index: str) -> dict:
    th = config["thresholds"]
    return {"vix_t1": th["vix_t1"], "vix_t2": th["vix_t2"], **th["per_index"][index]}


def load_context(config: dict, stage: str, *, gex: bool, with_etf: bool, root: Path | None = None,
                 expected_manifest_sha256: str | None = None) -> dict:
    """Calendar, flags and per-index session tables for `stage` (prices end at the stage cut-off).
    with_etf=False (precount) reads no ETF price."""
    kw = {"root": root, "expected_manifest_sha256": expected_manifest_sha256}
    calendar = D.load_calendar(**kw)
    aux = {}
    for name, sym in (("vix", "VIX"), ("v9", "VIX9D"), ("v3", "VIX3M")):
        d = D.load_prices(sym, stage, **kw)
        aux[name] = dict(zip(d["date"], d["close"].astype(float)))
    gex_rows = None
    if gex:
        gex_rows = D.load_prices("DIX", stage, **kw)
        aux["gex"] = dict(zip(gex_rows["date"], gex_rows["gex"].astype(float)))
    px, divs, tables, etf = {}, {}, {}, {}
    for index, spec in config["indices"].items():
        px[index] = D.load_prices(index, stage, **kw)
        divs[index] = D.load_dividends(index, **kw)
        tables[index] = FT.session_table(index, px[index], divs[index], calendar, spec["start"], aux)
        for side in CD.SIDES if with_etf else ():
            sym = spec[f"{side}_etf"]
            e = D.load_prices(sym, stage, **kw)
            etf[sym] = dict(zip(e["date"], (e["close"] / e["open"] - 1).astype(float)))
    return {"calendar": calendar, "cal": CD.calendar_flags(calendar, config["unscheduled_closures"]),
            "ev": CD.event_flags(calendar, CD.load_events()), "tables": tables, "px": px, "divs": divs,
            "etf": etf, "gex_rows": gex_rows, "stage": stage}


def window(t: pd.DataFrame, lo: str | None, hi: str) -> pd.DataFrame:
    m = t["eligible"] & (t["date"] <= hi)
    if lo is not None:
        m &= t["date"] >= lo
    return t[m].reset_index(drop=True)


def cond_values(family: str, cond: int, t: pd.DataFrame, config: dict, ctx: dict, index: str,
                gex_q20: float | None) -> pd.Series:
    if family == "main":
        return CD.main_condition(cond, t, thresholds_for(config, index), ctx["cal"], ctx["ev"])
    return CD.gex_condition(cond, t, gex_q20)


def split_date(config: dict, family: str, index: str, cond: int, gex_split: dict | None) -> str:
    if family == "gex":
        return gex_split[index]
    hs = config["half_split"][index]
    return hs.get(f"C{cond:02d}", hs["default"])


def split_of(dates: list[str]) -> str | None:
    """§3: first date of the back half = dates[n // 2] of the sorted defined-feature session list."""
    return dates[len(dates) // 2] if dates else None


def family_spec(config: dict, family: str) -> tuple[list[dict], list[dict], str]:
    if family == "main":
        return config["conditions"], config["candidates"], "k"
    return config["gex_conditions"], config["gex_candidates"], "kg"


def dev_window(config: dict, family: str) -> tuple[str | None, str]:
    w = config["windows"]
    return (None, w["main_dev_end"]) if family == "main" else (w["gex_dev_start"], w["gex_dev_end"])


def holdout_window(config: dict, family: str) -> tuple[str, str]:
    w = config["windows"]
    return w[f"{family}_holdout_start"], w["holdout_end"]


def decomposition(t: pd.DataFrame) -> dict:
    """Method ⑤ (report only): overnight g vs intraday i over the given sessions."""
    out = {"n": len(t)}
    if not len(t):
        return out
    lo, li = float(np.log1p(t["g"]).sum()), float(np.log1p(t["i"]).sum())
    for name, x, lx in (("overnight", t["g"], lo), ("intraday", t["i"], li)):
        out[name] = {"mean": float(x.mean()), "cumulative": float(math.expm1(lx)),
                     "log_share": lx / (lo + li) if (lo + li) != 0 else None}
    return out


# ------------------------------------------------------------------ precount (no P&L)


def precount_main(config: dict, ctx: dict, smh: tuple[pd.DataFrame, dict], holdout_dates: dict) -> dict:
    """Recompute start dates, thresholds, half-split dates and the prefilter; any difference from the config
    stops the run. Counts, sd of R* and power only — no mean return is computed."""
    smh_stats = FT.year_stats(*smh)
    dev_end = config["windows"]["main_dev_end"]
    last_year = int(dev_end[:4])
    problems = []
    for index, spec in config["indices"].items():
        got = FT.start_date(index, ctx["px"][index], ctx["divs"][index], last_year,
                            smh_stats if index == FT.SOX else None)
        if got != spec["start"]:
            problems.append(f"{index} start {got} != config {spec['start']}")
    th = FT.thresholds(ctx["tables"], dev_end)
    cfg = config["thresholds"]
    for key in ("vix_t1", "vix_t2", "vix_n"):
        if th[key] != cfg[key]:
            problems.append(f"{key} {th[key]!r} != config {cfg[key]!r}")
    for index, vals in th["per_index"].items():
        for key, v in vals.items():
            if v != cfg["per_index"][index][key]:
                problems.append(f"{index} {key} {v!r} != config {cfg['per_index'][index][key]!r}")
    splits = {}
    for index, t in ctx["tables"].items():
        dev = window(t, None, dev_end)
        splits[index] = {
            "default": split_of(list(dev["date"])),
            "C06": split_of(list(dev["date"][dev["vix_p"].notna() & dev["v3_p"].notna()])),
            "C05": split_of(list(dev["date"][dev["vix_p"].notna() & dev["v9_p"].notna()])),
        }
        if splits[index] != config["half_split"][index]:
            problems.append(f"{index} half split {splits[index]} != config {config['half_split'][index]}")
    if problems:
        raise StudyRefused("precount differs from the fixed config: " + "; ".join(problems))
    return _precount_family(config, ctx, "main", None, None, holdout_dates,
                            extra={"thresholds": th, "half_split": splits,
                                   "start_dates": {i: s["start"] for i, s in config["indices"].items()}})


def precount_gex(config: dict, ctx: dict, holdout_dates: dict) -> dict:
    lo, hi = dev_window(config, "gex")
    g = ctx["gex_rows"]
    sessions = set(ctx["calendar"])
    dev_gex = g[(g["date"] >= lo) & (g["date"] <= hi) & g["date"].isin(sessions)]
    q20 = FT.quantile(dev_gex["gex"].astype(float), config["gex"]["g03_quantile"])
    splits = {}
    for index, t in ctx["tables"].items():
        dev = window(t, lo, hi)
        splits[index] = split_of(list(dev["date"][dev["gex_p"].notna()]))
    return _precount_family(config, ctx, "gex", q20, splits, holdout_dates,
                            extra={"gex_q20": q20, "gex_dev_days": len(dev_gex), "half_split": splits})


def _precount_family(config: dict, ctx: dict, family: str, gex_q20, gex_split, holdout_dates: dict,
                     extra: dict) -> dict:
    conds, cands, key = family_spec(config, family)
    lo, hi = dev_window(config, family)
    min_n = config["stats"]["dev_min_n"]
    cells, sd, per_index = {}, {}, {}
    for index, t in ctx["tables"].items():
        dev = window(t, lo, hi)
        span = t["in_range"] & (t["date"] <= hi) & (t["date"] >= (lo or ""))
        excl = {e: int(t.loc[span, e].sum()) for e in FT.EXCLUSIONS}
        sd_all = [ST.sample_sd(RT.r_star(dev["i"], dev["g"], side)) for side in CD.SIDES]
        per_index[index] = {"eligible_dev": len(dev), "exclusions": {**excl, "any": int(t.loc[
            span, "excluded"].sum())}, "sd_all_r_star": {s: v for s, v in zip(CD.SIDES, sd_all)}}
        for c in conds:
            v = cond_values(family, c["n"], dev, config, ctx, index, gex_q20)
            on = v.fillna(False).astype(bool).to_numpy()
            days = dev[on]
            sp = split_date(config, family, index, c["n"], gex_split)
            cells[(index, c["id"])] = {
                "index": index, "condition": c["id"], "n": int(on.sum()), "undefined": int(v.isna().sum()),
                "n_front": int((days["date"] < sp).sum()), "n_back": int((days["date"] >= sp).sum()),
                "split": sp, "prefiltered": int(on.sum()) < min_n}
            for side in CD.SIDES:
                r = RT.r_star(days["i"], days["g"], side)
                sd[(index, c["id"], side)] = ST.sample_sd(r) if len(r) >= 2 else None
    prefiltered = sorted([i, c] for (i, c), cell in cells.items() if cell["prefiltered"])
    k_active = sum(1 for x in cands if not cells[(x["index"], x["condition"])]["prefiltered"])
    if family == "main":
        exp = config["prefilter"]
        if sorted(map(list, exp["expected_prefiltered"])) != prefiltered or k_active != exp["expected_k"]:
            raise StudyRefused(f"prefilter {prefiltered} (K={k_active}) differs from the config "
                               f"{exp['expected_prefiltered']} (K={exp['expected_k']})")
    alpha_dev = config["stats"]["q_bh"] / k_active if k_active else None
    m = config["stats"]["M"] if family == "main" else config["stats"]["M_G"]
    alpha_hold = config["stats"]["holm_alpha"] / m
    holdout = _holdout_counts(config, ctx, family, holdout_dates)
    for (index, _), cell in cells.items():
        sessions = holdout.get(index, {}).get("sessions") if family == "main" else holdout["GEX"]["days"]
        eligible = per_index[index]["eligible_dev"]
        cell["holdout_expected_n"] = cell["n"] / eligible * sessions if eligible and sessions else None
    per_cand = []
    for x in cands:
        cell = cells[(x["index"], x["condition"])]
        s = sd[(x["index"], x["condition"], x["side"])]
        per_cand.append({key: x[key], "index": x["index"], "condition": x["condition"], "side": x["side"],
                         "n": cell["n"], "prefiltered": cell["prefiltered"], "sd_r_star": s,
                         "delta_dev": power_delta(s, cell["n"], alpha_dev) if s and alpha_dev else None})
    delta_hold = {}
    for index, info in per_index.items():
        sd_i = max(info["sd_all_r_star"].values())
        counts = {k: v for k, v in holdout.get(index, {}).items() if isinstance(v, int) and v > 0}
        counts.update({c: cell["holdout_expected_n"] for (i, c), cell in cells.items()
                       if i == index and c not in counts and cell["holdout_expected_n"]})
        delta_hold[index] = {k: power_delta(sd_i, n, alpha_hold) for k, n in counts.items()}
    return {"family": family, "k_total": len(cands), "k_active": k_active, "prefiltered": prefiltered,
            "per_index": per_index, "cells": list(cells.values()), "candidates": per_cand,
            "power": {"alpha_dev": alpha_dev, "alpha_holdout": alpha_hold, "delta_holdout": delta_hold,
                      "note": "delta = (z_{1-alpha} + z_0.8) * sd / sqrt(n); HOLDOUT sd = larger side of the "
                              "index's all-DEV-days R* sd"},
            "holdout": holdout, **extra}


def _holdout_counts(config: dict, ctx: dict, family: str, holdout_dates: dict) -> dict:
    """HOLDOUT date counts from coverage (no prices): sessions per index and, for the main family, the
    calendar/event candidates' date counts (C17-C29)."""
    out = {}
    conds, _, _ = family_spec(config, family)
    for index, dates in holdout_dates.items():
        if index == "GEX":
            out["GEX"] = {"days": len(dates)}
            continue
        row = {"sessions": len(dates)}
        if family == "main":
            t = pd.DataFrame({"date": dates})
            for col in ("g", "poc", "prng", "vix_p", "v9_p", "v3_p"):
                t[col] = np.nan
            for c in conds:
                if c["n"] >= 17:
                    v = CD.main_condition(c["n"], t, thresholds_for(config, index), ctx["cal"], ctx["ev"])
                    row[c["id"]] = int(v.fillna(False).sum())
        out[index] = row
    return out


# ------------------------------------------------------------------ candidate trade series


def trade_series(days: pd.DataFrame, side: str, etf: dict) -> dict:
    """All per-trade series of one candidate (sorted by date)."""
    r = RT.r_star(days["i"], days["g"], side)
    etf_r = days["date"].map(lambda d: etf.get(d, np.nan)).to_numpy(dtype=float)
    has = ~np.isnan(etf_r)
    return {"dates": list(days["date"]), "r_star": r, **{c: RT.net(r, c) for c in COST_SETS},
            "simple_base": RT.net(RT.simple_3x(days["i"], side), "base"),
            "etf_dates": list(days["date"][has]), "etf_base": RT.net(etf_r[has], "base"),
            "etf_stress": RT.net(etf_r[has], "stress"), "synth_on_etf_days": RT.net(r[has], "base")}


def _corr(a, b) -> float | None:
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    if len(a) < 2 or ST.sample_sd(a) == 0 or ST.sample_sd(b) == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def summary(x) -> dict:
    arr = np.asarray(x, dtype=float)
    return {"n": len(arr), "mean": ST.mean_or_none(arr),
            "median": float(np.median(arr)) if len(arr) else None,
            "win_rate": float(np.mean(arr > 0)) if len(arr) else None, "t": ST.t_stat(arr)}


def etf_compare(s: dict) -> dict:
    out = {"n": len(s["etf_dates"]), "mean_base": ST.mean_or_none(s["etf_base"]),
           "mean_stress": ST.mean_or_none(s["etf_stress"]),
           "corr_with_synthetic": _corr(s["etf_base"], s["synth_on_etf_days"])}
    sm = ST.mean_or_none(s["synth_on_etf_days"])
    out["mean_diff_etf_minus_synthetic"] = None if sm is None or out["mean_base"] is None else \
        out["mean_base"] - sm
    return out


def candidate_days(config: dict, ctx: dict, family: str, x: dict, lo: str | None, hi: str,
                   gex_q20: float | None) -> pd.DataFrame:
    t = window(ctx["tables"][x["index"]], lo, hi)
    cond = int(x["condition"][1:])
    v = cond_values(family, cond, t, config, ctx, x["index"], gex_q20)
    return t[v.fillna(False).astype(bool).to_numpy()].reset_index(drop=True)


# ------------------------------------------------------------------ DEV


def run_dev(config: dict, ctx: dict, family: str, precount: dict) -> dict:
    _, cands, key = family_spec(config, family)
    lo, hi = dev_window(config, family)
    gex_q20 = precount.get("gex_q20")
    gex_split = precount["half_split"] if family == "gex" else None
    variant = ST.VARIANTS[f"{family}_dev"]
    prefiltered = {tuple(p) for p in precount["prefiltered"]}
    sox_from = config["windows"]["sox_etf_from"]
    rows, p_main = {}, {}
    for x in cands:
        k = x[key]
        if (x["index"], x["condition"]) in prefiltered:
            rows[k] = {key: k, "index": x["index"], "condition": x["condition"], "side": x["side"],
                       "prefiltered": True, "reason": f"DEV n < {config['stats']['dev_min_n']}"}
            continue
        days = candidate_days(config, ctx, family, x, lo, hi, gex_q20)
        s = trade_series(days, x["side"], ctx["etf"][x["etf"]])
        sp = split_date(config, family, x["index"], int(x["condition"][1:]), gex_split)
        front = np.array([d < sp for d in s["dates"]], dtype=bool)
        boot = ST.bootstrap_p(s["base"], variant, k)
        p_main[k] = boot["p"]
        row = {key: k, "index": x["index"], "condition": x["condition"], "side": x["side"], "etf": x["etf"],
               "prefiltered": False, "n": len(s["dates"]), "n_front": int(front.sum()),
               "n_back": int((~front).sum()), "split": sp, "bootstrap": boot,
               "base": summary(s["base"]), "stress": summary(s["stress"]), "sensitivity": summary(s["sensitivity"]),
               "simple_3x_base_mean": ST.mean_or_none(s["simple_base"]),
               "half_means": [ST.mean_or_none(s["base"][front]), ST.mean_or_none(s["base"][~front])],
               "actual_etf": etf_compare(s)}
        row["usd_700_base_mean"] = None if row["base"]["mean"] is None else row["base"]["mean"] * USD_NOTIONAL
        if family == "main":
            row["report_p"] = {
                name: ST.bootstrap_p(arr, ST.VARIANTS[f"main_dev_{name}"], k)
                for name, arr in (("stress", s["stress"]), ("sensitivity", s["sensitivity"]),
                                  ("actual_etf", s["etf_base"]))}
        sox = x["index"] == FT.SOX
        sox_mask = np.array([d >= sox_from for d in s["etf_dates"]], dtype=bool)
        row["sox_etf_mean_from_2010_03_11"] = ST.mean_or_none(s["etf_base"][sox_mask]) if sox else None
        rows[k] = row
    bh, by = ST.bh_reject(p_main, config["stats"]["q_bh"]), ST.by_reject(p_main, config["stats"]["q_bh"])
    survivors = []
    for k, row in rows.items():
        if row["prefiltered"]:
            continue
        row["bh_reject"], row["by_reject"] = k in bh, k in by
        row["dev"] = ST.dev_survivor(bh_rejected=k in bh, n=row["n"], stress_mean=row["stress"]["mean"],
                                     half_means=tuple(row["half_means"]), is_sox=row["index"] == FT.SOX,
                                     sox_etf_mean=row["sox_etf_mean_from_2010_03_11"])
        if row["dev"]["survivor"]:
            survivors.append({"k": k, "stress_t": row["stress"]["t"], "n": row["n"]})
    m = config["stats"]["M"] if family == "main" else config["stats"]["M_G"]
    selected = ST.rank_survivors(survivors, m)
    decomp = {index: decomposition(window(t, lo, hi)) for index, t in ctx["tables"].items()}
    return {"family": family, "k_active": len(p_main), "bh_rejected": sorted(bh), "by_rejected": sorted(by),
            "survivors": sorted(c["k"] for c in survivors), "selected": selected,
            "verdict": "SELECTED" if selected else "NO_PASS", "candidates": [rows[k] for k in sorted(rows)],
            "decomposition": decomp, "window": [lo, hi], "flat_bar_exclusions": flat_bar_counts(ctx, lo, hi)}


def flat_bar_counts(ctx: dict, lo: str | None, hi: str) -> dict:
    """Exclusion (b) flat bar uses d's own H/L/C (known only at the close): its count in the window, reported."""
    out = {}
    for index, t in ctx["tables"].items():
        m = t["in_range"] & (t["date"] <= hi) & (t["date"] >= (lo or ""))
        out[index] = int(t.loc[m, "b_flat_bar"].sum())
    return out


# ------------------------------------------------------------------ HOLDOUT


def run_holdout(config: dict, ctx: dict, family: str, precount: dict, dev_result: dict) -> dict:
    selected = dev_result["selected"]
    if not selected or dev_result.get("verdict") != "SELECTED":
        raise StudyRefused("refusing: DEV selected no candidate (NO_PASS); the HOLDOUT stays sealed")
    _, cands, key = family_spec(config, family)
    by_k = {x[key]: x for x in cands}
    lo, hi = holdout_window(config, family)
    w = config["windows"]
    clean_lo, clean_hi = max(lo, w["holdout_clean"][0]), w["holdout_clean"][1]
    part = w["holdout_parts_split"]
    variant = ST.VARIANTS[f"{family}_holdout"]
    rows, p = {}, {}
    for k in selected:
        x = by_k[k]
        days = candidate_days(config, ctx, family, x, lo, hi, precount.get("gex_q20"))
        s = trade_series(days, x["side"], ctx["etf"][x["etf"]])
        boot = ST.bootstrap_p(s["base"], variant, k)
        p[k] = boot["p"]
        clean = np.array([clean_lo <= d <= clean_hi for d in s["dates"]], dtype=bool)
        early = np.array([d < part for d in s["dates"]], dtype=bool)
        rows[k] = {key: k, "index": x["index"], "condition": x["condition"], "side": x["side"], "etf": x["etf"],
                   "n": len(s["dates"]), "bootstrap": boot, "base": summary(s["base"]),
                   "stress": summary(s["stress"]), "sensitivity": summary(s["sensitivity"]),
                   "simple_3x_base_mean": ST.mean_or_none(s["simple_base"]), "actual_etf": etf_compare(s),
                   "clean_subperiod": summary(s["base"][clean]),
                   "parts": {f"before_{part}": summary(s["base"][early]),
                             f"from_{part}": summary(s["base"][~early])}}
    adj = holm_adjust(p, len(selected))
    for k, row in rows.items():
        row["holm_adj"] = adj[k]
        row["holdout"] = ST.holdout_verdict(n=row["n"], holm_adj=adj[k], stress_mean=row["stress"]["mean"],
                                            etf_mean=row["actual_etf"]["mean_base"])
    decomp = {index: decomposition(window(t, lo, hi)) for index, t in ctx["tables"].items()}
    return {"family": family, "m": len(selected), "window": [lo, hi],
            "candidates": [rows[k] for k in selected], "decomposition": decomp,
            "flat_bar_exclusions": flat_bar_counts(ctx, lo, hi)}


# ------------------------------------------------------------------ steps (called by scripts/daily_screen_v1_*.py)


def _family(family: str) -> str:
    if family not in FAMILIES:
        raise StudyRefused(f"--family must be one of {FAMILIES}, got {family!r}")
    return family


def _approval(approved: bool) -> None:
    if not approved:
        raise StudyRefused("refusing: --i-have-user-approval is required")


def _check_precount(path: Path, sha: str | None, family: str, config_sha: str) -> dict:
    check_sha(path, sha, "precount.json")
    pc = read_json(path)
    if pc.get("family") != family or pc["reproduction"]["config_sha256"] != config_sha:
        raise StudyRefused("refusing: precount.json is for another family or another config")
    return pc


def precount_step(family: str, *, config_path: Path, config_sha256: str | None, out_dir: Path = OUT_DIR,
                  main_dev_result: Path | None = None, main_dev_result_sha256: str | None = None,
                  main_addendum_commit: str | None = None, allow_dirty: bool = False, root: Path | None = None,
                  expected_manifest_sha256: str | None = None, repo: Path = ROOT,
                  main_ref: str = MAIN_REF) -> tuple[str, Path]:
    """§6: counts, thresholds, sd of R* and power only (no mean return). Writes <out>/<family>/precount.json."""
    family = _family(family)
    out = Path(out_dir) / family / "precount.json"
    config = start_checks(config_path, config_sha256, out, allow_dirty=allow_dirty,
                          expected_manifest_sha256=expected_manifest_sha256)
    order = None
    if family == "gex":
        order = require_main_done(main_dev_result, main_dev_result_sha256, main_addendum_commit,
                                  config_sha256=config_sha256, out_dir=out_dir, repo=repo, main_ref=main_ref)
    kw = {"root": root, "expected_manifest_sha256": expected_manifest_sha256}
    ctx = load_context(config, f"{family}_dev", gex=family == "gex", with_etf=False, **kw)
    dates = {i: D.holdout_dates(i, family, **kw) for i in config["indices"]}
    if family == "main":
        smh = (D.load_prices("SMH", "main_dev", **kw), D.load_dividends("SMH", **kw))
        result = precount_main(config, ctx, smh, dates)
    else:
        sessions = set(ctx["calendar"])
        dates["GEX"] = [d for d in D.holdout_dates("DIX", "gex", **kw) if d in sessions]
        result = precount_gex(config, ctx, dates)
    result["run_order"] = order
    result["reproduction"] = reproduction(config_path, config)
    return write_result(out, result), out


def dev_step(family: str, *, config_path: Path, config_sha256: str | None, precount_path: Path,
             precount_sha256: str | None, approved: bool, out_dir: Path = OUT_DIR,
             main_dev_result: Path | None = None, main_dev_result_sha256: str | None = None,
             main_addendum_commit: str | None = None, allow_dirty: bool = False, root: Path | None = None,
             expected_manifest_sha256: str | None = None, repo: Path = ROOT,
             main_ref: str = MAIN_REF) -> tuple[str, Path]:
    """§7: one DEV run per family. Writes <out>/<family>/dev_result.json."""
    family = _family(family)
    _approval(approved)
    out = Path(out_dir) / family / "dev_result.json"
    config = start_checks(config_path, config_sha256, out, allow_dirty=allow_dirty,
                          expected_manifest_sha256=expected_manifest_sha256)
    pc = _check_precount(precount_path, precount_sha256, family, config_sha256)
    order = None
    if family == "gex":
        order = require_main_done(main_dev_result, main_dev_result_sha256, main_addendum_commit,
                                  config_sha256=config_sha256, out_dir=out_dir, repo=repo, main_ref=main_ref)
    ctx = load_context(config, f"{family}_dev", gex=family == "gex", with_etf=True, root=root,
                       expected_manifest_sha256=expected_manifest_sha256)
    result = run_dev(config, ctx, family, pc)
    result["run_order"] = order
    result["inputs"] = {"precount_sha256": precount_sha256}
    result["reproduction"] = reproduction(config_path, config)
    return write_result(out, result), out


def holdout_step(family: str, *, config_path: Path, config_sha256: str | None, precount_path: Path,
                 precount_sha256: str | None, dev_result_path: Path, dev_result_sha256: str | None,
                 addendum_commit: str | None, approved: bool, out_dir: Path = OUT_DIR,
                 gex_dev_result: Path | None = None, gex_dev_result_sha256: str | None = None,
                 gex_addendum_commit: str | None = None, allow_dirty: bool = False, root: Path | None = None,
                 expected_manifest_sha256: str | None = None, repo: Path = ROOT,
                 main_ref: str = MAIN_REF) -> tuple[str, Path]:
    """§8: one HOLDOUT run per family after its addendum is merged. Writes <out>/<family>/holdout_result.json.

    The addendum must record the config, precount, dev_result, manifest and events-yaml SHA-256 and hold one
    machine line `selected: [k, ...]` equal (as a set) to the dev_result selection. The main family also needs
    GEX DEV finished or the user's GEX-skip marker (§3 order: ① main DEV → ② GEX → ③ HOLDOUT)."""
    family = _family(family)
    _approval(approved)
    out = Path(out_dir) / family / "holdout_result.json"
    config = start_checks(config_path, config_sha256, out, allow_dirty=allow_dirty,
                          expected_manifest_sha256=expected_manifest_sha256)
    pc = _check_precount(precount_path, precount_sha256, family, config_sha256)
    check_sha(dev_result_path, dev_result_sha256, "dev_result.json")
    dev = read_json(dev_result_path)
    if dev.get("family") != family or dev["inputs"]["precount_sha256"] != precount_sha256 \
            or dev["reproduction"]["config_sha256"] != config_sha256:
        raise StudyRefused("refusing: dev_result.json is for another family, precount or config")
    text, merged = addendum_merged(addendum_commit, family, repo=repo, main_ref=main_ref)
    need = {"config": config_sha256, "precount": precount_sha256, "dev_result": dev_result_sha256,
            "manifest": config["data"]["manifest_sha256"], "events_yaml": config["data"]["events_yaml_sha256"]}
    missing = [k for k, v in need.items() if v not in text]
    if missing:
        raise StudyRefused(f"refusing: the addendum does not record the SHA-256 of {missing}")
    if sorted(selected_in_addendum(text)) != sorted(dev.get("selected") or []):
        raise StudyRefused("refusing: the addendum's selected candidates differ from dev_result.json")
    order = None
    if family == "main":
        order = require_gex_done(out_dir, gex_dev_result, gex_dev_result_sha256, gex_addendum_commit,
                                 config_sha256=config_sha256, repo=repo, main_ref=main_ref)
    ctx = load_context(config, "holdout", gex=family == "gex", with_etf=True, root=root,
                       expected_manifest_sha256=expected_manifest_sha256)
    result = run_holdout(config, ctx, family, pc, dev)
    result["run_order"] = order
    result["inputs"] = {"precount_sha256": precount_sha256, "dev_result_sha256": dev_result_sha256,
                        "addendum": merged}
    result["reproduction"] = reproduction(config_path, config)
    return write_result(out, result), out

