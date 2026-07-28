from __future__ import annotations

import importlib
import re
from pathlib import Path

PREREG = Path("docs/research/PREREG_P0_US30_ORB_V1.md")
IMPLEMENTATION_MAP = Path(
    "docs/research/PREREG_P0_US30_ORB_V1_IMPLEMENTATION_MAP.md")


def _table_rows(text: str, *, section_pattern: str) -> list[list[str]]:
    match = re.search(section_pattern, text, flags=re.DOTALL)
    assert match, f"section not found: {section_pattern}"
    rows = []
    for line in match.group(1).splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] in {"조항", "---"}:
            continue
        if all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        rows.append(cells)
    return rows


def _prereg_rows() -> list[list[str]]:
    return _table_rows(
        PREREG.read_text(encoding="utf-8"),
        section_pattern=r"## 15\..*?\n(.*?)(?:\n---\n|\n## 16\.)",
    )


def _map_rows(text: str | None = None) -> list[list[str]]:
    source = IMPLEMENTATION_MAP.read_text(encoding="utf-8") if text is None else text
    return _table_rows(source, section_pattern=r"\| 조항 \|.*?\n(.*)\Z")


def _module_from_path(path: str) -> str:
    assert path.endswith(".py")
    return path[:-3].replace("/", ".")


def test_mapping_has_exactly_the_same_clause_sequence_as_prereg_section_15():
    assert [row[0] for row in _map_rows()] == [row[0] for row in _prereg_rows()]


def test_every_mapping_row_has_five_non_placeholder_cells():
    for row in _map_rows():
        assert len(row) == 5
        assert all(cell and cell not in {"-", "`-`", "TBD"} for cell in row)


def test_every_implementation_reference_imports_and_symbol_exists():
    for row in _map_rows():
        reference = row[2].strip("`")
        path, symbol = reference.split(":", 1)
        module = importlib.import_module(_module_from_path(path))
        assert hasattr(module, symbol), reference


def test_every_test_reference_imports_and_function_exists():
    for row in _map_rows():
        reference = row[3].strip("`")
        path, function = reference.split(":", 1)
        module = importlib.import_module(_module_from_path(path))
        assert callable(getattr(module, function, None)), reference


def test_deleting_one_mapping_row_is_detected():
    lines = IMPLEMENTATION_MAP.read_text(encoding="utf-8").splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("| §10 |"))
    mutated = "\n".join(lines[:index] + lines[index + 1:]) + "\n"
    assert [row[0] for row in _map_rows(mutated)] != [row[0] for row in _prereg_rows()]
