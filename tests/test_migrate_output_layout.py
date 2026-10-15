"""scripts/migrate_output_layout.py: dry run, apply, MOVES.json append, idempotence, conflict abort."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "migrate_output_layout", Path(__file__).resolve().parents[1] / "scripts" / "migrate_output_layout.py")
M = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = M   # dataclasses resolve the module by name while the script loads
_SPEC.loader.exec_module(M)

NOW = datetime(2026, 10, 14, 1, 2, 3, tzinfo=timezone.utc)


def _touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def old_layout(tmp_path):
    r = tmp_path / "reports"
    _touch(r / "premarket-daily-brief.20261013.freeze.json", "brief")
    _touch(r / "premarket-postclose-ledger.json")
    _touch(r / "premarket-operation-logs" / "20261013.freeze" / "brief.log")
    _touch(r / "premarket-launchd" / "freeze-22.stdout.log")
    _touch(r / "history-collection" / "20261013.operation.json")
    _touch(r / "history-collection-logs" / "20261013" / "history_tail_repair.log")
    _touch(r / "daytrade_paper" / "2026-10-13.json")
    _touch(r / "explore_v3_asym_swing" / "out.json")
    _touch(r / "h4_screener_ranking.json")
    _touch(r / "P0_US30_ORB_CLEAN_SNAPSHOT_RECEIPT.json")
    _touch(r / "SESSION_007_FINAL.md")
    _touch(r / "mystery.txt")
    _touch(r / ".DS_Store")
    _touch(tmp_path / "logs" / "runtime.log")
    _touch(tmp_path / "status.json", "{}")
    _touch(tmp_path / "logs_us" / "trade.log")
    _touch(tmp_path / "status_us.json", "{}")
    _touch(tmp_path / "data" / "logs_keep" / "a.sqlite")
    _touch(tmp_path / "datasets" / "status_x.json")
    return tmp_path


def _tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_dry_run_plans_without_touching_anything(old_layout, capsys):
    before = _tree(old_layout)
    assert M.main(["--root", str(old_layout)]) == 0
    out = capsys.readouterr().out
    assert _tree(old_layout) == before
    assert "MOVE reports/premarket-daily-brief.20261013.freeze.json -> " \
           "reports/premarket/days/20261013/premarket-daily-brief.20261013.freeze.json" in out
    assert "MOVE logs_us -> runs/paper_2026-07/us/logs" in out
    assert "UNCLASSIFIED reports/mystery.txt" in out
    assert "# planned moves: 14" in out
    assert ".DS_Store" not in out and "data/" not in out.replace("data-ops/", "")


def test_apply_moves_records_and_is_idempotent(old_layout):
    plan = M.build_plan(old_layout)
    assert [p.name for p in plan.unclassified] == ["mystery.txt"]
    done = M.apply_plan(old_layout, plan, now=NOW)
    assert len(done) == 14
    tree = _tree(old_layout)
    for expected in (
        "reports/premarket/days/20261013/premarket-daily-brief.20261013.freeze.json",
        "reports/premarket/ledgers/premarket-postclose-ledger.json",
        "reports/premarket/logs/20261013.freeze/brief.log",
        "reports/premarket/launchd/freeze-22.stdout.log",
        "reports/history-collection/20261013.operation.json",
        "reports/history-collection/logs/20261013/history_tail_repair.log",
        "reports/daytrade/paper/2026-10-13.json",
        "reports/research/explore_v3_asym_swing/out.json",
        "reports/research/h2-h4/h4_screener_ranking.json",
        "reports/data-ops/P0_US30_ORB_CLEAN_SNAPSHOT_RECEIPT.json",
        "reports/archive/2026-07_paper_sessions/SESSION_007_FINAL.md",
        "runs/paper_2026-07/default/logs/runtime.log",
        "runs/paper_2026-07/default/status.json",
        "runs/paper_2026-07/us/logs/trade.log",
        "runs/paper_2026-07/us/status.json",
        "reports/mystery.txt",
        "data/logs_keep/a.sqlite",
        "datasets/status_x.json",
    ):
        assert expected in tree, expected
    ledger = json.loads((old_layout / "reports" / "MOVES.json").read_text())
    assert ledger == done
    assert {"old": "logs_us", "new": "runs/paper_2026-07/us/logs",
            "moved_at_utc": "2026-10-14T01:02:03Z"} in ledger
    again = M.build_plan(old_layout)
    assert again.moves == [] and again.conflicts == []
    assert M.apply_plan(old_layout, again) == []
    assert json.loads((old_layout / "reports" / "MOVES.json").read_text()) == ledger


def test_existing_destination_folder_is_merged_and_ledger_appended(old_layout):
    M.apply_plan(old_layout, M.build_plan(old_layout), now=NOW)
    first = json.loads((old_layout / "reports" / "MOVES.json").read_text())
    # an old-layout writer ran once more after the migration
    _touch(old_layout / "reports" / "premarket-operation-logs" / "20261014.primary" / "provider.log")
    _touch(old_layout / "reports" / "premarket-operation-logs" / ".DS_Store")
    plan = M.build_plan(old_layout)
    assert [(s.relative_to(old_layout).as_posix(), d.relative_to(old_layout).as_posix()) for s, d in plan.moves] \
        == [("reports/premarket-operation-logs/20261014.primary", "reports/premarket/logs/20261014.primary")]
    M.apply_plan(old_layout, plan, now=NOW)
    ledger = json.loads((old_layout / "reports" / "MOVES.json").read_text())
    assert ledger[:len(first)] == first and len(ledger) == len(first) + 1
    assert (old_layout / "reports/premarket/logs/20261014.primary/provider.log").is_file()
    assert not (old_layout / "reports" / "premarket-operation-logs").exists()   # .DS_Store alone does not keep it
    assert M.build_plan(old_layout).moves == []


def test_conflict_aborts_before_any_move(old_layout, capsys):
    _touch(old_layout / "reports/premarket/days/20261013/premarket-daily-brief.20261013.freeze.json", "other")
    before = _tree(old_layout)
    assert M.main(["--root", str(old_layout), "--apply"]) == 1
    out = capsys.readouterr().out
    assert "CONFLICT destination exists" in out and "nothing moved" in out
    assert _tree(old_layout) == before
    assert not (old_layout / "reports" / "MOVES.json").exists()
    with pytest.raises(RuntimeError):
        M.apply_plan(old_layout, M.build_plan(old_layout))


def test_ledger_must_be_a_list(old_layout):
    _touch(old_layout / "reports" / "MOVES.json", "{}")
    before = _tree(old_layout)
    with pytest.raises(TypeError):
        M.apply_plan(old_layout, M.build_plan(old_layout), now=NOW)
    assert _tree(old_layout) == before
