from __future__ import annotations

from datetime import datetime, timezone

from scripts.measure_toss_realtime import (
    bar_start,
    capture_rows,
    capture_summary,
    completed_rest_bars,
    gap_stats,
    label_check,
    parse_ts,
    ws_minute_volumes,
)

UTC = timezone.utc


def _utc(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 10, 5, h, m, s, tzinfo=UTC)


def test_bar_start_reads_end_minute_label():
    assert bar_start(parse_ts("2026-10-05T22:31:00.000+09:00")) == _utc(13, 30)


def test_ws_minute_volumes_buckets_by_trade_timestamp():
    trades = [
        {"symbol": "TQQQ", "ts": "2026-10-05T22:30:01.000+09:00", "volume": "100"},
        {"symbol": "TQQQ", "ts": "2026-10-05T22:30:59.000+09:00", "volume": "50"},
        {"symbol": "TQQQ", "ts": "2026-10-05T22:31:00.000+09:00", "volume": "7"},
    ]
    assert ws_minute_volumes(trades) == {("TQQQ", _utc(13, 30)): 150.0, ("TQQQ", _utc(13, 31)): 7.0}


def _poll(fetched: str, label: str, volume: str, symbol: str = "TQQQ") -> dict:
    return {"fetched": fetched, "symbol": symbol, "candles": [{"timestamp": label, "volume": volume}]}


def test_completed_rest_bars_ignores_in_progress_and_flags_revisions():
    polls = [
        _poll("2026-10-05T13:30:40+00:00", "2026-10-05T22:31:00.000+09:00", "90"),   # in progress
        _poll("2026-10-05T13:31:05+00:00", "2026-10-05T22:31:00.000+09:00", "150"),  # complete
        _poll("2026-10-05T13:31:20+00:00", "2026-10-05T22:31:00.000+09:00", "155"),  # late revision
    ]
    bars = completed_rest_bars(polls)
    assert bars == {("TQQQ", _utc(13, 30)): {"volume": 155.0, "first_volume": 150.0, "revised": True,
                                             "observations": 2}}


def test_capture_rows_and_summary_compare_same_bar():
    ws = {("TQQQ", _utc(13, 30)): 120.0}
    rest = {("TQQQ", _utc(13, 30)): {"volume": 150.0}, ("TQQQ", _utc(13, 31)): {"volume": 50.0}}
    rows = capture_rows(ws, rest, ["TQQQ"], _utc(13, 30), _utc(13, 32))
    assert [r["capture"] for r in rows] == [0.8, 0.0]
    summary = capture_summary(rows)
    assert summary["missing_pct"] == 40.0
    assert summary["capture_min"] == 0.0


def test_gap_stats():
    assert gap_stats([0.0, 1.0, 2.0, 10.0]) == {"n": 4, "gap_median_s": 1.0, "gap_p95_s": 1.0, "gap_max_s": 8.0}
    assert gap_stats([5.0])["gap_median_s"] is None


def test_label_check_sees_live_end_minute_bar():
    polls = [
        _poll("2026-10-05T13:30:10+00:00", "2026-10-05T22:31:00.000+09:00", "10"),
        _poll("2026-10-05T13:30:40+00:00", "2026-10-05T22:31:00.000+09:00", "30"),
        _poll("2026-10-05T13:31:10+00:00", "2026-10-05T22:31:00.000+09:00", "40"),
        _poll("2026-10-05T13:31:40+00:00", "2026-10-05T22:31:00.000+09:00", "40"),
    ]
    result = label_check(polls)
    assert result["newest_label_is_current_end_minute"] == 2
    assert result["bars_seen_in_progress_twice"] == 1 and result["of_which_volume_grew"] == 1
    assert result["of_which_changed_after_end"] == 0
