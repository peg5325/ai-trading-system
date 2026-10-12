"""Reversal study v1 data / wiring tests (PREREG_DAYTRADE_REVERSAL_STUDY_V1 §11): fixed-input refusal, seal and
development bound, OOS wiring, pipeline, precount, overlay reproduction harness. Synthetic data and temporary
databases only; no real price file is read."""
from __future__ import annotations

import copy
import json
import random
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from src.daytrade_paper.core import Bar
from src.entry_study_v1 import devrun as ENTRY_DEVRUN
from src.entry_study_v1 import mask as M
from src.entry_study_v1.bars import DataMismatch, DevBoundError, StudyRefused
from src.entry_study_v1.config import EVENTS_HISTORY, sha256_file
from src.entry_study_v1.config import load_cells as load_entry_cells
from src.entry_study_v1.data import (
    DEV_CUTOFF,
    DEV_SEGMENTS,
    OOS_EXCLUDED,
    Segment,
    SymbolData,
    load_symbol,
)
from src.entry_study_v1.pipeline import pair_trades
from src.history import exchange_calendar as xcal
from src.history.oos_guard import OOSLockError, research_load
from src.history.store import SCHEMA
from src.models import ET
from src.reversal_study_v1 import cli as C
from src.reversal_study_v1 import config as RC
from src.reversal_study_v1 import oos as O
from src.reversal_study_v1 import pipeline as PL
from src.reversal_study_v1 import precount as PC
from src.reversal_study_v1 import rules as RL

ROOT = Path(__file__).resolve().parents[1]
CELLS = RC.load_cells()
ENTRY_CELLS = load_entry_cells()
SIX = ("SMH", "SOXL", "SOXS", "QQQ", "TQQQ", "SQQQ")
OPEN = 570


# ------------------------------------------------------------------ synthetic DB


def _row(sym, d: date, label: int, p: float):
    hh, mm = divmod(label, 60)
    et = datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET)
    return (sym, "US", "1m", et.astimezone(timezone.utc).isoformat(), et.isoformat(), "IGNORED",
            p, p + 0.1, p - 0.1, p, 1000.0, "USD", 1, "test", "x", "x")


def make_db(path, rows):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany(f"INSERT INTO candle_history VALUES ({','.join('?' * 16)})", rows)
    con.commit()
    con.close()
    return str(path)


def six_symbol_db(tmp_path):
    rows = []
    for i, sym in enumerate(SIX):
        rows.append(_row(sym, date(2023, 5, 2), 600, 100.0 + i))
        rows.append(_row(sym, date(2025, 5, 2), 600, 200.0 + i))
    return make_db(tmp_path / "six.sqlite", rows)


# ------------------------------------------------------------------ seal and development bound


@pytest.fixture()
def spy_db(tmp_path):
    rows = [_row("SPY", date(2025, 12, 15), 600, 100.0), _row("SPY", date(2026, 1, 20), 600, 100.0),
            _row("SPY", date(2026, 3, 16), 600, 100.0), _row("SPY", date(2026, 9, 1), 600, 100.0)]
    return make_db(tmp_path / "h.sqlite", rows)


def test_performance_load_overlapping_the_seal_raises(spy_db):
    with pytest.raises(OOSLockError):
        research_load(spy_db, "SPY", "1m", start=datetime(2026, 1, 21, tzinfo=ET), end=datetime(2026, 7, 22, tzinfo=ET),
                      purpose="performance")
    with pytest.raises(OOSLockError):  # also through the study's own loader, without a grant
        load_symbol(spy_db, "SPY", date(2026, 3, 1), date(2026, 3, 31), purpose="performance", dev=False)
    assert load_symbol(spy_db, "SPY", date(2026, 3, 1), date(2026, 3, 31), purpose="coverage", dev=False).row_count == 1


def test_dev_code_refuses_requests_reaching_2026_01_21(tmp_path, spy_db):
    assert PL.DEV_SEGMENTS is DEV_SEGMENTS and DEV_CUTOFF == datetime(2026, 1, 21, tzinfo=ET)
    for seg in DEV_SEGMENTS:
        assert seg.bounds()[1] <= DEV_CUTOFF
    over = Segment("X", date(2026, 1, 15), date(2026, 1, 21), date(2026, 1, 15), date(2026, 1, 21))
    after = Segment("Y", date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 1), date(2026, 9, 2))
    for seg in (over, after):
        for purpose in ("performance", "coverage"):
            with pytest.raises(DevBoundError):
                PL.verified_dev_data(spy_db, ["SPY"], {"rows": {}}, purpose=purpose, segments=(seg,))
    ok = Segment("Z", date(2025, 12, 1), date(2025, 12, 31), date(2025, 12, 1), date(2025, 12, 31))
    with pytest.raises(DataMismatch):  # reaches the manifest check only because the window is legal
        PL.verified_dev_data(spy_db, ["SPY"], {"rows": {}}, purpose="performance", segments=(ok,))


def test_only_the_oos_module_may_load_without_the_development_bound():
    for path in (ROOT / "src" / "reversal_study_v1").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "load_candles" not in text and "daytrade_v11" not in text, path
        if path.name != "oos.py":
            assert "dev=False" not in text, path


# ------------------------------------------------------------------ fixed inputs (hash refusal)


def _fixed(tmp_path, monkeypatch, *, gate_pairs=("P1", "P2"), events_sha=None, mask_gate_sha=None, manifest=None):
    gate = tmp_path / "gate.json"
    man = tmp_path / "manifest.json"
    mask = tmp_path / "mask.json"
    gate.write_bytes(M.canonical_bytes({"pairs": {**{p: {"status": "PASS"} for p in gate_pairs},
                                                  "P3": {"status": "DATA_GATE_FAIL"}}}))
    man.write_bytes(M.canonical_bytes(manifest or {"rows": {}}))
    pm = RC.pair_map()
    mask.write_bytes(M.canonical_bytes({
        "pairs": {p: {"ref": pm[p][0], "long": pm[p][1], "short": pm[p][2]} for p in ("P1", "P2")},
        "events_calendar_sha256": events_sha or sha256_file(EVENTS_HISTORY),
        "gate_sha256": mask_gate_sha or sha256_file(gate), "data_manifest_sha256": sha256_file(man)}))
    frozen = {"gate_sha256": sha256_file(gate), "mask_sha256": sha256_file(mask),
              "data_manifest_sha256": sha256_file(man), "entry_dev_result_sha256": "0" * 64}
    monkeypatch.setattr(RC, "frozen_inputs", lambda path=RC.CONFIG_PATH: frozen)
    return gate, mask, man


def _argv(tmp_path, gate, mask, man, kind):
    common = ["--db", str(tmp_path / "none.sqlite"), "--gate", str(gate), "--mask", str(mask), "--manifest", str(man),
              "--allow-dirty", "--out-dir", str(tmp_path / "out")]
    approval = ["--i-have-user-approval"]
    dev_result = ["--dev-result", str(tmp_path / "dev.json"), "--dev-result-sha256", "0" * 64]
    return {
        "precount": common,
        "dev": [*common, *approval, "--entry-dev-result", str(tmp_path / "entry_dev.json")],
        "oos_prepare": ["prepare", *common, *dev_result],
        "oos_run": ["run", *common, *approval, *dev_result, "--prepare", "p", "--prepare-sha256", "0" * 64,
                    "--oos-mask", "m", "--oos-mask-sha256", "0" * 64, "--oos-snapshot", "s",
                    "--oos-snapshot-sha256", "0" * 64, "--snapshot-manifest-path", "x", "--snapshot-hash-field", "y"],
    }[kind]


def _main(kind):
    from scripts import (
        reversal_study_v1_dev,
        reversal_study_v1_oos,
        reversal_study_v1_precount,
    )
    return {"precount": reversal_study_v1_precount.main, "dev": reversal_study_v1_dev.main,
            "oos_prepare": reversal_study_v1_oos.main, "oos_run": reversal_study_v1_oos.main}[kind]


KINDS = ["precount", "dev", "oos_prepare", "oos_run"]


def test_fixed_inputs_pass_when_everything_matches(tmp_path, monkeypatch):
    gate, mask, man = _fixed(tmp_path, monkeypatch)
    args = _args(tmp_path, gate, mask, man)
    _g, _m, _man, pairs = C.require_inputs(args)
    assert pairs == RC.pair_map()


def _args(tmp_path, gate, mask, man, approval=True):
    import argparse
    return argparse.Namespace(db=tmp_path / "none", gate=gate, mask=mask, manifest=man, allow_dirty=True,
                              i_have_user_approval=approval)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("what", ["gate", "mask", "manifest"])
def test_one_changed_byte_in_a_fixed_file_refuses_every_script(tmp_path, monkeypatch, kind, what):
    gate, mask, man = _fixed(tmp_path, monkeypatch)
    path = {"gate": gate, "mask": mask, "manifest": man}[what]
    path.write_bytes(path.read_bytes() + b" ")  # one byte
    with pytest.raises(DataMismatch, match="hashes to"):
        _main(kind)(_argv(tmp_path, gate, mask, man, kind))
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("kind", KINDS)
def test_mask_link_and_events_calendar_mismatch_refuse_every_script(tmp_path, monkeypatch, kind):
    gate, mask, man = _fixed(tmp_path, monkeypatch, mask_gate_sha="a" * 64)
    with pytest.raises(DataMismatch, match="different gate"):
        _main(kind)(_argv(tmp_path, gate, mask, man, kind))
    gate, mask, man = _fixed(tmp_path, monkeypatch, events_sha="f" * 64)
    with pytest.raises(DataMismatch, match="event calendar"):
        _main(kind)(_argv(tmp_path, gate, mask, man, kind))


def test_gate_pairs_must_be_exactly_the_config_pairs(tmp_path, monkeypatch):
    gate, mask, man = _fixed(tmp_path, monkeypatch, gate_pairs=("P1",))
    with pytest.raises(DataMismatch):
        C.require_inputs(_args(tmp_path, gate, mask, man))


@pytest.mark.parametrize("kind", ["dev", "oos_run"])
def test_dev_and_oos_run_need_the_approval_flag(tmp_path, monkeypatch, kind):
    gate, mask, man = _fixed(tmp_path, monkeypatch)
    argv = [a for a in _argv(tmp_path, gate, mask, man, kind) if a != "--i-have-user-approval"]
    with pytest.raises(StudyRefused, match="approval"):
        _main(kind)(argv)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("kind", KINDS)
def test_every_script_goes_through_the_shared_verification(tmp_path, monkeypatch, kind):
    class Sentinel(Exception):
        pass

    def boom(*a, **k):
        raise Sentinel

    monkeypatch.setattr(C, "prepare_dev", boom)
    monkeypatch.setattr(O, "prepare_dev", boom)  # the OOS commands need the full check, not only (1)-(3)
    gate, mask, man = _fixed(tmp_path, monkeypatch)
    with pytest.raises(Sentinel):
        _main(kind)(_argv(tmp_path, gate, mask, man, kind))


def test_oos_prepare_refuses_when_a_non_target_symbol_row_changed(tmp_path, monkeypatch):
    from scripts import reversal_study_v1_oos
    db = six_symbol_db(tmp_path)
    manifest = M.build_manifest(PL.load_dev_segments(db, SIX, purpose="performance"), DEV_SEGMENTS)
    gate, mask, man = _fixed(tmp_path, monkeypatch, manifest=manifest)
    con = sqlite3.connect(db)  # QQQ (P2) is not a target of (a), which trades P1
    con.execute("UPDATE candle_history SET close = close + 0.01 WHERE symbol = 'QQQ' AND ts_exchange < '2024'")
    con.commit()
    con.close()
    dev = tmp_path / "dev.json"
    dev.write_text(json.dumps({"k_total": 16, "selected_for_oos": []}), encoding="utf-8")
    argv = ["prepare", "--db", db, "--gate", str(gate), "--mask", str(mask), "--manifest", str(man), "--allow-dirty",
            "--out-dir", str(tmp_path / "out"), "--dev-result", str(dev), "--dev-result-sha256", sha256_file(dev)]
    with pytest.raises(DataMismatch, match="QQQ.*differ from data_manifest"):
        reversal_study_v1_oos.main(argv)
    assert not (tmp_path / "out").exists()


def test_db_row_of_any_of_the_six_symbols_is_checked_against_the_manifest(tmp_path):
    db = six_symbol_db(tmp_path)
    seg_data = PL.load_dev_segments(db, SIX, purpose="performance")
    manifest = M.build_manifest(seg_data, DEV_SEGMENTS)
    PL.verified_dev_data(db, SIX, manifest, purpose="coverage")  # unchanged rows pass
    for sym in SIX:
        con = sqlite3.connect(db)
        con.execute("UPDATE candle_history SET close = close + 0.01 WHERE symbol = ? AND ts_exchange < '2024'",
                    (sym,))
        con.commit()
        con.close()
        with pytest.raises(DataMismatch, match=sym):
            PL.verified_dev_data(db, SIX, manifest, purpose="coverage")
        con = sqlite3.connect(db)
        con.execute("UPDATE candle_history SET close = close - 0.01 WHERE symbol = ? AND ts_exchange < '2024'",
                    (sym,))
        con.commit()
        con.close()
    # a deleted row changes the count
    con = sqlite3.connect(db)
    con.execute("DELETE FROM candle_history WHERE symbol = 'SQQQ' AND ts_exchange > '2025'")
    con.commit()
    con.close()
    with pytest.raises(DataMismatch, match="SQQQ"):
        PL.verified_dev_data(db, SIX, manifest, purpose="coverage")


# ------------------------------------------------------------------ synthetic sessions / mask rebuild


def synth_cal(start="2023-04-01", end="2026-12-31"):
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    events = [{"date": d.isoformat(), "type": "EARLY_CLOSE"} for d in xcal.early_close_days(d0, d1)]
    return {"version": "t", "coverage": {"start": start, "end": end}, "events": events}


def flat_data(seg, symbols=("REF", "L", "S")):
    out = {}
    for sym in symbols:
        sd = SymbolData(sym)
        for d in xcal.trading_days(seg.data_start, seg.data_end):
            iso = d.isoformat()
            sd.regular[iso] = [Bar(m, 100.0, 100.1, 99.9, 100.0, 1000.0)
                               for m in range(OPEN, xcal.regular_close_minute(d))]
            sd.pre[iso] = [Bar(480, 100.0, 100.0, 100.0, 100.0, 100.0)]
            sd.labels[iso] = [b.t + 1 for b in sd.regular[iso]]
        out[sym] = sd
    return out


SEG = Segment("T", date(2024, 9, 3), date(2024, 12, 31), date(2024, 10, 15), date(2024, 12, 31))


def test_rebuilt_mask_must_equal_the_fixed_mask():
    pairs = {"P": ("REF", "L", "S")}
    cal = synth_cal()
    data = {"T": flat_data(SEG)}
    fixed = M.build_mask(pairs, data, (SEG,), cal, "0" * 64)
    PL.check_rebuilt_mask(fixed, pairs, data, cal, "0" * 64, segments=(SEG,))  # identical -> accepted
    tampered = copy.deepcopy(fixed)
    tampered["pairs"]["P"]["segments"]["T"]["scored"].pop()
    with pytest.raises(DataMismatch):
        PL.check_rebuilt_mask(tampered, pairs, data, cal, "0" * 64, segments=(SEG,))
    broken = {"T": flat_data(SEG)}
    broken["T"]["L"].regular["2024-10-22"] = broken["T"]["L"].regular["2024-10-22"][:-1]  # rows differ from the mask
    with pytest.raises(DataMismatch):
        PL.check_rebuilt_mask(fixed, pairs, broken, cal, "0" * 64, segments=(SEG,))


def test_oos_mask_excludes_the_paper_week():
    assert OOS_EXCLUDED == tuple(date(2026, 10, d) for d in (5, 6, 7, 8, 9)) and O.OOS_EXCLUDED is OOS_EXCLUDED
    seg = Segment("OOS", date(2026, 8, 3), date(2026, 10, 9), date(2026, 9, 21), date(2026, 10, 9))
    m = M.build_segment_mask(seg, flat_data(seg), "REF", ("L", "S"), synth_cal(), OOS_EXCLUDED)
    ex = {e["date"]: e["reasons"] for e in m["excluded"]}
    for d in OOS_EXCLUDED:
        assert ex[d.isoformat()] == ["paper_week_already_seen"]
    assert "2026-10-02" in m["scored"] and "2026-10-05" not in m["scored"]
    text = (ROOT / "src" / "reversal_study_v1" / "oos.py").read_text(encoding="utf-8")
    assert text.count("cal, cal_sha, OOS_EXCLUDED)") == 2  # prepare and run both build the mask with the exclusion


# ------------------------------------------------------------------ OOS wiring


def test_targets_always_contain_the_designated_cell_a():
    only_a = O.targets({"k_total": 16, "selected_for_oos": []})
    assert [(t.label, t.cell.k, t.cell.strategy_id, t.pair, t.cell.rule) for t in only_a] == [
        ("a", 16, "reversal-study-v1-oos-a", "P1", "E2")]
    both = O.targets({"k_total": 16, "selected_for_oos": [11]})
    assert [(t.label, t.cell.k, t.cell.strategy_id, t.pair, t.cell.rule) for t in both] == [
        ("a", 16, "reversal-study-v1-oos-a", "P1", "E2"), ("b", 11, "reversal-study-v1-cell-11", "P2", "T2")]
    with pytest.raises(StudyRefused):
        O.targets({"k_total": 16, "selected_for_oos": [1, 2]})
    with pytest.raises(StudyRefused):
        O.targets({"k_total": 20, "selected_for_oos": []})  # an entry-study dev_result is not accepted


def test_implementation_paths_cover_both_packages_and_the_shared_dependencies():
    paths = set(O.IMPLEMENTATION_PATHS)
    for p in (*(f"src/entry_study_v1/{n}.py" for n in ("bars", "cli", "config", "data", "devrun", "gate", "mask",
                                                      "pipeline", "precount", "rules", "stats", "trades")),
              *(f"src/reversal_study_v1/{n}.py" for n in ("cli", "config", "devrun", "oos", "overlay", "pipeline",
                                                         "precount", "rules")),
              "scripts/reversal_study_v1_oos.py", "src/daytrade_paper/rules.py", "src/daytrade_paper/core.py",
              "src/daytrade_paper/store.py", "src/daytrade_paper/external.py", "src/history/exchange_calendar.py",
              "src/history/loader.py", "src/history/oos_guard.py", "src/models.py"):
        assert p in paths and (ROOT / p).is_file(), p
    assert O.IMPLEMENTATION_PATHS == tuple(sorted(paths))


def _oos_files(tmp_path, monkeypatch):
    gate, mask, man = _fixed(tmp_path, monkeypatch)
    dev = tmp_path / "dev.json"
    dev.write_text(json.dumps({"k_total": 16, "selected_for_oos": []}), encoding="utf-8")
    prep = tmp_path / "prep.json"
    prep.write_text(json.dumps({"targets": [{"label": "a", "k": 16}], "gates": {}, "target_status": {"a": "READY"},
                                "oos_events_sha256": "c" * 64, "dev_result_sha256": sha256_file(dev)}),
                    encoding="utf-8")
    omask = tmp_path / "omask.json"
    omask.write_text(json.dumps({"events_calendar_sha256": "c" * 64, "pairs": {}}), encoding="utf-8")
    snap = tmp_path / "snap.json"
    snap.write_text(json.dumps({"rows": {}}), encoding="utf-8")
    monkeypatch.setattr(O, "load_oos_calendar", lambda path: ({}, "c" * 64))
    monkeypatch.setattr(O, "prepare_dev", lambda args, purpose: ({}, {}, {}, RC.pair_map(), {}))
    common = ["run", "--db", str(tmp_path / "none.sqlite"), "--gate", str(gate), "--mask", str(mask),
              "--manifest", str(man), "--allow-dirty", "--out-dir", str(tmp_path / "out"), "--i-have-user-approval",
              "--dev-result", str(dev), "--dev-result-sha256", sha256_file(dev), "--prepare", str(prep),
              "--prepare-sha256", sha256_file(prep), "--oos-mask", str(omask), "--oos-mask-sha256", sha256_file(omask),
              "--oos-snapshot", str(snap), "--oos-snapshot-sha256", sha256_file(snap),
              "--snapshot-manifest-path", "x", "--snapshot-hash-field", "y"]
    return common


def test_oos_run_without_a_grant_for_a_target_is_refused(tmp_path, monkeypatch):
    from scripts import reversal_study_v1_oos
    argv = _oos_files(tmp_path, monkeypatch)
    with pytest.raises(StudyRefused, match="stays sealed"):
        reversal_study_v1_oos.main(argv)
    assert not (tmp_path / "out").exists()
    with pytest.raises(Exception):  # noqa: B017 - an artifact that cannot be verified must not open anything
        reversal_study_v1_oos.main([*argv, "--unlock-artifact", f"a={tmp_path / 'missing.json'}"])
    assert not (tmp_path / "out").exists()


def test_oos_prepare_refuses_without_the_oos_event_calendar(tmp_path, monkeypatch):
    from scripts import reversal_study_v1_oos
    gate, mask, man = _fixed(tmp_path, monkeypatch)
    monkeypatch.setattr(O, "prepare_dev", lambda args, purpose: ({}, {}, {}, RC.pair_map(), {}))
    dev = tmp_path / "dev.json"
    dev.write_text("{}", encoding="utf-8")
    argv = ["prepare", "--db", str(tmp_path / "none.sqlite"), "--gate", str(gate), "--mask", str(mask),
            "--manifest", str(man), "--out-dir", str(tmp_path / "out"), "--dev-result", str(dev),
            "--dev-result-sha256", sha256_file(dev), "--oos-events", str(tmp_path / "no_such_calendar.yaml")]
    with pytest.raises(StudyRefused, match="does not exist"):
        reversal_study_v1_oos.main(argv)


# ------------------------------------------------------------------ pipeline on synthetic days


SEG_A = Segment("A", date(2024, 1, 2), date(2024, 3, 28), date(2024, 2, 26), date(2024, 3, 28))
SEG_B = Segment("B", date(2024, 4, 1), date(2024, 6, 28), date(2024, 5, 1), date(2024, 6, 28))
SEGS = (SEG_A, SEG_B)
NAMES = {"P1": ("SMH", "SOXL", "SOXS"), "P2": ("QQQ", "TQQQ", "SQQQ")}


def random_data(seg, seed, names):
    ref, lo, sh = names
    rng = random.Random(seed)
    out = {s: SymbolData(s) for s in names}
    last = {ref: 400.0, lo: 50.0, sh: 20.0}
    lev = {ref: 1, lo: 3, sh: -3}
    for d in xcal.trading_days(seg.data_start, seg.data_end):
        iso = d.isoformat()
        jump = rng.gauss(0, 0.004)  # overnight gap
        move = rng.gauss(0, 0.0007)
        for sym, sd in out.items():
            p = last[sym] * (1 + lev[sym] * jump)
            pre_px = p
            bars = []
            for m in range(OPEN, xcal.regular_close_minute(d)):
                o = p
                p = o * (1 + lev[sym] * (move + rng.gauss(0, 0.0006)))
                bars.append(Bar(m, o, max(o, p) * 1.0001, min(o, p) * 0.9999, p, 1000.0 + rng.random() * 500))
            last[sym] = p
            sd.regular[iso] = bars
            sd.pre[iso] = [Bar(500, pre_px, pre_px, pre_px, pre_px, 10.0)]
            sd.labels[iso] = [b.t + 1 for b in bars]
    return out


@pytest.fixture(scope="module")
def world():
    cal = synth_cal()
    seg_data = {}
    masks = {}
    for pair, names in NAMES.items():
        a, b = random_data(SEG_A, 1 + len(pair), names), random_data(SEG_B, 5 + len(pair), names)
        for seg, data in ((SEG_A, a), (SEG_B, b)):
            seg_data.setdefault(seg.name, {}).update(data)
        masks[pair] = {"ref": names[0], "long": names[1], "short": names[2], "segments": {
            s.name: M.build_segment_mask(s, d, names[0], names[1:], cal) for s, d in ((SEG_A, a), (SEG_B, b))}}
    trades: dict = {}
    for pair in NAMES:
        trades.update(PL.collect_trades([c for c in CELLS if c.pair == pair], RL.evaluate, masks[pair], seg_data, SEGS))
    return masks, seg_data, trades


WINDOWS = {"T1": (576, 630), "T2": (587, 720), "T3": (661, 900), "T4": (602, 901)}


def test_collect_trades_follow_the_rules(world):
    _, seg_data, trades = world
    n_by_rule = {r: 0 for r in RC.RULES}
    for c in CELLS:
        recs = trades[c.k]
        n_by_rule[c.rule] += len(recs)
        lo, hi = WINDOWS[c.rule]
        for r in recs:
            assert lo <= r.entry_t <= hi
            assert r.res["close"]["exit_t"] == 955
            assert r.res["30m"]["exit_t"] == min(r.entry_t + 30, 955)
            assert r.res["60m"]["exit_t"] == min(r.entry_t + 60, 955)
        assert [r.day for r in recs] == sorted(r.day for r in recs)
    for pair in NAMES:  # long and short cells of a rule never trade the same day
        for rule in RC.RULES:
            lg, sh = (next(c for c in CELLS if (c.pair, c.rule, c.side) == (pair, rule, s)) for s in ("long", "short"))
            assert not {r.day for r in trades[lg.k]} & {r.day for r in trades[sh.k]}
    assert n_by_rule["T1"] > 0 and n_by_rule["T2"] > 0  # the synthetic gaps and breakouts produce signals
    # one trade recomputed by hand from the bars and the config costs
    c = next(c for c in CELLS if trades[c.k])
    r = trades[c.k][0]
    bars = {b.t: b for b in seg_data[r.seg][c.etf].regular[r.day]}
    base = RC.cfg()["costs"]["base_0.20"]
    pe, px = bars[r.entry_t].open, bars[955].open
    assert r.res["close"]["r"]["base_0.20"] == pytest.approx(px / pe * (1 - base["c"] - base["f"]) - (1 + base["c"]))
    stress = RC.cfg()["costs"]["stress_0.30"]
    assert r.res["close"]["r"]["stress_0.30"] == pytest.approx(
        px * (1 - stress["s"]) / (pe * (1 + stress["s"])) * (1 - stress["c"] - stress["f"]) - (1 + stress["c"]))


def test_precount_matches_the_trade_lists_and_power_table(world):
    masks, seg_data, trades = world
    for pair in NAMES:
        counts = PC.count_pair(pair, CELLS, masks[pair], seg_data, SEGS)
        for c in (c for c in CELLS if c.pair == pair):
            got = counts["signals"].get(f"{c.rule}:{c.side}", {"A": 0, "B": 0, "event_days": 0})
            assert got["A"] + got["B"] == len(trades[c.k])
            assert got["event_days"] == sum(r.event_day for r in trades[c.k])
        for key, dist in counts["entry_time"].items():
            lo, hi = WINDOWS[key.split(":")[0]]
            assert lo <= dist["min"] <= dist["q1"] <= dist["median"] <= dist["q3"] <= dist["max"] <= hi
        d = counts["diagnostics"]
        scored = sum(len(m["scored"]) for m in masks[pair]["segments"].values())
        assert all(d[r]["scored_days"] == scored for r in RC.RULES)
        assert d["T2"]["failed_up"] <= d["T2"]["breakout_up"] and d["T2"]["failed_down"] <= d["T2"]["breakout_down"]
        assert d["T4"]["opposite_breakouts"] <= d["T4"]["first_breakouts"]
        assert len(PC.power_table(pair, counts)) == 8
    # the document's worked power examples (K = 16): P1 n = 100 -> 2.06%, n = 50 -> 2.91%; P2 n = 100 -> 1.27%
    fake = {"signals": {"T1:long": {"A": 60, "B": 40}, "T1:short": {"A": 25, "B": 25}}}
    rows = {r["cell"]: r for r in PC.power_table("P1", fake)}
    assert rows["P1:T1:long"]["close"]["delta_alpha_0.05_over_K"] == pytest.approx(0.0206, abs=1e-4)
    assert rows["P1:T1:short"]["close"]["delta_alpha_0.05_over_K"] == pytest.approx(0.0291, abs=1e-4)
    assert rows["P1:T1:long"]["close"]["delta_alpha_0.05"] == pytest.approx(2.486 * 0.0575 / 10, abs=2e-4)
    assert PC.power_table("P2", fake)[0]["close"]["delta_alpha_0.05_over_K"] == pytest.approx(0.0127, abs=1e-4)
    assert PC.power_table("P1", fake)[2]["close"]["delta_alpha_0.05"] is None  # n = 0 row


def test_designated_cell_trades_only_its_own_side(world):
    from src.entry_study_v1 import rules as ERL
    masks, seg_data, _ = world
    cell_a = RC.load_designated()
    out = PL.collect_trades([cell_a], ERL.evaluate, masks["P1"], seg_data, SEGS)
    assert set(out) == {16}
    entry = pair_trades("P1", ENTRY_CELLS, masks["P1"], seg_data, SEGS)["main"][2]  # the entry study's own cell k = 2
    assert [(r.day, r.entry_t) for r in out[16]] == [(r.day, r.entry_t) for r in entry]
    assert all(r.entry_t == 585 for r in out[16])
    # the entry study's fee_only_0.20 cost set is the reversal base set
    assert [r.res["close"]["r"]["base_0.20"] for r in out[16]] == pytest.approx(
        [r.res["close"]["r"]["fee_only_0.20"] for r in entry])


# ------------------------------------------------------------------ overlay on the entry study's trades + harness


@pytest.fixture(scope="module")
def entry_world(world):
    masks, seg_data, _ = world
    et = {"P2": pair_trades("P2", ENTRY_CELLS, masks["P2"], seg_data, SEGS)}
    dev = {"cells": [json.loads(json.dumps(ENTRY_DEVRUN.cell_report(c, et["P2"]["main"][c.k], 50)))
                     for c in ENTRY_CELLS if c.pair == "P2"]}
    return et, dev


def _params():
    return {c.rule: c.params for c in CELLS}


def test_reproduction_harness_compares_listed_fields_exactly(world, entry_world):
    _, _, _ = world
    et, dev = entry_world
    assert PL.reproduce_entry_baseline(ENTRY_CELLS, et, dev) == []
    for field, mutate in {
        "mean_base": lambda row: row.__setitem__("mean_base", row["mean_base"] * (1 + 1e-12)),
        "mean_stress": lambda row: row.__setitem__("mean_stress", row["mean_stress"] + 1e-15),
        "median_base": lambda row: row.__setitem__("median_base", row["median_base"] + 1e-12),
        "main.n": lambda row: row["main"].__setitem__("n", row["main"]["n"] + 1),
        "main.n_by_segment": lambda row: row["main"]["n_by_segment"].__setitem__("A", 999),
        "main.mean_by_segment": lambda row: row["main"]["mean_by_segment"].__setitem__("B", 0.123),
    }.items():
        bad = copy.deepcopy(dev)
        row = next(r for r in bad["cells"] if r["main"]["n"] > 0 and r["k"] == 12) if field != "x" else None
        mutate(row)
        msgs = PL.reproduce_entry_baseline(ENTRY_CELLS, et, bad)
        assert len(msgs) == 1 and msgs[0].startswith(f"k=12: {field} "), (field, msgs)
    missing = {"cells": [r for r in dev["cells"] if r["k"] != 15]}
    assert PL.reproduce_entry_baseline(ENTRY_CELLS, et, missing) == ["k=15: missing in dev_result"]


def test_overlay_report_is_withheld_unless_the_baseline_reproduces(world, entry_world):
    _, seg_data, _ = world
    et, dev = entry_world
    p = _params()
    ok = PL.overlay_report(ENTRY_CELLS, et, dev, seg_data, SEGS, p["T3"], p["T4"])
    assert ok["reported"] and len(ok["cells"]) == 10
    bad = copy.deepcopy(dev)
    bad["cells"][3]["mean_base"] += 1e-9
    withheld = PL.overlay_report(ENTRY_CELLS, et, bad, seg_data, SEGS, p["T3"], p["T4"])
    assert not withheld["reported"] and withheld["mismatches"] and "cells" not in withheld


def test_overlay_cells_compare_overlay_exit_with_hold_to_close(world, entry_world):
    _, seg_data, _ = world
    et, _dev = entry_world
    p = _params()
    seen = 0
    for c in (c for c in ENTRY_CELLS if c.pair == "P2"):
        recs = et["P2"]["main"][c.k]
        out = PL.overlay_cell(c, recs, seg_data, SEGS, p["T3"], p["T4"])
        assert out["n"] == len(recs)
        if not recs:
            continue
        seen += 1
        assert sum(out[f"share_{x}"] for x in ("T3", "T4", "both", "close")) == pytest.approx(1.0)
        # hold-to-close under the 0.20% set equals the entry study's fee_only_0.20 set (same c, s = 0, f)
        assert out["mean_close_base_0.20"] == pytest.approx(
            float(np.mean([r.res["close"]["r"]["fee_only_0.20"] for r in recs])))
        assert out["mean_diff_base_0.20"] == pytest.approx(out["mean_overlay_base_0.20"] - out["mean_close_base_0.20"])
        assert out["median_hold_min"] >= 1
    assert seen >= 5
