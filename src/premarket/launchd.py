"""macOS launchd job definitions for the local premarket operation runner."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

LABEL_PREFIX = "com.peg5325.ai-trading.premarket"


@dataclass(frozen=True)
class LaunchdJob:
    name: str
    hour: int
    minute: int
    arguments: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{LABEL_PREFIX}.{self.name}"


def jobs(project_root: Path, python: Path) -> tuple[LaunchdJob, ...]:
    runner = project_root / "scripts" / "run_premarket_pipeline.py"
    return (
        # New York's daylight-saving offset changes the equivalent KST hour.
        # The runner validates the actual NYSE-relative window, so each paired
        # trigger has one safe no-op and one real execution depending on season.
        LaunchdJob("primary-21", 21, 1, (str(python), str(runner), "--stage", "PRIMARY")),
        LaunchdJob("primary-22", 22, 1, (str(python), str(runner), "--stage", "PRIMARY")),
        LaunchdJob("verification-22", 22, 1, (str(python), str(runner), "--stage", "VERIFICATION")),
        LaunchdJob("verification-23", 23, 1, (str(python), str(runner), "--stage", "VERIFICATION")),
        LaunchdJob("freeze-22", 22, 11, (str(python), str(runner), "--stage", "FREEZE")),
        LaunchdJob("freeze-23", 23, 11, (str(python), str(runner), "--stage", "FREEZE")),
        LaunchdJob("postclose-05", 5, 20, (str(python), str(runner), "--postclose")),
        LaunchdJob("postclose-06", 6, 20, (str(python), str(runner), "--postclose")),
    )


def plist_payload(job: LaunchdJob, project_root: Path) -> dict[str, Any]:
    log_dir = project_root / "reports" / "premarket-launchd"
    return {
        "Label": job.label,
        "ProgramArguments": list(job.arguments),
        "WorkingDirectory": str(project_root),
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
        "StartCalendarInterval": {"Hour": job.hour, "Minute": job.minute},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / f"{job.name}.stdout.log"),
        "StandardErrorPath": str(log_dir / f"{job.name}.stderr.log"),
    }
