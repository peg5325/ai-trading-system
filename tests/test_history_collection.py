from __future__ import annotations

import fcntl
import json
import shutil
import sqlite3
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.run_history_collection import main as collection_main
from src.premarket.collection import collection_paths, run_history_collection

REPO = Path(__file__).resolve().parents[1]
# 2026-10-01 10:40 KST: the 2026-09-30 POST session closed at 00:00Z.
AFTER_POST = datetime(2026, 10, 1, 1, 40, tzinfo=timezone.utc)


def _root(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir()
    shutil.copy(REPO / "config" / "universe_p0_us30.yaml", tmp_path / "config")
    return tmp_path


def _seed_regular_bars(root: Path, symbol: str, day: date, minutes: int) -> None:
    db = root / "data" / "market_history.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS candle_history ("
                "symbol TEXT, interval TEXT, ts_utc TEXT, ts_exchange TEXT, session TEXT)")
    open_utc = datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc)
    for i in range(minutes):
        ts = open_utc + timedelta(minutes=i)
        con.execute("INSERT INTO candle_history VALUES (?,?,?,?,?)", (
            symbol, "1m", ts.isoformat(),
            (ts - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:00-04:00"), "REGULAR"))
    con.commit()
    con.close()


def _ok_runner(calls: list[list[str]]):
    def run(command: list[str], _root: Path) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "{}", "")
    return run


def test_collects_the_full_universe_for_the_last_post_closed_day(tmp_path):
    root = _root(tmp_path)
    day = date(2026, 9, 30)
    _seed_regular_bars(root, "SPY", day, 390)
    _seed_regular_bars(root, "QQQ", day, 389)
    calls: list[list[str]] = []
    result = run_history_collection(root=root, now=AFTER_POST, runner=_ok_runner(calls))

    assert (result["status"], result["trading_day"]) == ("COMPLETE", "2026-09-30")
    [command] = calls
    assert command[command.index("--completed-through") + 1] == "2026-09-30"
    assert "--tail-repair" in command
    assert command[command.index("--run-id") + 1] == "history-collection-20260930"
    symbols = command[command.index("--symbols") + 1].split(",")
    assert len(symbols) == 30 and result["symbols"] == symbols
    assert result["regular_coverage"]["SPY"] == 390
    assert result["regular_coverage"]["QQQ"] == 389
    assert "SPY" not in result["incomplete_symbols"]
    assert "QQQ" in result["incomplete_symbols"]
    assert len(result["incomplete_symbols"]) == 29
    receipt = collection_paths(root, day)["operation"]
    assert json.loads(receipt.read_text())["artifact_sha256"] == result["artifact_sha256"]

    again = run_history_collection(
        root=root, now=AFTER_POST,
        runner=lambda *_args: pytest.fail("a COMPLETE receipt must make the retry a no-op"))
    assert again == result


@pytest.mark.parametrize(("now", "expected"), [
    # 19:59 ET on 2026-09-30: that day's POST session is still open.
    (datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc), "2026-09-29"),
    # Monday 10:40 KST is Sunday evening in New York: Friday is the last closed day.
    (datetime(2026, 10, 5, 1, 40, tzinfo=timezone.utc), "2026-10-02"),
    # EST regime: 10:40 KST on 2026-12-02 is 20:40 ET after the 2026-12-01 POST close.
    (datetime(2026, 12, 2, 1, 40, tzinfo=timezone.utc), "2026-12-01"),
])
def test_targets_only_days_whose_post_session_has_closed(tmp_path, now, expected):
    calls: list[list[str]] = []
    result = run_history_collection(root=_root(tmp_path), now=now, runner=_ok_runner(calls))
    assert result["trading_day"] == expected
    assert calls[0][calls[0].index("--completed-through") + 1] == expected


def test_lock_timeout_and_backfill_failure_stay_retryable(tmp_path):
    root = _root(tmp_path)
    receipt = collection_paths(root, date(2026, 9, 30))["operation"]
    lock_path = root / "data" / ".market_history.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+b") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        locked = run_history_collection(
            root=root, now=AFTER_POST, lock_timeout_seconds=0.05,
            runner=lambda *_args: pytest.fail("a held lock must block the backfill"))
    assert (locked["status"], locked["reason_codes"]) == ("INCOMPLETE", ["LOCK_TIMEOUT"])
    assert not receipt.exists()

    failed = run_history_collection(
        root=root, now=AFTER_POST,
        runner=lambda command, _root: subprocess.CompletedProcess(command, 1, "", "HTTP 503"))
    assert (failed["status"], failed["reason_codes"]) == ("INCOMPLETE", ["BACKFILL_FAILED"])
    assert not receipt.exists()


def test_cli_reports_incomplete_with_nonzero_exit(tmp_path, monkeypatch, capsys):
    import scripts.run_history_collection as cli

    monkeypatch.setattr(cli, "run_history_collection", lambda **_kw: {"status": "INCOMPLETE"})
    assert collection_main(["--no-dotenv"], now=AFTER_POST) == 2
    assert json.loads(capsys.readouterr().out) == {"status": "INCOMPLETE"}
