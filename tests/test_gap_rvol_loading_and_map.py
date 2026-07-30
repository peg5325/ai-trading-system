from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

from src.research.gap_rvol import loading
from src.research.gap_rvol.contract import PREREG_JSON_SHA256, PREREG_MD_SHA256

PACKAGE = Path("src/research/gap_rvol")
MAP = Path("docs/research/PREREG_P0_US30_GAP_RVOL_V1_IMPLEMENTATION_MAP.md")


def test_performance_package_has_no_direct_loader_or_coverage_bypass():
    for path in PACKAGE.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "src.history.loader"
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    assert not (
                        keyword.arg == "purpose"
                        and isinstance(keyword.value, ast.Constant)
                        and keyword.value.value == "coverage"
                    )


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
    assert not {"runner.py", "report.py", "oos.py"} & names
    assert not list(Path("scripts").glob("*gap*rvol*"))
    assert not list(Path("docs/research").glob("RESULT*GAP*RVOL*"))


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
