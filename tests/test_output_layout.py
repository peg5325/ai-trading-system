"""src/output_layout.py: folder rules, the old-layout classifier, and agreement with every writer."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src import output_layout as L
from src.freeze_paper.evaluator import evaluation_paths
from src.freeze_paper.gates import freeze_paths
from src.freeze_paper.runner import paper_paths
from src.premarket.collection import collection_paths
from src.premarket.launchd import jobs, plist_payload
from src.premarket.operations import STAGES, postclose_paths, stage_paths
from src.runtime.config import RuntimeConfig

DAY = date(2026, 10, 13)


@pytest.mark.parametrize("name, expected", [
    ("premarket-daily-brief.20260915.freeze.v2.corrected.md",
     "reports/premarket/days/20260915/premarket-daily-brief.20260915.freeze.v2.corrected.md"),
    ("premarket-provider-bundle.20261013.primary.json.receipt.json",
     "reports/premarket/days/20261013/premarket-provider-bundle.20261013.primary.json.receipt.json"),
    ("premarket-operation.20261013.postclose.json",
     "reports/premarket/days/20261013/premarket-operation.20261013.postclose.json"),
    ("premarket-freeze-paper.20261013.drift.json",
     "reports/premarket/days/20261013/premarket-freeze-paper.20261013.drift.json"),
    ("premarket-postclose-ledger.json", "reports/premarket/ledgers/premarket-postclose-ledger.json"),
    ("premarket-postclose-cumulative.md", "reports/premarket/ledgers/premarket-postclose-cumulative.md"),
    ("premarket-freeze-paper-ledger.json", "reports/premarket/ledgers/premarket-freeze-paper-ledger.json"),
    ("premarket-freeze-paper-evaluation.interim.20261001_20261009.json",
     "reports/premarket/ledgers/premarket-freeze-paper-evaluation.interim.20261001_20261009.json"),
    ("premarket-operation-logs", "reports/premarket/logs"),
    ("premarket-launchd", "reports/premarket/launchd"),
    ("history-collection-logs", "reports/history-collection/logs"),
    ("daytrade_paper", "reports/daytrade/paper"),
    ("daytrade_shadow", "reports/daytrade/shadow"),
    ("daytrade_v11", "reports/daytrade/v11"),
    ("daytrade_v13", "reports/daytrade/v13"),
    ("explore_v3c_asym_swing", "reports/research/explore_v3c_asym_swing"),
    ("daily_screen_v1", "reports/research/daily_screen_v1"),
    ("phase1a_probe", "reports/research/phase1a_probe"),
    ("h3b_longhorizon_validation.json", "reports/research/h2-h4/h3b_longhorizon_validation.json"),
    ("WORKORDER_H3B_LONGHORIZON.md", "reports/research/h2-h4/WORKORDER_H3B_LONGHORIZON.md"),
    ("score_correlation.md", "reports/research/h2-h4/score_correlation.md"),
    ("p0_us30_1m_bulk.p0-us30-1m-20260721-r82.stderr.log",
     "reports/data-ops/p0_us30_1m_bulk.p0-us30-1m-20260721-r82.stderr.log"),
    ("P0_US30_ORB_CLEAN_SNAPSHOT_RECEIPT.json", "reports/data-ops/P0_US30_ORB_CLEAN_SNAPSHOT_RECEIPT.json"),
    ("history_external_replica_receipt.20260722T160829Z.json",
     "reports/data-ops/history_external_replica_receipt.20260722T160829Z.json"),
    ("raw_snapshot_manifest.p0-us30-1m-20260721-r82.json",
     "reports/data-ops/raw_snapshot_manifest.p0-us30-1m-20260721-r82.json"),
    ("SESSION_007_FINAL.md", "reports/archive/2026-07_paper_sessions/SESSION_007_FINAL.md"),
    ("paper_report_unknown.md", "reports/archive/2026-07_paper_sessions/paper_report_unknown.md"),
    ("krx_exit_test_session004_system_review.json",
     "reports/archive/2026-07_paper_sessions/krx_exit_test_session004_system_review.json"),
    ("us_reviews_session001.jsonl", "reports/archive/2026-07_paper_sessions/us_reviews_session001.jsonl"),
    ("phase6_us_baseline_launch_preflight.md",
     "reports/archive/2026-07_paper_sessions/phase6_us_baseline_launch_preflight.md"),
    ("NEXT_SESSION_IMPROVEMENTS.md", "reports/archive/2026-07_paper_sessions/NEXT_SESSION_IMPROVEMENTS.md"),
])
def test_classify_report_entries(name, expected):
    assert L.classify(f"reports/{name}", is_dir=False) == expected


@pytest.mark.parametrize("name", ["premarket", "daytrade", "research", "archive", "data-ops",
                                  "history-collection", "MOVES.json"])
def test_layout_entries_are_not_reclassified(name):
    assert L.classify(f"reports/{name}", is_dir=True) is None


@pytest.mark.parametrize("name", ["notes.txt", "us_notes.md", "premarket-unknown.json", "RESAMPLER_PARITY_RECEIPT.json"])
def test_unknown_report_entries_stay_unclassified(name):
    assert L.classify(f"reports/{name}", is_dir=False) is None


@pytest.mark.parametrize("name, is_dir, expected", [
    ("logs", True, "runs/paper_2026-07/default/logs"),
    ("status.json", False, "runs/paper_2026-07/default/status.json"),
    ("logs_us_phase6_s001", True, "runs/paper_2026-07/us_phase6_s001/logs"),
    ("status_us_phase6_s001.json", False, "runs/paper_2026-07/us_phase6_s001/status.json"),
    ("logs", False, None),               # a file named logs is not a log folder
    ("status_x.json", True, None),
    ("data", True, None),
    ("datasets", True, None),
    ("reports", True, None),
    ("runs", True, None),
])
def test_classify_root_entries(name, is_dir, expected):
    assert L.classify(name, is_dir) == expected


def test_deeper_paths_are_never_classified():
    assert L.classify("reports/premarket/days/20261013/premarket-operation.20261013.freeze.json", False) is None
    assert L.classify("data/logs", True) is None


def test_day_key_rejects_malformed_days():
    assert L.premarket_day_relpath("2026-10-13") == Path("premarket/days/20261013")
    with pytest.raises(ValueError):
        L.premarket_day_relpath("2026-1-3")


def _assert_classified_in_place(root: Path, path: Path) -> None:
    """A file the writer produces must be one the migration classifier puts in the same place."""
    rel = path.relative_to(root).as_posix()
    assert L.classify(f"reports/{path.name}", path.is_dir()) == rel, rel


def test_writers_agree_with_the_classifier(tmp_path):
    for stage in STAGES:
        paths = stage_paths(tmp_path, DAY, stage)
        for key in ("provider", "market", "input", "brief", "brief_markdown", "operation"):
            _assert_classified_in_place(tmp_path, paths[key])
    post = postclose_paths(tmp_path, DAY)
    for key in ("evaluation", "evaluation_markdown", "ledger", "cumulative", "cumulative_markdown", "operation"):
        _assert_classified_in_place(tmp_path, post[key])
    paper = paper_paths(tmp_path, DAY)
    for key in ("session", "session_markdown", "drift", "alert", "ledger", "operation"):
        _assert_classified_in_place(tmp_path, paper[key])
    for label in ("FINAL", "INTERIM"):
        evaluation = {"label": label, "window": {"first_session": "2026-10-01", "end_close_date": "2026-10-14"}}
        for path in evaluation_paths(tmp_path, evaluation):
            _assert_classified_in_place(tmp_path, path)


def test_writer_folders_come_from_the_layout(tmp_path):
    assert stage_paths(tmp_path, DAY, "FREEZE")["brief"].parent == L.premarket_day_dir(tmp_path, DAY)
    assert freeze_paths(DAY, L.reports_dir(tmp_path)) == (
        stage_paths(tmp_path, DAY, "FREEZE")["brief"], stage_paths(tmp_path, DAY, "FREEZE")["operation"])
    assert paper_paths(tmp_path, DAY)["logs"] == L.premarket_logs_dir(tmp_path) / "20261013.paper"
    collection = collection_paths(tmp_path, DAY)
    assert collection["operation"] == tmp_path / "reports/history-collection/20261013.operation.json"
    assert collection["logs"] == tmp_path / "reports/history-collection/logs/20261013"
    job = jobs(tmp_path, Path("/usr/bin/python3"))[0]
    assert Path(plist_payload(job, tmp_path)["StandardOutPath"]).parent == tmp_path / "reports/premarket/launchd"


def test_runtime_defaults_live_under_runs():
    for cfg in (RuntimeConfig(), RuntimeConfig.from_dict({})):
        assert (cfg.log_dir, cfg.status_path, cfg.reports_dir) == (
            "runs/paper/default/logs", "runs/paper/default/status.json", "runs/paper/default/reports")


def test_runtime_yaml_ops_paths_live_under_runs():
    import yaml

    root = Path(__file__).resolve().parents[1]
    for path in sorted((root / "config").glob("runtime*.yaml")):
        cfg = RuntimeConfig.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))
        run = Path(cfg.log_dir).parent
        assert run.parent == Path("runs/paper_2026-07"), path.name
        assert Path(cfg.status_path) == run / "status.json", path.name
        assert Path(cfg.reports_dir) == run / "reports", path.name
