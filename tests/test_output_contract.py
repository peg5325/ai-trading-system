"""Full output contract vs regression projection (closeout 2026-10-19 §4). Synthetic fixtures only."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_daytrade_paper import EXT_OK, NO_EVENTS, PREV, PRIOR, QC, s1_day
from test_daytrade_shadow import gap_stop_day

from src.daytrade_paper import rules as R
from src.daytrade_v13 import output_contract as oc
from src.daytrade_v13.day import V10_TRADE_KEYS
from src.daytrade_v13.policy import COST_MODELS, V13
from src.daytrade_v13.report import NOT_EVALUATED, PASS
from src.daytrade_v13.runio import config_hash, run_record
from src.daytrade_v13.runner import run_arm

ROOT = Path(__file__).resolve().parents[1]
NEW_FIELDS = ("timing", "cost_breakdown")


def _di(build):
    reg, pre = build()
    return {"day": "2026-10-05", "regular": reg, "qqq_pre": pre, "prior": PRIOR, "external": EXT_OK,
            "events": NO_EVENTS, "prev_times": QC["prev_times"], "expected_prev": QC["expected_prev"],
            "prev_early_close": False}


def synthetic_arm(costs=COST_MODELS["CM030_v1"]):
    seg_days = {"A": [_di(s1_day), _di(lambda: s1_day(qqq_after=lambda t: PREV * 1.03))],
                "B": [_di(gap_stop_day)]}
    return run_arm(seg_days, V13, costs, 0)


def synthetic_doc():
    arm = synthetic_arm()
    rec = run_record(run_id="synthetic-1", run_type="ENGINE_REPRO", config_hash=config_hash(V13, COST_MODELS["CM030_v1"], 0),
                     cost_model_ids=["CM030_v1"])
    return oc.build_full_output(arm, label=oc.SYNTHETIC_LABEL, run_record=rec,
                                mask={"sha256": None, "quality_conditional": True, "note": "synthetic days, no mask"})


def test_full_output_survives_serialization_with_every_contract_field():
    doc = synthetic_doc()
    back = oc.loads(oc.dumps(doc))
    assert back == json.loads(json.dumps(doc))
    assert oc.missing_fields(back) == []
    assert back["label"] == "SYNTHETIC_OUTPUT_CONTRACT_EXAMPLE"
    assert back["result_kind"] == "account_sim" and back["cost_model_id"] == "CM030_v1"
    trades = [t for acc in back["accounts"].values() for d in acc["days"] for t in d["trades"]]
    assert len(trades) == 4
    for t in trades:
        assert t["timing"]["sizing_price_known_at_t"] == t["timing"]["fill_t"] == t["entry_t"]
        assert "not verified" in t["timing"]["fill_assumption"]
        cb = t["cost_breakdown"]
        for k in ("reference_price_pnl", "executed_price_pnl", "slippage_impact", "explicit_fees", "net_pnl"):
            assert k in cb
    for acc in back["accounts"].values():
        assert acc["result_kind"] == "account_sim" and acc["cost_model_id"] == "CM030_v1"
        assert acc["start_equity"] == R.START_EQUITY  # each segment account starts again at USD 2,000


def test_aggregation_meaning_is_written_and_matches_the_numbers():
    doc = synthetic_doc()
    rs = [a["R"] for a in doc["accounts"].values()]
    assert doc["G"] == sum(rs) / len(rs)
    assert doc["D"] == max(a["D"] for a in doc["accounts"].values())
    agg = doc["aggregation"]
    assert "not the return of one continuous account and not a CAGR" in agg["G"]
    assert "reset per segment" in agg["G"] and "quality mask" in agg["days"]


def test_unmeasured_axes_stay_not_evaluated_after_a_round_trip():
    back = oc.loads(oc.dumps(synthetic_doc()))
    axes = back["verdict_axes"]["axes"]
    assert axes["accounting"]["status"] == PASS and axes["accounting"]["trades_checked"] == 4
    for k in ("data_validity", "causality", "economics", "statistics", "contract_match", "sample"):
        assert axes[k]["status"] == NOT_EVALUATED
    assert back["verdict_axes"]["engine_validation_is_not_strategy_validation"] is True
    rr = back["run_record"]
    assert rr["run_id"] == "synthetic-1" and len(rr["config_hash"]) == 64
    assert rr["family_id"] == {"value": None, "unknown_reason": "not supplied by the caller"}


def test_a_trade_without_timing_is_reported_missing():
    doc = oc.loads(oc.dumps(synthetic_doc()))
    del doc["accounts"]["A"]["days"][0]["trades"][0]["timing"]
    assert oc.missing_fields(doc) == ["A.2026-10-05.trade0.timing"]


def _compute():
    spec = importlib.util.spec_from_file_location("eval_repro_v13_compute", ROOT / "scripts" / "eval_repro_v13_compute.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_regression_projection_drops_the_reporting_fields_on_purpose():
    """Why post-CM030-*.json had no timing/cost fields: `projection` keeps only the v1.0 trade keys."""
    proj = _compute().projection(synthetic_arm(), V10_TRADE_KEYS)
    t = proj["A"]["days"][0]["trades"][0]
    assert set(t) == set(V10_TRADE_KEYS)
    assert not set(NEW_FIELDS) & set(t) and "result_kind" not in proj["A"]


def test_compute_full_out_writer_produces_a_complete_contract(tmp_path):
    comp = _compute()
    mask = tmp_path / "mask.json"
    mask.write_text('{"schema": "daytrade_v11_mask/v1"}', encoding="utf-8")
    args = SimpleNamespace(full_out=tmp_path / "x.full.json", run_key="synthetic-run", delay=0, mask=mask)
    arm = synthetic_arm()
    comp._write_full(args, {"segments": arm["segments"], "G": arm["G"], "D": arm["D"]}, V13,
                     COST_MODELS["CM030_v1"], {"schema": "daytrade_v11_mask/v1"})
    doc = json.loads((tmp_path / "x.full.json").read_text(encoding="utf-8"))
    assert oc.missing_fields(doc) == []
    assert doc["label"] == "ENGINE_REPRO" and doc["mask"]["quality_conditional"] is True
    assert doc["run_record"]["mask_sha256"] == doc["mask"]["sha256"]
    with pytest.raises(FileExistsError):
        comp._write_full(args, {"segments": arm["segments"], "G": arm["G"], "D": arm["D"]}, V13,
                         COST_MODELS["CM030_v1"], {})
