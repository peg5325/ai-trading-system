"""Long-history daily data foundation (data only: collect, normalize, quality-check, guarded load).

No strategy returns or signal performance are computed in this package. Data files live outside git
under `data/external/longhist/` of the main checkout; `manifest.json` there pins every file by SHA-256.
"""
from __future__ import annotations

import os
from pathlib import Path


def default_root() -> Path:
    """Data root: $LONGHIST_DIR, else <main checkout>/data/external/longhist (worktrees map to main)."""
    env = os.environ.get("LONGHIST_DIR")
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[2]
    parts = repo.parts
    if ".claude" in parts:  # .../<main>/.claude/worktrees/<name> -> <main>
        repo = Path(*parts[: parts.index(".claude")])
    return repo / "data" / "external" / "longhist"
