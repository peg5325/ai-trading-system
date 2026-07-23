"""비공식 backfill CLI cutoff 인자 + partial remediation CLI(이슈 #72).

- 비공식 backfill: --completed-through 미지정 → stderr 비공식 경고(조용한 fail-open 금지),
  미래/형식오류 cutoff → creds 접근 전에 exit 2.
- remediation CLI: dry-run 무변경, --execute 는 expected 필수, 정확 대상만 삭제.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

import scripts.history_backfill as gen
import scripts.history_partial_remediation as rem
from src.history.store import HistoryStore, ImportStats
from src.models import Candle


def _utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


AFTER_CLOSE = _utc("2025-07-15T20:30")


def test_generic_cli_future_cutoff_fails_before_creds(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("creds accessed before cutoff fail-closed")
    monkeypatch.setattr(gen, "load_settings", _boom)
    monkeypatch.setattr(gen, "TossMarketDataProvider", _boom)
    monkeypatch.setattr(gen, "HistoryStore", _boom)
    code = gen.main(["--symbols", "QQQ", "--interval", "1m",
                     "--completed-through", "2025-07-16"], now_utc=AFTER_CLOSE)
    assert code == 2


def test_generic_cli_bad_cutoff_format_fails(monkeypatch):
    monkeypatch.setattr(gen, "load_settings",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no creds")))
    code = gen.main(["--symbols", "QQQ", "--completed-through", "2025/07/15"],
                    now_utc=AFTER_CLOSE)
    assert code == 2


def test_generic_cli_refuses_canonical_without_cutoff(monkeypatch, capsys):
    # cutoff 없이 canonical DB(기본 --db)에 쓰는 것은 하드 게이트로 거부(조용한 fail-open 금지).
    def _boom(*a, **k):
        raise AssertionError("creds accessed before canonical fail-open gate")
    monkeypatch.setattr(gen, "load_settings", _boom)
    code = gen.main(["--symbols", "QQQ", "--interval", "1m"], now_utc=AFTER_CLOSE)
    assert code == 2
    err = capsys.readouterr().err
    assert "거부" in err and "canonical" in err


def test_generic_cli_warns_on_scratch_without_cutoff(monkeypatch, capsys, tmp_path):
    # 비 canonical(scratch) 경로는 경고 후 진행(creds 없음 → exit 2). 경고가 stderr 에 남는지 확인.
    class FakeSettings:
        has_credentials = False
    monkeypatch.setattr(gen, "load_settings", lambda: FakeSettings())
    code = gen.main(["--db", str(tmp_path / "scratch.sqlite"),
                     "--symbols", "QQQ", "--interval", "1m"], now_utc=AFTER_CLOSE)
    assert code == 2
    assert "비공식" in capsys.readouterr().err


def test_generic_cli_canonical_optin_allows(monkeypatch, capsys):
    # 명시 opt-in 이면 canonical 도 진행(creds 없음 → exit 2). 게이트 통과 확인.
    class FakeSettings:
        has_credentials = False
    monkeypatch.setattr(gen, "load_settings", lambda: FakeSettings())
    code = gen.main(["--symbols", "QQQ", "--interval", "1m",
                     "--allow-unofficial-canonical"], now_utc=AFTER_CLOSE)
    assert code == 2  # creds 없음에서 멈춤(게이트는 통과)
    assert "거부" not in capsys.readouterr().err


# ---- remediation CLI ----
def _c(symbol, iso_et, interval="1m"):
    m = datetime.fromisoformat(iso_et)
    return Candle.from_moment(symbol=symbol, interval=interval, moment=m,
                              o=1, h=2, low=0.5, c=1.5, v=10.0, currency="USD", adjusted=True)


def _seed(dbp):
    with HistoryStore(dbp) as s:
        s.upsert_candles([_c("QQQ", "2025-07-16T09:31:00-04:00"),
                          _c("QQQ", "2025-07-16T09:32:00-04:00"),
                          _c("QQQ", "2025-07-15T09:31:00-04:00")],
                         source="toss_backfill", run_id="seed",
                         stats=ImportStats(source="toss_backfill"))
        s.commit()


def _count(dbp, day):
    con = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM candle_history WHERE symbol='QQQ' "
                           "AND interval='1m' AND substr(ts_exchange,1,10)=?",
                           (day,)).fetchone()[0]
    finally:
        con.close()


def test_remediation_cli_dry_run_no_mutation(tmp_path, capsys):
    dbp = str(tmp_path / "m.sqlite")
    _seed(dbp)
    code = rem.main(["--db", dbp, "--symbol", "QQQ", "--interval", "1m",
                     "--days", "2025-07-16", "--backup-dir", str(tmp_path / "bk")])
    assert code == 0
    assert _count(dbp, "2025-07-16") == 2       # 무변경
    out = capsys.readouterr().out
    assert "dry-run" in out and "--execute" in out


def test_remediation_cli_execute_requires_expected(tmp_path):
    dbp = str(tmp_path / "m.sqlite")
    _seed(dbp)
    code = rem.main(["--db", dbp, "--symbol", "QQQ", "--interval", "1m",
                     "--days", "2025-07-16", "--backup-dir", str(tmp_path / "bk"),
                     "--execute"])
    assert code == 2
    assert _count(dbp, "2025-07-16") == 2       # 무변경


def test_remediation_cli_execute_deletes_target(tmp_path):
    dbp = str(tmp_path / "m.sqlite")
    _seed(dbp)
    from src.history.partial_remediation import identify_partial
    info = identify_partial(dbp, "QQQ", "1m", ["2025-07-16"])
    code = rem.main(["--db", dbp, "--symbol", "QQQ", "--interval", "1m",
                     "--days", "2025-07-16", "--backup-dir", str(tmp_path / "bk"),
                     "--receipt", str(tmp_path / "r.jsonl"),
                     "--execute", "--expected-rows", str(info["row_count"]),
                     "--expected-hash", info["content_hash"]])
    assert code == 0
    assert _count(dbp, "2025-07-16") == 0       # 대상 삭제
    assert _count(dbp, "2025-07-15") == 1       # 나머지 보존


def test_remediation_cli_execute_requires_receipt(tmp_path):
    # ★리뷰 P2: --execute 는 감사 receipt 필수 — 없으면 DB 열기 전 거부(무변경).
    dbp = str(tmp_path / "m.sqlite")
    _seed(dbp)
    from src.history.partial_remediation import identify_partial
    info = identify_partial(dbp, "QQQ", "1m", ["2025-07-16"])
    code = rem.main(["--db", dbp, "--symbol", "QQQ", "--interval", "1m",
                     "--days", "2025-07-16", "--backup-dir", str(tmp_path / "bk"),
                     "--execute", "--expected-rows", str(info["row_count"]),
                     "--expected-hash", info["content_hash"]])  # no --receipt
    assert code == 2
    assert _count(dbp, "2025-07-16") == 2       # 무변경
