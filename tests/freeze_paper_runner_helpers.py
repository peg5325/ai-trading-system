"""tmp_path project roots for the FREEZE Paper runner tests (offline, synthetic data)."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from datetime import date, datetime, time
from pathlib import Path

from src.freeze_paper.receipt import (
    CONFIG_RELATIVE,
    CONTRACT_JSON_RELATIVE,
    CONTRACT_MD_RELATIVE,
    RECEIPT_RELATIVE,
    SCHEMA,
    file_sha256,
    previous_trading_day,
    scheduled_sessions,
)
from src.freeze_paper.spy import spy_close_evidence
from src.history.store import SCHEMA as DB_SCHEMA
from src.models import ET, Candle
from src.premarket.contract import FREEZE_PAPER_TEMPLATE_ID, load_config, sha256_payload
from tests.freeze_paper_test_helpers import schedule, write_freeze

REPO = Path(__file__).resolve().parents[1]
FIRST = "2026-10-01"
NODE_FILE = "tests/test_fake_verified_paths.py"


def make_root(tmp_path: Path, *, activate_config: bool = True) -> Path:
    root = tmp_path / "project"
    for rel in (CONTRACT_JSON_RELATIVE, CONTRACT_MD_RELATIVE):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, root / rel)
    (root / "config").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / "config" / "universe_p0_us30.yaml", root / "config" / "universe_p0_us30.yaml")
    text = (REPO / CONFIG_RELATIVE).read_text(encoding="utf-8")
    # Set both allowlists explicitly so the fixture does not depend on the repo's current
    # activation state.
    approved = f"[{FREEZE_PAPER_TEMPLATE_ID}]" if activate_config else "[]"
    for key in ("approved_strategy_templates", "approved_short_strategy_templates"):
        text, count = re.subn(rf"^{key}: .*$", f"{key}: {approved}", text, flags=re.MULTILINE)
        assert count == 1, key
    (root / CONFIG_RELATIVE).write_text(text, encoding="utf-8")
    (root / NODE_FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / NODE_FILE).write_text(
        "".join(f"def test_{name}():\n    pass\n\n\n" for name in
                ("restart", "eod", "reconciliation", "duplicate")), encoding="utf-8")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    return root


def receipt_payload(root: Path, *, first: str = FIRST, **overrides) -> dict:
    first_day = date.fromisoformat(first)
    sessions = [d.isoformat() for d in scheduled_sessions(first_day, 10)]
    window_start = previous_trading_day(first_day)
    db = root / "data" / "market_history.sqlite"
    if spy_close_evidence(db, window_start) is None:     # captured the same evening
        write_bars(root, window_start.isoformat(), {"SPY": 500.0},
                   created_at=f"{window_start.isoformat()}T20:30:00+00:00")
    receipt = {
        "schema": SCHEMA,
        "contract_json_sha256": file_sha256(root / CONTRACT_JSON_RELATIVE),
        "contract_md_sha256": file_sha256(root / CONTRACT_MD_RELATIVE),
        "contract_version": "1.2",
        "code_commit": "a" * 40,
        "activation_config_sha256": file_sha256(root / CONFIG_RELATIVE),
        "first_session": first,
        "scheduled_sessions": sessions,
        "window_start_close_date": window_start.isoformat(),
        "window_start_spy_close_evidence": spy_close_evidence(db, window_start),
        "intraday_bar_provider": "Toss official 1m candles (tail repair)",
        "spy_price_source": "Toss official daily candles",
        "spy_dividend_source": "State Street SPY distribution page",
        "spy_ex_dividend_source": "State Street SPY distribution page",
        "spy_ex_dividend_dates_in_window": [],
        "operating_costs": {"items": [], "total_usd": 0.0,
                            "allocation": "window sessions / billing-period sessions",
                            "reason": "no dedicated paid service used"},
        "account_event_fee": {"observed_commission_bps_per_side": None,
                              "note": "no account event fee observed"},
        "allowed_symbols_check": {"index_3x_etfs_listed": ["SOXL", "TQQQ"],
                                  "single_stock_leveraged_etfs": [], "passed": True},
        "verified_paths": {key: [f"{NODE_FILE}::test_{key}"]
                           for key in ("restart", "eod", "reconciliation", "duplicate")},
        "test_run": {"command": ".venv/bin/python -m pytest -q", "passed": True},
    }
    receipt.update(overrides)
    receipt.pop("artifact_sha256", None)
    receipt["artifact_sha256"] = sha256_payload(receipt)
    return receipt


def write_receipt(root: Path, receipt: dict | None = None, **overrides) -> dict:
    receipt = receipt or receipt_payload(root, **overrides)
    path = root / RECEIPT_RELATIVE
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    return receipt


def write_day_freeze(root: Path, day: str, **kwargs) -> dict:
    """FREEZE brief + COMPLETE receipt for ``day`` (planner artifact re-dated and re-signed)."""
    user_mutate = kwargs.pop("mutate", None)

    policy = json.loads(json.dumps(load_config(root / CONFIG_RELATIVE).to_dict()))

    def redate(artifact):
        artifact["brief"]["trading_day"] = day
        artifact["snapshot"]["payload"]["schedule"] = schedule(day)
        artifact["snapshot"]["payload"]["policy_config"] = policy   # the pinned activation config
        if user_mutate is not None:
            user_mutate(artifact)

    return write_freeze(root / "reports", day=day, mutate=redate, **kwargs)


def write_bars(root: Path, day: str, prices: dict[str, float],
               overrides: dict[tuple[int, int], dict[str, tuple | None]] | None = None,
               end: tuple[int, int] = (16, 0),
               created_at: str = "2026-01-01T00:00:00+00:00") -> Path:
    db = root / "data" / "market_history.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(DB_SCHEMA)
    hh, mm = 9, 30
    d = date.fromisoformat(day)
    while (hh, mm) < end:
        for sym, price in prices.items():
            ohlc = (price, price, price, price)
            override = (overrides or {}).get((hh, mm), {})
            if sym in override:
                if override[sym] is None:
                    ohlc = None
                else:
                    ohlc = override[sym]
            if ohlc is not None:
                bar = Candle.from_moment(sym, "1m", datetime.combine(d, time(hh, mm), tzinfo=ET),
                                         *ohlc, 1000.0, "USD", False, market="US")
                con.execute(
                    "INSERT OR REPLACE INTO candle_history "
                    "(symbol,market,interval,ts_utc,ts_exchange,session,open,high,low,close,"
                    "volume,currency,adjusted,source,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (bar.symbol, bar.market, bar.interval, bar.ts_utc.isoformat(),
                     bar.ts_exchange.isoformat(), bar.session, bar.open, bar.high, bar.low,
                     bar.close, bar.volume, bar.currency, int(bar.adjusted), "test",
                     created_at, created_at))
        mm += 1
        if mm == 60:
            hh, mm = hh + 1, 0
    con.commit()
    con.close()
    return db


class FakeBackfill:
    """Records backfill commands; returncode 0 unless told to fail."""

    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, self.returncode, "{}", "")


def fake_git(returncode: int = 0):
    """git runner stub: 0 = receipt commit is an ancestor of HEAD."""
    calls: list[list[str]] = []

    def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, "", "")

    run.calls = calls
    return run


def after_close(day: str) -> datetime:
    return datetime.combine(date.fromisoformat(day), time(16, 30), tzinfo=ET)
