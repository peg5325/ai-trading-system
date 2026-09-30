from __future__ import annotations

import fcntl
import json
import sys
from datetime import date, datetime, time

import src.freeze_paper.runner as runner_module
from scripts import run_freeze_paper_session
from src.freeze_paper.runner import (
    COMPLETE_STATUS,
    DATA_DRIFT,
    DRY_RUN,
    INCOMPLETE,
    NOT_ACTIVATED,
    SKIPPED,
    build_ledger,
    paper_paths,
    run_paper_session,
)
from src.history.oos_guard import OOSLockError
from src.models import ET
from src.premarket.contract import sha256_payload
from tests.freeze_paper_runner_helpers import (
    FIRST,
    FakeBackfill,
    after_close,
    fake_git,
    make_root,
    write_bars,
    write_day_freeze,
    write_receipt,
)
from tests.freeze_paper_test_helpers import watch_item

DAY2 = "2026-10-02"
DAY3 = "2026-10-05"


def _run(root, day, backfill=None, **kwargs):
    kwargs.setdefault("git_runner", fake_git())
    return run_paper_session(trading_day=date.fromisoformat(day), root=root,
                             now=kwargs.pop("now", after_close(day)),
                             runner=backfill or FakeBackfill(), **kwargs)


def _session(root, day):
    return json.loads(paper_paths(root, date.fromisoformat(day))["session"].read_text())


def _activated(tmp_path):
    root = make_root(tmp_path)
    write_receipt(root)
    return root


def test_missing_receipt_is_not_activated_and_writes_only_the_operation_receipt(tmp_path):
    root = make_root(tmp_path)
    backfill = FakeBackfill()
    result = _run(root, FIRST, backfill)
    assert result["status"] == NOT_ACTIVATED
    assert result["reason_codes"] == ["MISSING_FIRST_SESSION_RECEIPT"]
    paths = paper_paths(root, date.fromisoformat(FIRST))
    assert paths["operation"].is_file()
    assert json.loads(paths["operation"].read_text())["status"] == NOT_ACTIVATED
    assert not paths["session"].exists() and not paths["ledger"].exists()
    assert backfill.commands == []


def test_contract_or_config_change_after_receipt_is_not_activated(tmp_path):
    root = _activated(tmp_path)
    config = root / "config" / "premarket_daily_brief.yaml"
    config.write_text(config.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    result = _run(root, FIRST)
    assert result["status"] == NOT_ACTIVATED
    assert "ACTIVATION_CONFIG_SHA256" in result["reason_codes"][0]


def test_days_outside_the_schedule_are_not_activated(tmp_path):
    root = _activated(tmp_path)
    for day in ("2026-09-30", "2026-10-15"):
        result = _run(root, day)
        assert (result["status"], result["reason_codes"]) == (
            NOT_ACTIVATED, ["NOT_A_SCHEDULED_SESSION"])


def test_non_trading_day_is_skipped_without_writes(tmp_path):
    root = _activated(tmp_path)
    result = _run(root, "2026-10-03")
    assert (result["status"], result["reason_codes"]) == (SKIPPED, ["NON_TRADING_DAY"])
    assert not paper_paths(root, date(2026, 10, 3))["operation"].exists()


def test_before_close_plus_20_minutes_is_skipped(tmp_path):
    root = _activated(tmp_path)
    backfill = FakeBackfill()
    now = datetime.combine(date(2026, 10, 1), time(16, 19), tzinfo=ET)
    result = _run(root, FIRST, backfill, now=now)
    assert (result["status"], result["reason_codes"]) == (SKIPPED, ["PAPER_WINDOW_NOT_REACHED"])
    assert backfill.commands == []


def test_missing_previous_session_is_incomplete_and_never_skipped(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, DAY2)
    result = _run(root, DAY2)
    assert (result["status"], result["reason_codes"]) == (
        INCOMPLETE, ["MISSING_PREVIOUS_SESSION"])
    assert not paper_paths(root, date.fromisoformat(DAY2))["session"].exists()


def test_backfill_failure_is_incomplete_and_retryable(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})
    failed = _run(root, FIRST, FakeBackfill(returncode=1))
    assert (failed["status"], failed["reason_codes"]) == (INCOMPLETE, ["BACKFILL_FAILED"])
    assert not paper_paths(root, date.fromisoformat(FIRST))["session"].exists()
    retried = _run(root, FIRST)
    assert retried["status"] == COMPLETE_STATUS


def test_backfill_uses_the_invoked_python_and_traded_symbols(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST, regime="RISK_OFF", watchlist=[
        watch_item("TQQQ", "SHORT_WATCH"), watch_item("TSLA", "SHORT_WATCH")])
    write_bars(root, FIRST, {"SQQQ": 20.0})
    backfill = FakeBackfill()
    result = _run(root, FIRST, backfill)
    assert result["status"] == COMPLETE_STATUS
    (command,) = backfill.commands
    assert command[0] == sys.executable
    assert command[1].endswith("history_backfill_official.py")
    assert command[command.index("--symbols") + 1] == "SQQQ"
    assert "--tail-repair" in command
    session = _session(root, FIRST)
    assert session["status"] == "TRADED"
    assert {f["leg"] for f in session["fills"]} == {"INVERSE"}
    assert set(session["legs"]) == {"INVERSE"}


def test_two_sessions_chain_carry_and_restore_the_peak(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    missing = {(h, m): {"AAPL": None} for h in range(10, 16) for m in range(60) if (h, m) >= (10, 35)}
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0}, missing)
    assert _run(root, FIRST)["status"] == COMPLETE_STATUS
    day1 = _session(root, FIRST)
    assert day1["status"] == "UNRESOLVED_EXIT"
    assert day1["reconciliation"]["ok"] is True

    write_day_freeze(root, DAY2)
    write_bars(root, DAY2, {"AAPL": 101.0, "MSFT": 200.0})
    backfill = FakeBackfill()
    assert _run(root, DAY2, backfill)["status"] == COMPLETE_STATUS
    day2 = _session(root, DAY2)
    assert day2["prev_session_artifact_sha256"] == day1["artifact_sha256"]
    assert day2["status"] == "HALTED"
    assert day2["equity"]["start"] == day1["equity"]["end"]
    assert day2["end_state"]["peak_equity"] >= day1["end_state"]["peak_equity"]
    assert [(f["symbol"], f["kind"]) for f in day2["fills"]] == [("AAPL", "EXIT")]
    halted = [(e["symbol"], e["reason"]) for e in day2["events"]
              if e["type"] == "NO_TRADE_SYMBOL"]
    assert halted == [("AAPL", "HALTED"), ("MSFT", "HALTED")]
    assert backfill.commands[0][backfill.commands[0].index("--symbols") + 1] == "AAPL"

    ledger = json.loads(paper_paths(root, date(2026, 10, 2))["ledger"].read_text())
    assert [r["status"] for r in ledger["rows"]] == ["UNRESOLVED_EXIT", "HALTED"]


def test_dst_second_run_is_a_no_op_without_backfill(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})
    assert _run(root, FIRST)["status"] == COMPLETE_STATUS
    paths = paper_paths(root, date.fromisoformat(FIRST))
    session, receipt = paths["session"].read_bytes(), paths["operation"].read_bytes()
    second_backfill = FakeBackfill()
    second = _run(root, FIRST, second_backfill,
                  now=datetime.combine(date(2026, 10, 1), time(17, 35), tzinfo=ET))
    assert (second["status"], second["reason_codes"]) == (
        COMPLETE_STATUS, ["SESSION_ALREADY_RECORDED"])
    assert second["operation_receipt_kept"] is True
    assert second_backfill.commands == []
    assert paths["session"].read_bytes() == session
    assert paths["operation"].read_bytes() == receipt


def test_recompute_with_changed_bars_writes_only_the_drift_file(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})
    assert _run(root, FIRST)["status"] == COMPLETE_STATUS
    paths = paper_paths(root, date.fromisoformat(FIRST))
    session, receipt = paths["session"].read_bytes(), paths["operation"].read_bytes()
    same = _run(root, FIRST, recompute=True)
    assert (same["status"], same["reason_codes"]) == (COMPLETE_STATUS, ["RECOMPUTE_IDENTICAL"])
    assert not paths["drift"].exists()
    write_bars(root, FIRST, {"AAPL": 100.5})
    backfill = FakeBackfill()
    drift = _run(root, FIRST, backfill, recompute=True)
    assert (drift["status"], drift["reason_codes"]) == (DATA_DRIFT, ["SESSION_ARTIFACT_DIFFERS"])
    assert backfill.commands == []
    report = json.loads(paths["drift"].read_text())
    assert report["existing_artifact_sha256"] == json.loads(session)["artifact_sha256"]
    assert "inputs" in report["differing_keys"]
    assert paths["session"].read_bytes() == session
    assert paths["operation"].read_bytes() == receipt


def test_exceptions_become_incomplete_with_the_type_name_only(tmp_path, monkeypatch):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})

    def locked(*_args):
        raise OOSLockError("blind OOS secret-detail")

    result = _run(root, FIRST, bar_loader=locked)
    assert (result["status"], result["reason_codes"]) == (
        INCOMPLETE, ["EXCEPTION:OOSLockError"])
    receipt_text = paper_paths(root, date.fromisoformat(FIRST))["operation"].read_text()
    assert "secret-detail" not in receipt_text and "Traceback" not in receipt_text

    def exists(*_args, **_kwargs):
        raise FileExistsError("/private/path/token")

    monkeypatch.setattr(runner_module, "write_immutable_pair", exists)
    result = _run(root, FIRST)
    assert result["reason_codes"] == ["EXCEPTION:FileExistsError"]
    assert "/private/path" not in paper_paths(root, date.fromisoformat(FIRST))[
        "operation"].read_text()


def _resign(path, **changes):
    artifact = json.loads(path.read_text())
    artifact.update(changes)
    artifact.pop("artifact_sha256")
    artifact["artifact_sha256"] = sha256_payload(artifact)
    path.write_text(json.dumps(artifact))


def _two_days(tmp_path):
    root = _activated(tmp_path)
    for day in (FIRST, DAY2):
        write_day_freeze(root, day)
        write_bars(root, day, {"AAPL": 100.0, "MSFT": 200.0})
    assert _run(root, FIRST)["status"] == COMPLETE_STATUS
    return root


def test_previous_session_must_carry_this_receipt_and_its_index(tmp_path):
    root = _two_days(tmp_path)
    day1 = paper_paths(root, date.fromisoformat(FIRST))["session"]
    original = day1.read_text()
    _resign(day1, first_session_receipt_sha256="0" * 64)
    assert _run(root, DAY2)["reason_codes"] == ["SESSION_RECEIPT_MISMATCH"]
    day1.write_text(original)
    _resign(day1, session_index=3)
    assert _run(root, DAY2)["reason_codes"] == ["SESSION_POSITION_INVALID"]
    day1.write_text(original)
    assert _run(root, DAY2)["status"] == COMPLETE_STATUS


def test_freeze_snapshot_built_with_another_config_is_no_trade(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST, mutate=lambda a: a["snapshot"]["payload"]["policy_config"]
                     .update(min_liquidity_score=0.0))
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})
    backfill = FakeBackfill()
    assert _run(root, FIRST, backfill)["status"] == COMPLETE_STATUS
    session = _session(root, FIRST)
    assert session["status"] == "NO_TRADE"
    assert "POLICY_CONFIG_MISMATCH" in session["gate"]["reason_codes"]
    assert session["fills"] == [] and backfill.commands == []


def test_corrupted_restart_checkpoint_is_flagged(tmp_path, monkeypatch):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})
    real = runner_module._checkpoint

    def corrupt(state):
        restored = real(state)
        restored["peak_equity"] += 1000.0
        return restored

    monkeypatch.setattr(runner_module, "_checkpoint", corrupt)
    _run(root, FIRST)
    reconciliation = _session(root, FIRST)["reconciliation"]
    assert reconciliation["ok"] is False
    assert {"check": "RESTART_REBUILD_MISMATCH"} in reconciliation["mismatches"]
    assert reconciliation["restart_checkpoint_bar"] == "2026-10-01T16:45:00Z"


def test_clean_run_passes_the_midpoint_restart_check(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})
    _run(root, FIRST)
    reconciliation = _session(root, FIRST)["reconciliation"]
    assert reconciliation["ok"] is True and reconciliation["restart_checkpoint_bar"]


def test_ledger_marks_tampered_artifacts_invalid(tmp_path):
    root = _two_days(tmp_path)
    assert _run(root, DAY2)["status"] == COMPLETE_STATUS
    day1 = paper_paths(root, date.fromisoformat(FIRST))["session"]
    artifact = json.loads(day1.read_text())
    artifact["status"] = "TRADED_TWICE"                       # unsigned edit
    day1.write_text(json.dumps(artifact))
    sessions = [date.fromisoformat(FIRST), date.fromisoformat(DAY2)]
    ledger = build_ledger(root, sessions, through=date.fromisoformat(DAY2))
    assert [(r["status"], r.get("reason")) for r in ledger["rows"]] == [
        ("INVALID", "SESSION_SIGNATURE_INVALID"), ("INVALID", "SESSION_CHAIN_BROKEN")]


def test_invalid_backfill_symbol_is_incomplete(tmp_path, monkeypatch):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    monkeypatch.setattr(runner_module, "_gate_symbols", lambda _gate, _state: ["AAPL;rm"])
    backfill = FakeBackfill()
    result = _run(root, FIRST, backfill)
    assert (result["status"], result["reason_codes"]) == (INCOMPLETE, ["INVALID_SYMBOL"])
    assert backfill.commands == []


def test_code_commit_must_be_an_ancestor_of_head(tmp_path):
    root = _activated(tmp_path)
    git = fake_git(0)
    _run(root, "2026-10-15", git_runner=git)
    assert git.calls == [["git", "merge-base", "--is-ancestor", "a" * 40, "HEAD"]]
    result = _run(root, FIRST, git_runner=fake_git(1))
    assert (result["status"], result["reason_codes"]) == (
        NOT_ACTIVATED, ["CODE_COMMIT_NOT_ANCESTOR"])
    assert _run(root, FIRST, git_runner=fake_git(128))["reason_codes"] == ["GIT_UNAVAILABLE"]

    def missing_git(_args, _cwd):
        raise FileNotFoundError("git")

    assert _run(root, FIRST, git_runner=missing_git)["reason_codes"] == ["GIT_UNAVAILABLE"]


def test_shared_market_db_lock_timeout_is_incomplete(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})
    lock_path = root / "data" / ".market_history.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    backfill = FakeBackfill()
    with open(lock_path, "a+b") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        result = _run(root, FIRST, backfill, lock_timeout_seconds=0.05)
    assert (result["status"], result["reason_codes"]) == (INCOMPLETE, ["LOCK_TIMEOUT"])
    assert backfill.commands == []
    assert _run(root, FIRST)["status"] == COMPLETE_STATUS          # retry after release


def test_bar_ts_is_the_bar_start_and_the_session_is_open_to_close(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0},
               {(16, 0): {"AAPL": (1.0, 1.0, 1.0, 1.0)}}, end=(16, 1))
    _run(root, FIRST)
    session = _session(root, FIRST)
    assert session["inputs"]["bars"]["AAPL"]["count"] == 390       # 09:30..15:59, 16:00 dropped
    buy = next(f for f in session["fills"] if f["symbol"] == "AAPL" and f["side"] == "BUY")
    assert buy["timestamp"] == "2026-10-01T13:35:00Z"              # 09:35 ET bar start (EDT)
    assert buy["ref_price"] == 100.0


def test_stale_partial_is_reported_and_kept(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})
    path = paper_paths(root, date.fromisoformat(FIRST))["session"]
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(b"half")
    result = _run(root, FIRST)
    assert (result["status"], result["reason_codes"]) == (INCOMPLETE, ["STALE_PARTIAL"])
    assert partial.read_bytes() == b"half" and not path.exists()


def test_ledger_is_a_regenerable_fold_with_incomplete_rows(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})
    _run(root, FIRST)
    written = json.loads(paper_paths(root, date.fromisoformat(FIRST))["ledger"].read_text())
    sessions = [date.fromisoformat(d) for d in json.loads(
        (root / "docs/research/PREREG_PREMARKET_FREEZE_LONG_PAPER_V1_FIRST_SESSION_RECEIPT.json")
        .read_text())["scheduled_sessions"]]
    assert build_ledger(root, sessions, through=date(2026, 10, 1)) == written
    assert written["rows"][0]["status"] == "TRADED"
    assert set(written["rows"][0]["legs"]) == {"LONG"}
    later = build_ledger(root, sessions, through=date.fromisoformat(DAY3))
    assert [r["status"] for r in later["rows"]] == ["TRADED", INCOMPLETE, INCOMPLETE]
    unsigned = {k: v for k, v in written.items() if k != "artifact_sha256"}
    assert written["artifact_sha256"] == sha256_payload(unsigned)


def test_session_artifact_records_inputs_and_has_no_wall_clock(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})
    _run(root, FIRST)
    session = _session(root, FIRST)
    assert set(session["inputs"]["bars"]) == {"AAPL", "MSFT"}
    assert session["inputs"]["freeze_brief_file_sha256"]
    assert session["inputs"]["freeze_receipt_file_sha256"]
    assert all({"leg", "commission", "sec_fee"} <= set(f) for f in session["fills"])
    assert "run_at" not in json.dumps(session)
    op = json.loads(paper_paths(root, date.fromisoformat(FIRST))["operation"].read_text())
    assert op["status"] == COMPLETE_STATUS and "run_at" in op and "git_commit" in op


def test_dry_run_writes_nothing(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})
    backfill = FakeBackfill()
    result = _run(root, FIRST, backfill, dry_run=True)
    assert result["status"] == DRY_RUN
    assert result["session"]["status"] == "TRADED"
    paths = paper_paths(root, date.fromisoformat(FIRST))
    assert not any(paths[k].exists() for k in ("session", "ledger", "operation"))
    assert backfill.commands == []


def test_cli_dry_run_prints_summary(tmp_path, capsys):
    root = make_root(tmp_path)
    code = run_freeze_paper_session.main(
        ["--trading-day", FIRST, "--reports-root", str(root / "reports"), "--dry-run",
         "--no-dotenv"], now=after_close(FIRST))
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == NOT_ACTIVATED
    assert not paper_paths(root, date.fromisoformat(FIRST))["operation"].exists()


def test_missing_markdown_is_restored_from_the_verified_json(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})
    _run(root, FIRST)
    paths = paper_paths(root, date.fromisoformat(FIRST))
    markdown, session = paths["session_markdown"].read_bytes(), paths["session"].read_bytes()
    paths["session_markdown"].unlink()
    backfill = FakeBackfill()
    assert _run(root, FIRST, backfill)["reason_codes"] == ["SESSION_ALREADY_RECORDED"]
    assert paths["session_markdown"].read_bytes() == markdown
    assert paths["session"].read_bytes() == session and backfill.commands == []


def test_tampered_recorded_session_keeps_the_receipt_and_writes_an_alert(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})
    _run(root, FIRST)
    paths = paper_paths(root, date.fromisoformat(FIRST))
    receipt = paths["operation"].read_bytes()
    artifact = json.loads(paths["session"].read_text())
    artifact["status"] = "NO_TRADE"                               # unsigned edit
    paths["session"].write_text(json.dumps(artifact))
    result = _run(root, FIRST)
    assert (result["status"], result["reason_codes"]) == (
        INCOMPLETE, ["EXISTING_SESSION_SIGNATURE_INVALID"])
    assert paths["operation"].read_bytes() == receipt
    alert = json.loads(paths["alert"].read_text())
    assert alert["reason_codes"] == ["EXISTING_SESSION_SIGNATURE_INVALID"]
    assert set(alert) == {"schema", "trading_day", "reason_codes", "artifact_sha256"}


def test_ledger_verification_includes_the_contract_hash(tmp_path):
    root = _activated(tmp_path)
    write_day_freeze(root, FIRST)
    write_bars(root, FIRST, {"AAPL": 100.0, "MSFT": 200.0})
    _run(root, FIRST)
    path = paper_paths(root, date.fromisoformat(FIRST))["session"]
    artifact = json.loads(path.read_text())
    sessions = [date.fromisoformat(FIRST)]
    ok = build_ledger(root, sessions, through=sessions[0],
                      contract_sha256=artifact["contract_sha256"])
    assert ok["rows"][0]["status"] == "TRADED"
    other = build_ledger(root, sessions, through=sessions[0], contract_sha256="0" * 64)
    assert (other["rows"][0]["status"], other["rows"][0]["reason"]) == (
        "INVALID", "SESSION_CONTRACT_MISMATCH")
    written = json.loads(paper_paths(root, sessions[0])["ledger"].read_text())
    assert written["rows"] == ok["rows"]          # the run path verified with the same contract
