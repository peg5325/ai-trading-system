from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from scripts import collect_premarket_market
from scripts.install_premarket_launchd import install
from src.premarket.launchd import LABEL_PREFIX, jobs, plist_payload
from src.premarket.operations import (
    OPERATION_SCHEMA,
    postclose_paths,
    run_postclose,
    run_stage,
    stage_paths,
    trading_day_from_clock,
)


def _provider_bundle() -> dict:
    return {
        "schema": "premarket-provider-bundle/v1",
        "trading_day": "2026-09-24",
        "stage": "PRIMARY",
        "as_of": "2026-09-24T12:00:00Z",
        "artifact_sha256": "a" * 64,
    }


def _runner_that_writes_outputs(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    for flag in ("--out-json", "--out-markdown"):
        if flag in command:
            target = Path(command[command.index(flag) + 1])
            target.parent.mkdir(parents=True, exist_ok=True)
            if flag == "--out-json" and target.name.startswith("premarket-daily-brief"):
                target.write_text(json.dumps({"brief": {"status": "NO_TRADE", "reason_codes": ["TEST"]}}))
            else:
                target.write_text("{}")
    return subprocess.CompletedProcess(command, 0, "", "")


def test_market_cli_writes_standard_failure_bundle_for_toss_http_403(tmp_path, monkeypatch):
    provider = tmp_path / "provider.json"
    provider.write_text(json.dumps(_provider_bundle()), encoding="utf-8")
    out = tmp_path / "market.json"
    response = httpx.Response(403, request=httpx.Request("POST", "https://api.invalid/token"))

    def fail(*_args, **_kwargs):
        raise httpx.HTTPStatusError("forbidden", request=response.request, response=response)

    monkeypatch.setattr(collect_premarket_market, "build_market_bundle", fail)
    assert collect_premarket_market.main([
        "--provider-bundle", str(provider), "--out-json", str(out),
        "--raw-root", str(tmp_path / "raw"), "--no-dotenv",
    ]) == 2
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["collection_status"] == "FAIL_CLOSED"
    assert result["collection_defects"] == ["TOSS_HTTP_STATUS_403"]
    assert result["symbols"] == []
    assert (tmp_path / "market.json.receipt.json").exists()


def test_stage_runner_completes_no_trade_artifact_once_and_reuses_it(tmp_path):
    trading_day = date(2026, 9, 24)
    result = run_stage(
        trading_day=trading_day,
        stage="PRIMARY",
        root=tmp_path,
        runner=_runner_that_writes_outputs,
        now=datetime(2026, 9, 24, 12, 1, tzinfo=timezone.utc),
    )
    paths = stage_paths(tmp_path, trading_day, "PRIMARY")
    assert result["schema"] == OPERATION_SCHEMA
    assert result["status"] == "COMPLETE"
    assert result["brief_status"] == "NO_TRADE"
    assert paths["operation"].exists()

    def never_run(_command: list[str], _root: Path) -> subprocess.CompletedProcess[str]:
        raise AssertionError("existing immutable operation must not invoke a command")

    assert run_stage(
        trading_day=trading_day,
        stage="PRIMARY",
        root=tmp_path,
        runner=never_run,
    ) == result


def test_stage_runner_preserves_the_invoked_virtualenv_python(tmp_path, monkeypatch):
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(Path(sys.executable).resolve())
    monkeypatch.setattr(sys, "executable", str(venv_python))
    invoked: list[str] = []

    def record(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
        invoked.append(command[0])
        return _runner_that_writes_outputs(command, root)

    result = run_stage(
        trading_day=date(2026, 9, 24),
        stage="PRIMARY",
        root=tmp_path,
        runner=record,
        now=datetime(2026, 9, 24, 12, 1, tzinfo=timezone.utc),
    )

    assert result["status"] == "COMPLETE"
    assert invoked
    assert set(invoked) == {str(venv_python)}


def test_stage_runner_repairs_a_missing_brief_markdown_sibling(tmp_path):
    trading_day = date(2026, 9, 24)
    paths = stage_paths(tmp_path, trading_day, "PRIMARY")
    paths["brief"].parent.mkdir(parents=True, exist_ok=True)
    paths["brief"].write_text(json.dumps({"brief": {"status": "NO_TRADE", "reason_codes": []}}))
    for path in (paths["provider"], paths["market"], paths["input"]):
        path.write_text("{}")

    result = run_stage(
        trading_day=trading_day,
        stage="PRIMARY",
        root=tmp_path,
        runner=_runner_that_writes_outputs,
        now=datetime(2026, 9, 24, 12, 1, tzinfo=timezone.utc),
    )

    assert result["status"] == "COMPLETE"
    assert paths["brief_markdown"].exists()


def test_stage_runner_skips_nyse_nontrading_day_without_running_commands(tmp_path):
    result = run_stage(
        trading_day=date(2026, 9, 26),
        stage="PRIMARY",
        root=tmp_path,
        runner=lambda *_args: pytest.fail("must not run on a non-trading day"),
    )
    assert result["status"] == "SKIPPED"
    assert result["reason_codes"] == ["NON_TRADING_DAY"]


def test_dst_secondary_primary_trigger_runs_only_in_standard_time_window(tmp_path):
    trading_day = date(2026, 12, 15)
    early = run_stage(
        trading_day=trading_day,
        stage="PRIMARY",
        root=tmp_path,
        now=datetime(2026, 12, 15, 12, 1, tzinfo=timezone.utc),
        runner=lambda *_args: pytest.fail("EDT-only KST trigger must no-op during EST"),
    )
    assert early["status"] == "SKIPPED"
    assert not stage_paths(tmp_path, trading_day, "PRIMARY")["operation"].exists()

    late = run_stage(
        trading_day=trading_day,
        stage="PRIMARY",
        root=tmp_path,
        now=datetime(2026, 12, 15, 13, 1, tzinfo=timezone.utc),
        runner=_runner_that_writes_outputs,
    )
    assert late["status"] == "COMPLETE"


def test_postclose_runner_requires_freeze_before_any_market_backfill(tmp_path):
    result = run_postclose(
        trading_day=date(2026, 9, 24),
        root=tmp_path,
        now=datetime(2026, 9, 24, 20, 20, tzinfo=timezone.utc),
        runner=lambda *_args: pytest.fail("missing FREEZE must block all commands"),
    )
    assert result["status"] == "INCOMPLETE"
    assert result["reason_codes"] == ["MISSING_FREEZE_BRIEF"]
    assert postclose_paths(tmp_path, date(2026, 9, 24))["operation"].exists()


def test_postclose_runner_waits_for_the_shared_market_db_lock(tmp_path):
    import fcntl

    day = date(2026, 9, 24)
    freeze = postclose_paths(tmp_path, day)["freeze"]
    freeze.parent.mkdir(parents=True, exist_ok=True)
    freeze.write_text(json.dumps({"brief": {"watchlist": []}}), encoding="utf-8")
    lock_path = tmp_path / "data" / ".market_history.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        result = run_postclose(
            trading_day=day, root=tmp_path,
            now=datetime(2026, 9, 24, 20, 20, tzinfo=timezone.utc),
            runner=lambda *_args: pytest.fail("a held lock must block the backfill"),
            lock_timeout_seconds=0.05,
        )
    assert (result["status"], result["reason_codes"]) == ("INCOMPLETE", ["LOCK_TIMEOUT"])
    assert not postclose_paths(tmp_path, day)["operation"].exists()     # retryable


def test_postclose_runner_rejects_early_execution_before_reading_freeze(tmp_path):
    result = run_postclose(
        trading_day=date(2026, 9, 24),
        root=tmp_path,
        now=datetime(2026, 9, 24, 20, 19, tzinfo=timezone.utc),
        runner=lambda *_args: pytest.fail("pre-close window must block all commands"),
    )
    assert result["status"] == "SKIPPED"
    assert result["reason_codes"] == ["POSTCLOSE_WINDOW_NOT_REACHED"]


def test_postclose_kst_morning_uses_the_same_previous_et_trading_day():
    kst = ZoneInfo("Asia/Seoul")
    # 05:20 KST on Sep 25 is 16:20 EDT on Sep 24, immediately after that
    # Sep 24 NYSE regular close. The ET date is exactly the desired freeze day.
    assert trading_day_from_clock(datetime(2026, 9, 25, 5, 20, tzinfo=kst)) == date(2026, 9, 24)


def test_runner_preserves_redacted_failure_log(tmp_path):
    def fails(command: list[str], _root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "Authorization=Bearer secret-token https://example.invalid/?api_key=abc",
        )

    result = run_stage(
        trading_day=date(2026, 9, 24),
        stage="PRIMARY",
        root=tmp_path,
        runner=fails,
        now=datetime(2026, 9, 24, 12, 1, tzinfo=timezone.utc),
    )
    assert result["status"] == "INCOMPLETE"
    log = tmp_path / "reports" / "premarket-operation-logs" / "20260924.primary" / "provider.log"
    content = log.read_text(encoding="utf-8")
    assert "secret-token" not in content
    assert "api_key=abc" not in content
    assert "<REDACTED>" in content
    receipt = tmp_path / "reports" / "premarket-operation.20260924.primary.json"
    assert receipt.exists()


def test_launchd_definitions_use_local_runner_without_credentials(tmp_path):
    python = tmp_path / "venv" / "bin" / "python"
    all_jobs = jobs(tmp_path, python)
    assert [job.name for job in all_jobs] == [
        "primary-21", "primary-22", "verification-22", "verification-23",
        "freeze-22", "freeze-23", "postclose-05", "postclose-06",
        "freeze-paper-05", "freeze-paper-06", "freeze-paper-07",
        "history-collect-10", "history-collect-13",
        "daytrade-paper-11", "daytrade-paper-14",
        "daytrade-shadow-21", "daytrade-shadow-compare-11", "daytrade-shadow-compare-14",
    ]
    assert [(job.hour, job.minute) for job in all_jobs] == [
        (21, 1), (22, 1), (22, 1), (23, 1), (22, 11), (23, 11), (5, 20), (6, 20),
        (5, 35), (6, 35), (7, 35), (10, 40), (13, 40), (11, 7), (14, 7),
        (21, 50), (11, 20), (14, 20),
    ]
    for job in all_jobs:
        payload = plist_payload(job, tmp_path)
        assert payload["Label"].startswith(LABEL_PREFIX)
        assert "TOSS_CLIENT_SECRET" not in str(payload)
        args = payload["ProgramArguments"]
        shadow = any(a.endswith("run_daytrade_shadow.py") for a in args)
        assert ("--stage" in args or "--postclose" in args
                or args[-1].endswith("run_freeze_paper_session.py")
                or args[-1].endswith("run_history_collection.py")
                or args[1].endswith("run_daytrade_paper.py") or shadow)
        if args[1].endswith("run_daytrade_paper.py"):
            assert args[2:] == ["--auto", "--out-root", str(tmp_path)]
        if shadow:
            tail = args[args.index(next(a for a in args if a.endswith("run_daytrade_shadow.py"))) + 1:]
            assert tail in (["live", "--out-root", str(tmp_path)], ["compare", "--auto", "--out-root", str(tmp_path)])
        # only the live shadow run (minute polls, stays awake to the close) is not throttled
        assert payload["ProcessType"] == ("Standard" if job.name == "daytrade-shadow-21" else "Background")
    live = next(job for job in all_jobs if job.name == "daytrade-shadow-21")
    assert live.arguments[:2] == ("/usr/bin/caffeinate", "-i")


def test_launchd_write_only_install_generates_valid_plists(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    paths = install(
        project_root=project,
        home=home,
        python=project / ".venv" / "bin" / "python",
        bootstrap=False,
    )
    assert len(paths) == 18
    for path in paths:
        payload = plistlib.loads(path.read_bytes())
        assert payload["Label"].startswith(LABEL_PREFIX)
        assert payload["StartCalendarInterval"]["Minute"] in {1, 7, 11, 20, 35, 40, 50}


def test_launchd_status_covers_the_freeze_paper_pair_without_launchctl(tmp_path, monkeypatch):
    import scripts.install_premarket_launchd as installer

    probed: list[list[str]] = []

    def fake_run(command, **_kwargs):
        probed.append(command)
        return subprocess.CompletedProcess(command, 113, "", "")

    monkeypatch.setattr(installer.subprocess, "run", fake_run)
    rows = installer.status(project_root=tmp_path, home=tmp_path / "home",
                            python=tmp_path / "python")
    paper = [row for row in rows if ".freeze-paper-" in row["label"]]
    assert [(row["hour"], row["minute"]) for row in paper] == [(5, 35), (6, 35), (7, 35)]
    assert all(row["plist_exists"] is False and row["loaded"] is False for row in paper)
    assert all(command[:2] == ["launchctl", "print"] for command in probed)


def test_freeze_paper_launchd_pair_runs_after_close_in_both_dst_regimes():
    from datetime import timedelta

    from src.premarket.operations import postclose_earliest
    from src.premarket.schedule import build_premarket_schedule

    kst = ZoneInfo("Asia/Seoul")
    for day in (date(2026, 10, 1), date(2026, 11, 27), date(2026, 12, 1)):
        earliest = postclose_earliest(build_premarket_schedule(day))
        next_kst = day + timedelta(days=1)
        runs = [datetime(next_kst.year, next_kst.month, next_kst.day, h, 35, tzinfo=kst)
                for h in (5, 6, 7)]
        assert any(run >= earliest for run in runs)
        assert all(run.astimezone(ZoneInfo("America/New_York")).date() == day for run in runs)
