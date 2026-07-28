"""Phase 6 리포트 CLI(scripts/phase6_promotion_report.py) glue 테스트 (PR #38).

순수 판정/조립은 test_shadow_promotion{,_io} 가 덮으므로, 여기서는 CLI 의 얇은 glue —
spec 경로 추론, 인자 검증, **실제 sqlite journal/candle read 경로** end-to-end 만 확인한다.
read-only(원본 DB 변경 없음).
"""
from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timezone

from src.journal.models import JournalCycle
from src.journal.sqlite_store import SqliteJournalStore
from src.models import Candle
from src.runtime.isolation.identity import SESSION_SCHEMA_VERSION, SessionIdentity
from src.runtime.isolation.manifest import SessionManifest, write_manifest
from src.shadow.promotion_io import SessionInputs
from src.storage.candle_store import CandleStore

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "phase6_promotion_report.py")


def _load_cli():
    spec = importlib.util.spec_from_file_location("phase6_cli", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load_cli()


def test_resolve_session_spec_infers_candles_and_manifest():
    j, c, m = cli.resolve_session_spec("data/journal_us_phase6_s001.sqlite")
    assert c == "data/market_us_phase6_s001.sqlite"
    assert m == "data/journal_us_phase6_s001.sqlite.manifest.json"


def test_resolve_session_spec_explicit_overrides():
    j, c, m = cli.resolve_session_spec("a/j.sqlite:b/cand.sqlite:x/man.json")
    assert (j, c, m) == ("a/j.sqlite", "b/cand.sqlite", "x/man.json")


def test_main_no_session_returns_2(capsys):
    assert cli.main([]) == 2
    assert "세션이 지정되지 않았습니다" in capsys.readouterr().err


def test_main_bad_horizons_returns_2(capsys):
    assert cli.main(["--session", "x.sqlite", "--horizons", "abc"]) == 2


def _c(symbol, minute, close, hour=14):
    ts = datetime(2026, 7, 7, hour, minute, tzinfo=timezone.utc)
    return Candle.from_moment(symbol=symbol, interval="1m", moment=ts,
                              o=close, h=close + 1, low=close - 1, c=close, v=1000.0,
                              currency="USD", adjusted=True)


def _seed_session(tmp_path, name, *, config_hash):
    journal_path = tmp_path / f"journal_{name}.sqlite"
    candles_path = tmp_path / f"market_{name}.sqlite"

    cs = CandleStore(str(candles_path))
    cs.upsert_many([_c("AAA", 0, 100.0), _c("AAA", 30, 95.0)])
    cs.flush()
    cs.close()

    js = SqliteJournalStore(str(journal_path))
    js.save_cycle(JournalCycle(
        cycle_id=f"{name}-c1", cycle_ts="2026-07-07T14:00:00Z", session="REGULAR",
        universe=["AAA"], status="RAN", reason=None, error=None,
        outcomes=[{"symbol": "AAA", "candidate_id": f"{name}-cid1", "action": "ORDERED"}],
        shadow={"shadow_version": "1", "policy_version": "1",
                "decision_time": "2026-07-07T14:00:00Z",
                "market_context_snapshot": {"benchmark_symbol": "QQQ",
                                            "benchmark_trend": "DOWN"},
                "policies": [{"policy_id": "A", "would_allow": False,
                              "reason_codes": ["CLOSE_BELOW_EMA"]}],
                "candidate_ids": [f"{name}-cid1"]}))
    js.flush()
    js.close()

    if config_hash is None:
        return f"{journal_path}:{candles_path}"

    identity = SessionIdentity(
        session_id=name,
        run_id=name,
        parent_run_id=None,
        start_mode="FRESH",
        config_hash=config_hash,
        producer_commit="a" * 40,
        schema_version=SESSION_SCHEMA_VERSION,
        market="US",
        currency="USD",
        initial_equity=100_000.0,
        broker_mode="PAPER",
        ai_provider="fake",
    )
    manifest = SessionManifest.create(identity, "2026-07-07T13:00:00Z").finalize(
        "2026-07-07T21:00:00Z", completion_status="FULL")
    manifest_path = str(journal_path) + ".manifest.json"
    write_manifest(manifest_path, manifest)
    return f"{journal_path}:{candles_path}:{manifest_path}"


def test_cli_end_to_end_reads_real_sqlite(tmp_path, capsys):
    journal_path = tmp_path / "journal_probe.sqlite"
    candles_path = tmp_path / "market_probe.sqlite"

    cs = CandleStore(str(candles_path))
    cs.upsert_many([_c("AAA", 0, 100.0), _c("AAA", 30, 95.0)])  # 결정봉 100 → +30m 95(-5%)
    cs.flush()
    cs.close()

    js = SqliteJournalStore(str(journal_path))
    js.save_cycle(JournalCycle(
        cycle_id="c1", cycle_ts="2026-07-07T14:00:00Z", session="REGULAR",
        universe=["AAA"], status="RAN", reason=None, error=None,
        outcomes=[{"symbol": "AAA", "candidate_id": "cid1", "action": "ORDERED"}],
        shadow={"shadow_version": "1", "policy_version": "1",
                "decision_time": "2026-07-07T14:00:00Z",
                "market_context_snapshot": {"benchmark_symbol": "QQQ",
                                            "benchmark_trend": "DOWN"},
                "policies": [{"policy_id": "A", "would_allow": False,
                              "reason_codes": ["CLOSE_BELOW_EMA"]}],
                "candidate_ids": ["cid1"]}))
    js.flush()
    js.close()

    rc = cli.main(["--session", f"{journal_path}:{candles_path}",
                   "--horizons", "30", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["meta"]["n_sessions"] == 1
    assert out["meta"]["cohorts_by_market"]["US"] == {
        "status": "UNVERIFIED_SINGLE_SESSION",
        "config_hash": None,
        "session_ids": ["journal_probe"],
    }
    assert out["meta"]["markets"] == ["US"]        # manifest 없어도 레코드 market 로 US
    assert "A" in out["meta"]["policies"]
    report = out["reports"]["US"]["A"]["30"]
    # 조건 #5 는 항상 DEFER, 세션 1개라 게이트 NOT_MET
    assert report["gate_status"] == "NOT_MET"
    cost = next(c for c in report["criteria"] if c["key"] == "cost_mdd")
    assert cost["status"] == "DEFER"


def test_cli_fails_closed_before_aggregating_different_config_hashes(tmp_path, capsys):
    first = _seed_session(tmp_path, "s001", config_hash="1" * 64)
    second = _seed_session(tmp_path, "s002", config_hash="2" * 64)

    rc = cli.main([
        "--session", first,
        "--session", second,
        "--horizons", "30",
        "--json",
    ])

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "config_hash cohort 혼합" in captured.err
    assert "s001" in captured.err and "s002" in captured.err


def test_cli_reports_verified_single_config_hash_cohort(tmp_path, capsys):
    config_hash = "f" * 64
    first = _seed_session(tmp_path, "s001", config_hash=config_hash)
    second = _seed_session(tmp_path, "s002", config_hash=config_hash)

    rc = cli.main([
        "--session", first,
        "--session", second,
        "--horizons", "30",
        "--json",
    ])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    cohort = out["meta"]["cohorts_by_market"]["US"]
    assert cohort["status"] == "VERIFIED"
    assert cohort["config_hash"] == config_hash
    assert cohort["session_ids"] == ["s001", "s002"]


def test_cli_fails_closed_when_multi_session_config_hash_is_unknown(tmp_path, capsys):
    first = _seed_session(tmp_path, "s001", config_hash=None)
    second = _seed_session(tmp_path, "s002", config_hash=None)

    rc = cli.main([
        "--session", first,
        "--session", second,
        "--horizons", "30",
        "--json",
    ])

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "config_hash 누락" in captured.err


def test_different_config_hashes_are_allowed_across_market_cells():
    def inputs(session_id, market, config_hash):
        return SessionInputs(
            session_id=session_id,
            market=market,
            completion_status="FULL",
            manual_flatten_count=0,
            regime="UP",
            started_at="2026-07-07T13:00:00Z",
            config_hash=config_hash,
        )

    cohorts = cli._verify_config_cohorts([
        (inputs("us-s001", "US", "1" * 64), {}),
        (inputs("kr-s001", "KR", "2" * 64), {}),
    ])

    assert cohorts["US"]["config_hash"] == "1" * 64
    assert cohorts["KR"]["config_hash"] == "2" * 64
