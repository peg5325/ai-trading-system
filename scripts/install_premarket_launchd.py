#!/usr/bin/env python3
"""Install or inspect local launchd jobs for the read-only premarket pipeline."""
from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.output_layout import premarket_launchd_dir
from src.premarket.launchd import jobs, plist_payload


def _agent_dir(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def _write_plist(path: Path, payload: dict) -> None:
    content = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
    partial = path.with_name(f".{path.name}.{os.getpid()}.partial")
    with open(partial, "xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, path)


def install(*, project_root: Path, home: Path, python: Path, bootstrap: bool) -> list[Path]:
    destination = _agent_dir(home)
    destination.mkdir(parents=True, exist_ok=True)
    premarket_launchd_dir(project_root).mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    uid = str(os.getuid())
    for job in jobs(project_root, python):
        path = destination / f"{job.label}.plist"
        _write_plist(path, plist_payload(job, project_root))
        paths.append(path)
        if bootstrap:
            service = f"gui/{uid}/{job.label}"
            subprocess.run(["launchctl", "bootout", service], check=False, capture_output=True)
            subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
                check=True,
                capture_output=True,
                text=True,
            )
    return paths


def status(*, project_root: Path, home: Path, python: Path) -> list[dict[str, object]]:
    destination = _agent_dir(home)
    uid = str(os.getuid())
    result: list[dict[str, object]] = []
    for job in jobs(project_root, python):
        path = destination / f"{job.label}.plist"
        probe = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{job.label}"],
            check=False,
            capture_output=True,
            text=True,
        )
        result.append({
            "label": job.label,
            "hour": job.hour,
            "minute": job.minute,
            "plist_exists": path.exists(),
            "loaded": probe.returncode == 0,
        })
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="install/status premarket launchd jobs")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--install", action="store_true")
    action.add_argument("--status", action="store_true")
    parser.add_argument("--write-only", action="store_true", help="write plists without launchctl bootstrap")
    parser.add_argument("--home", type=Path, default=Path.home(), help=argparse.SUPPRESS)
    parser.add_argument("--python", type=Path, default=Path(sys.executable), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.install:
        paths = install(
            project_root=ROOT,
            home=args.home,
            python=args.python,
            bootstrap=not args.write_only,
        )
        print("\n".join(str(path) for path in paths))
        return 0
    for item in status(project_root=ROOT, home=args.home, python=args.python):
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
