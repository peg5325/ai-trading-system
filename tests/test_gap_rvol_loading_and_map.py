from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from src.research.gap_rvol import loading
from src.research.gap_rvol.contract import PREREG_JSON_SHA256, PREREG_MD_SHA256

PACKAGE = Path("src/research/gap_rvol")
RUNNER = Path("scripts/run_gap_rvol_development.py")
D9_AUDIT_RUNNER = Path("scripts/audit_gap_rvol_d9.py")
MAP = Path("docs/research/PREREG_P0_US30_GAP_RVOL_V1_IMPLEMENTATION_MAP.md")


def test_performance_package_and_runner_have_no_loader_bypass():
    for path in (*PACKAGE.glob("*.py"), RUNNER, D9_AUDIT_RUNNER):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "src.history.loader"
            if isinstance(node, ast.Attribute):
                assert node.attr != "load_candles"
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    assert not (
                        keyword.arg == "purpose"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value == "coverage"
                    )
                    assert keyword.arg != "unlock"


def test_dynamic_loader_is_performance_intraday_and_propagates_oos_denial(monkeypatch):
    captured = {}

    def deny(*args, **kwargs):
        captured.update(kwargs)
        raise PermissionError("blind OOS denied")

    monkeypatch.setattr(loading, "research_load", deny)
    with pytest.raises(PermissionError, match="blind OOS denied"):
        loading.load_symbol_day("/not-opened.sqlite", "AAPL", date(2026, 2, 2))
    assert captured["purpose"] == "performance"
    assert captured["track"] == "intraday"


def test_package_has_no_runner_result_or_oos_unlock_artifact():
    names = {path.name for path in PACKAGE.glob("*.py")}
    assert "oos.py" not in names
    assert "runner.py" not in names
    assert {"development.py", "manifest.py", "report.py", "stats.py"} <= names
    assert RUNNER.is_file()
    assert D9_AUDIT_RUNNER.is_file()
    allowed_scripts = {
        "audit_gap_rvol_d9.py",
        "run_gap_rvol_development.py",
    }
    forbidden_scripts = [
        path for path in Path("scripts").glob("*gap*rvol*")
        if path.name not in allowed_scripts
    ]
    assert forbidden_scripts == []
    results = {
        path.name for path in Path("docs/research").glob("RESULT*GAP*RVOL*")
    }
    assert results == {
        "RESULT_P0_US30_GAP_RVOL_DEVELOPMENT.json",
        "RESULT_P0_US30_GAP_RVOL_DEVELOPMENT.md",
    }
    assert not Path(
        "docs/research/OOS_UNLOCK_P0_US30_GAP_RVOL.json"
    ).exists()


def test_implementation_map_tracks_frozen_hashes_and_required_sections():
    text = MAP.read_text(encoding="utf-8")
    assert PREREG_MD_SHA256 in text
    assert PREREG_JSON_SHA256 in text
    for section in (
        "gap/RVOL/VWAP",
        "pullback armed",
        "retention",
        "strict reclaim",
        "next-bar fill",
        "60분 time stop",
        "EOD/조기폐장",
        "입력 순서·중복 불변",
        "동시 후보 경쟁",
        "OOS",
    ):
        assert section in text
