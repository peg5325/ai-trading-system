"""macOS launchd job definitions for the local premarket operation runner."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.output_layout import premarket_launchd_dir

LABEL_PREFIX = "com.peg5325.ai-trading.premarket"


@dataclass(frozen=True)
class LaunchdJob:
    name: str
    hour: int
    minute: int
    arguments: tuple[str, ...]
    # "Background" lets macOS throttle timers and I/O; the live shadow run polls at minute + 4 s.
    process_type: str = "Background"

    @property
    def label(self) -> str:
        return f"{LABEL_PREFIX}.{self.name}"


def jobs(project_root: Path, python: Path) -> tuple[LaunchdJob, ...]:
    runner = project_root / "scripts" / "run_premarket_pipeline.py"
    paper = project_root / "scripts" / "run_freeze_paper_session.py"
    collection = project_root / "scripts" / "run_history_collection.py"
    daytrade = (str(python), str(project_root / "scripts" / "run_daytrade_paper.py"))
    shadow = (str(python), str(project_root / "scripts" / "run_daytrade_shadow.py"))
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
        # FREEZE Paper session runner: exits NOT_ACTIVATED without a valid first-session
        # receipt, and SKIPPED before regular close + 20 min (the DST no-op of the pair).
        LaunchdJob("freeze-paper-05", 5, 35, (str(python), str(paper))),
        LaunchdJob("freeze-paper-06", 6, 35, (str(python), str(paper))),
        # Retry slot: 17:35 ET in EST. A recorded session makes it a no-op.
        LaunchdJob("freeze-paper-07", 7, 35, (str(python), str(paper))),
        # Full-universe 1m collection after the POST session closes in both DST regimes
        # (09:00/10:00 KST) and clear of every other job; a COMPLETE receipt makes the
        # retry slot a no-op.
        LaunchdJob("history-collect-10", 10, 40, (str(python), str(collection))),
        LaunchdJob("history-collect-13", 13, 40, (str(python), str(collection))),
        # Exploratory day-trading Paper: catches up every closed session once its
        # history-collect receipt is COMPLETE, so it trails each collect slot by ~25 min.
        LaunchdJob("daytrade-paper-11", 11, 7, (*daytrade, "--auto", "--out-root", str(project_root))),
        LaunchdJob("daytrade-paper-14", 14, 7, (*daytrade, "--auto", "--out-root", str(project_root))),
        # R1 shadow run of the same rules (no orders). 21:50 KST is 08:50 EDT / 07:50 EST, before the
        # 09:25 ET pre-market cutoff in both DST regimes; the run waits for the session itself, refuses
        # holidays and weekends, and keeps the Mac awake until the close.
        LaunchdJob("daytrade-shadow-21", 21, 50,
                   ("/usr/bin/caffeinate", "-i", *shadow, "live", "--out-root", str(project_root)),
                   process_type="Standard"),
        # Compare every finished shadow day with its Paper report and measure the WebSocket log;
        # trails each Paper slot by ~13 min, and a day with compare.json is skipped.
        LaunchdJob("daytrade-shadow-compare-11", 11, 20, (*shadow, "compare", "--auto", "--out-root", str(project_root))),
        LaunchdJob("daytrade-shadow-compare-14", 14, 20, (*shadow, "compare", "--auto", "--out-root", str(project_root))),
    )


def plist_payload(job: LaunchdJob, project_root: Path) -> dict[str, Any]:
    log_dir = premarket_launchd_dir(project_root)
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
        "ProcessType": job.process_type,
        "StandardOutPath": str(log_dir / f"{job.name}.stdout.log"),
        "StandardErrorPath": str(log_dir / f"{job.name}.stderr.log"),
    }
