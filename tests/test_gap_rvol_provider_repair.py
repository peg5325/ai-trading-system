from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.history.raw_snapshot import RawSnapshotStore
from src.history.session_filter import SessionFilter
from src.history.store import HistoryStore
from src.models import Candle
from src.research.gap_rvol.provider_repair import (
    RepairTarget,
    build_manifest,
    build_repair_targets,
    collect_target_day,
    data_content_sha256,
    persist_collection,
    render_manifest_report,
    request_before,
    summarize_collection,
)

ET = ZoneInfo("America/New_York")


def _candle(symbol: str, day: date, minute: int) -> Candle:
    moment = datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=ET)
    return Candle.from_moment(
        symbol=symbol,
        interval="1m",
        moment=moment,
        o=1,
        h=2,
        low=0.5,
        c=1.5,
        v=10,
        currency="USD",
        adjusted=True,
    )


class FakeProvider:
    def __init__(self, pages):
        self.pages = list(pages)

    def fetch_page(self, symbol, interval, count=200, before=None, *, raw_sink=None):
        candles, nxt = self.pages.pop(0)
        if raw_sink is not None:
            raw_sink(
                symbol=symbol,
                interval=interval,
                before=before,
                requested_at="2026-08-04T00:00:00Z",
                received_at="2026-08-04T00:00:01Z",
                source="toss",
                payload={"candles": [], "nextBefore": nxt},
            )
        return candles, nxt


def test_build_repair_targets_is_exact_tracked_intersection():
    audit = {"exclusion_audit": {"events": [
        {"day": "2024-08-05", "symbol": "SPY", "reasons": ["x"]},
        {"day": "2024-08-05", "symbol": "QQQ", "reasons": ["y"]},
    ]}}
    probe = {"observations": [
        {"target_day": "2024-08-05", "symbol": "SPY", "has_target_day": True},
        {"target_day": "2024-08-05", "symbol": "QQQ", "has_target_day": False},
    ]}
    assert build_repair_targets(audit=audit, probe=probe) == (
        RepairTarget(date(2024, 8, 5), "SPY", ("x",)),
    )


def test_request_before_uses_observed_toss_kst_cursor_form():
    assert request_before(date(2024, 8, 5)) == "2024-08-06T08:59:00.000+09:00"


def test_collect_target_day_filters_other_days_and_stops_at_pre_boundary(tmp_path):
    target = RepairTarget(date(2024, 8, 5), "SPY", ("x",))
    page1 = [_candle("SPY", target.day, minute) for minute in range(700, 900)]
    page2 = [_candle("SPY", target.day, minute) for minute in range(500, 700)]
    page2.append(_candle("SPY", date(2024, 8, 2), 959))
    provider = FakeProvider([(page1, "cursor"), (page2, None)])
    collection = collect_target_day(
        provider,
        RawSnapshotStore(str(tmp_path / "raw")),
        target,
        spacing=0,
        sleep=lambda _: None,
    )
    assert collection.pages_fetched == 2
    assert collection.stopped_reason == "crossed_pre_boundary"
    assert {c.ts_exchange.date() for c in collection.candles} == {target.day}
    assert len(collection.candles) == 400


def test_summary_requires_exact_regular_minutes():
    target = RepairTarget(date(2024, 8, 5), "SPY", ("x",))
    full = tuple(_candle("SPY", target.day, minute) for minute in range(570, 960))
    collection = type("Collection", (), {
        "target": target,
        "candles": full,
        "pages_fetched": 2,
        "http_429": 0,
        "stopped_reason": "done",
    })()
    summary = summarize_collection(collection)
    assert summary["full_regular_session"] is True
    assert summary["required_feature_minutes_complete"] is True

    incomplete = type("Collection", (), {
        **collection.__dict__,
        "target": target,
        "candles": full[:-1],
        "pages_fetched": 2,
        "http_429": 0,
        "stopped_reason": "done",
    })()
    assert summarize_collection(incomplete)["full_regular_session"] is False


def test_persisted_candidate_is_isolated_and_content_hash_deterministic(tmp_path):
    target = RepairTarget(date(2024, 8, 5), "SPY", ("x",))
    candles = tuple(_candle("SPY", target.day, minute) for minute in range(570, 575))
    collection = type("Collection", (), {
        "target": target,
        "candles": candles,
        "pages_fetched": 1,
        "http_429": 0,
        "stopped_reason": "done",
    })()
    db = tmp_path / "candidate.sqlite"
    with HistoryStore(str(db)) as store:
        persist_collection(store, collection, SessionFilter.default())
        first = data_content_sha256(store)
        persist_collection(store, collection, SessionFilter.default())
        assert data_content_sha256(store) == first
        assert store.total_rows() == 5


def test_tracked_inputs_freeze_76_targets():
    audit = json.loads(Path("docs/research/AUDIT_P0_US30_GAP_RVOL_D9.json").read_text())
    probe = json.loads(Path("docs/research/PROBE_P0_US30_GAP_RVOL_D9_TOSS.json").read_text())
    targets = build_repair_targets(audit=audit, probe=probe)
    assert len(targets) == 76
    assert all(target.day <= date(2024, 8, 6) for target in targets)


def test_tracked_candidate_manifest_and_report_are_consistent():
    json_path = Path(
        "docs/research/MANIFEST_P0_US30_GAP_RVOL_D9_TOSS_REPAIR_CANDIDATE.json"
    )
    md_path = json_path.with_suffix(".md")
    manifest = json.loads(json_path.read_text(encoding="utf-8"))
    assert manifest["target_count"] == 76
    assert manifest["full_regular_session_count"] == 66
    assert manifest["required_feature_minutes_complete_count"] == 66
    assert render_manifest_report(manifest) == md_path.read_text(encoding="utf-8")


def test_manifest_keeps_scope_closed():
    target = RepairTarget(date(2024, 8, 5), "SPY", ("x",))
    summary = {
        "day": "2024-08-05",
        "symbol": "SPY",
        "full_regular_session": True,
        "required_feature_minutes_complete": True,
        "http_429": 0,
    }
    manifest = build_manifest(
        targets=[target],
        summaries=[summary],
        raw_manifest={"content_file_count": 1, "raw_content_sha256": "a" * 64},
        repair_db_content_sha256="b" * 64,
        repair_db_rows=390,
        generated_at_utc="2026-08-04T00:00:00Z",
    )
    assert manifest["scope"]["canonical_db_written"] is False
    assert manifest["scope"]["blind_oos_opened"] is False
    assert manifest["scope"]["performance_computed"] is False
    report = render_manifest_report(manifest)
    assert "PARTIAL_REPAIR_CANDIDATE" in report
    assert "canonical DB written: `False`" in report
