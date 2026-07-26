"""resampler_parity_check CLI 계약 (이슈 #76).

- 공식 게이트(`research_load(purpose="coverage")`) 경유·성과 미계산
- tracked snapshot identity 대조 fail-closed
- blind OOS 날짜는 명시 플래그 없이 거부
- receipt sanitized(절대경로 없음)·결정론 필드
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import scripts.resampler_parity_check as cli
from scripts.resampler_parity_check import canon, main
from src.history.store import HistoryStore, ImportStats
from src.models import ET, Candle

DAY = "2026-01-20"          # blind OOS(2026-01-21~) 직전 거래일
OOS_DAY = "2026-03-02"      # blind OOS 안쪽
REPO = Path(__file__).resolve().parent.parent


def _seed(db, symbol="SPY", day=(2026, 1, 20), minutes=45):
    start = datetime(*day, 9, 30, tzinfo=ET)
    candles = [
        Candle.from_moment(symbol, "1m", start + timedelta(minutes=i),
                           100.0 + i, 102.0 + i, 99.0 + i, 101.0 + i, 10.0 * (i + 1),
                           "USD", True)
        for i in range(minutes)
    ]
    with HistoryStore(str(db)) as store:
        store.upsert_candles(candles, source="test", run_id="RUN-76",
                             stats=ImportStats(source="test"))
        store.commit()
    return candles


def test_cli_reports_parity_on_seeded_day(tmp_path):
    db = tmp_path / "snap.sqlite"
    _seed(db)
    out = tmp_path / "receipt.json"

    assert main(["--db", str(db), "--symbols", "SPY", "--days", DAY,
                 "--out", str(out), "--skip-identity-check"]) == 0

    receipt = json.loads(out.read_text(encoding="utf-8"))
    assert receipt["verdict"] == "PARITY_OK"
    assert receipt["performance_computed"] is False
    assert receipt["purpose"] == "coverage"
    assert receipt["summary"]["cells_mismatched"] == 0
    assert receipt["summary"]["cells_with_data"] == 1
    assert receipt["summary"]["bars_1m_total"] == 45
    assert receipt["summary"]["bars_5m_total"] == 8      # 09:30..10:14 → 닫힌 5m 8개
    assert receipt["summary"]["bars_15m_total"] == 2     # 닫힌 15m 2개
    assert receipt["blind_oos"]["days_inside_window"] == 0
    assert set(receipt["compared_paths"]) == {
        "research_core", "live_adapter", "candidate_stream", "replay"}


def test_receipt_is_sanitized_and_has_no_absolute_paths(tmp_path):
    db = tmp_path / "snap.sqlite"
    _seed(db)
    out = tmp_path / "receipt.json"
    main(["--db", str(db), "--symbols", "SPY", "--days", DAY,
          "--out", str(out), "--skip-identity-check"])

    text = out.read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert "/Users/" not in text and "/home/" not in text
    receipt = json.loads(text)
    assert receipt["snapshot_basename_checked"] == "snap.sqlite"


def test_identity_check_rejects_foreign_snapshot(tmp_path):
    """tracked snapshot basename 과 다르면 거부(엉뚱한 DB 로 parity 를 주장하지 못하게)."""
    db = tmp_path / "not-the-snapshot.sqlite"
    _seed(db)
    with pytest.raises(SystemExit):
        main(["--db", str(db), "--symbols", "SPY", "--days", DAY,
              "--out", str(tmp_path / "r.json")])


def test_identity_check_rejects_right_name_wrong_content(tmp_path):
    """basename 만 맞춘 DB 는 **파일 SHA-256 불일치**로 거부된다(독립 리뷰 B2).

    이름 대조만 하면 아무 DB 로도 canonical hash 가 박힌 receipt 를 만들 수 있었다.
    """
    tracked = json.loads(
        (REPO / "docs" / "research"
         / "P0_US30_ORB_CLEAN_SNAPSHOT_RECEIPT.json").read_text(encoding="utf-8"))
    db = tmp_path / tracked["snapshot"]["basename"]      # 이름은 정확히 일치
    _seed(db)                                            # 내용은 전혀 다름
    out = tmp_path / "r.json"
    with pytest.raises(SystemExit, match="SHA-256"):
        main(["--db", str(db), "--symbols", "SPY", "--days", DAY, "--out", str(out)])
    assert not out.exists()


def test_skipped_identity_receipt_does_not_claim_canonical_hashes(tmp_path):
    """--skip-identity-check 로 만든 receipt 는 canonical hash 를 주장하지 않는다."""
    db = tmp_path / "snap.sqlite"
    _seed(db)
    out = tmp_path / "r.json"
    main(["--db", str(db), "--symbols", "SPY", "--days", DAY,
          "--out", str(out), "--skip-identity-check"])
    snap = json.loads(out.read_text(encoding="utf-8"))["snapshot"]
    assert snap["identity_verified"] is False
    assert "file_sha256" not in snap and "content_sha256" not in snap


def test_blind_oos_day_requires_explicit_flag(tmp_path):
    """blind OOS 날짜는 coverage 목적이라도 기본 거부(개봉 실수 방지)."""
    db = tmp_path / "snap.sqlite"
    _seed(db, day=(2026, 3, 2))
    out = tmp_path / "r.json"
    with pytest.raises(SystemExit):
        main(["--db", str(db), "--symbols", "SPY", "--days", OOS_DAY,
              "--out", str(out), "--skip-identity-check"])
    assert not out.exists()

    assert main(["--db", str(db), "--symbols", "SPY", "--days", OOS_DAY,
                 "--out", str(out), "--skip-identity-check",
                 "--allow-oos-coverage"]) == 0
    receipt = json.loads(out.read_text(encoding="utf-8"))
    assert receipt["blind_oos"]["days_inside_window"] == 1
    assert receipt["performance_computed"] is False


def test_no_data_cell_is_reported_not_silently_passed(tmp_path):
    db = tmp_path / "snap.sqlite"
    _seed(db)
    out = tmp_path / "r.json"
    rc = main(["--db", str(db), "--symbols", "ZZZZ", "--days", DAY,
               "--out", str(out), "--skip-identity-check"])
    receipt = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 1 and receipt["verdict"] == "NO_DATA"
    assert receipt["summary"]["cells_no_data"] == 1


def _perturb_core(monkeypatch):
    """core 결과를 한 봉 줄여 core != live 를 만든다."""
    real = cli.resample_1m_to_5m
    monkeypatch.setattr(cli, "resample_1m_to_5m",
                        lambda c, **kw: real(c, **kw)[:-1])


def _perturb_candidate_stream(monkeypatch):
    import src.history.candidate_stream as cs
    real = cs.build_decision_bars
    monkeypatch.setattr(cs, "build_decision_bars",
                        lambda c, s: real(c, s)[:-1])


def _perturb_replay(monkeypatch):
    class _Broken:
        def __init__(self, **kw):
            pass

        def _candles(self, symbol, as_of):
            return []
    monkeypatch.setattr(cli, "ReplaySystem", _Broken)


def _perturb_order_dependence(monkeypatch):
    """입력 순서에 따라 다른 결과를 내는 core(순서 의존 회귀 시뮬레이션)."""
    real = cli.resample_1m_to_5m

    def _flaky(c, **kw):
        out = real(c, **kw)
        return out[:-1] if c and c[0].ts_utc > c[-1].ts_utc else out
    monkeypatch.setattr(cli, "resample_1m_to_5m", _flaky)


def _perturb_opening_range(monkeypatch):
    real = cli.opening_range

    def _wrong(c, **kw):
        out = real(c, **kw)
        if not out:
            return out
        first = out[0]
        return [type(first)(**{**first.__dict__, "high": first.high + 1.0})]
    monkeypatch.setattr(cli, "opening_range", _wrong)


@pytest.mark.parametrize("perturb,tag", [
    (_perturb_core, "5min:core!=live"),
    (_perturb_candidate_stream, "5min:candidate_stream!=live"),
    (_perturb_replay, "5min:replay!=live"),
    (_perturb_order_dependence, "5min:order_dependent"),
    (_perturb_opening_range, "15m_or:ohlcv!=15m_bucket"),
])
def test_cli_actually_detects_each_divergence(tmp_path, monkeypatch, perturb, tag):
    """**checker 가 '아니오' 라고 말할 수 있는지** 검증한다(mutation review 최우선 지적).

    `compare_day` 의 탐지 분기를 전부 `if False:` 로 만들어도 기존 스위트는 green 이었다.
    receipt 가 이 PR 의 실데이터 증거인데 탐지 능력 자체가 미검증이면 증거가 아니다.
    """
    db = tmp_path / "snap.sqlite"
    _seed(db)
    out = tmp_path / "r.json"
    perturb(monkeypatch)

    rc = main(["--db", str(db), "--symbols", "SPY", "--days", DAY,
               "--out", str(out), "--skip-identity-check"])

    receipt = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 1
    assert receipt["verdict"] == "PARITY_MISMATCH"
    assert receipt["summary"]["cells_mismatched"] == 1
    assert receipt["summary"]["cells_matched"] == 0
    assert tag in receipt["cells"][0]["mismatches"]


def test_canon_has_no_performance_fields():
    """직렬화 대상에 수익률·PnL 계열 필드가 없다(성과 열람 경계)."""
    start = datetime(2026, 1, 20, 9, 30, tzinfo=ET)
    c = [Candle.from_moment("SPY", "1m", start, 1.0, 2.0, 0.5, 1.5, 10.0, "USD", True)]
    row = canon(c)[0]
    assert len(row) == 13
    assert row[0] == "SPY" and row[1] == "1m" and row[4] == "REGULAR"
