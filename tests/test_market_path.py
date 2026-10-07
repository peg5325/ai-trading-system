"""market_path: price path of one session from the shadow candle log. Synthetic log only."""
from __future__ import annotations

import json

from scripts.market_path import load_bars, summarize


def _row(fetched: str, candles: list[tuple[str, float, float, float]]) -> str:
    return json.dumps({"fetched": fetched, "symbol": "QQQ",
                       "candles": [{"timestamp": ts, "openPrice": str(o), "closePrice": str(c), "volume": str(v)}
                                   for ts, o, c, v in candles]})


def test_last_fetch_wins_and_in_progress_and_pre_bars_are_skipped(tmp_path):
    log = tmp_path / "candles.jsonl"
    log.write_text("\n".join([
        # 09:29 ET bar (label 13:30 UTC) is pre-open; 09:31 label is still in progress at this fetch
        _row("2026-10-06T13:31:04+00:00", [("2026-10-06T22:32:00.000+09:00", 101, 101, 5),
                                           ("2026-10-06T22:31:00.000+09:00", 100, 100.5, 10),
                                           ("2026-10-06T22:30:00.000+09:00", 99, 99, 1)]),
        _row("2026-10-06T13:32:04+00:00", [("2026-10-06T22:32:00.000+09:00", 100.5, 102, 20),
                                           ("2026-10-06T22:31:00.000+09:00", 100, 100.6, 12)]),
    ]) + "\n")
    bars = load_bars(log)["QQQ"]
    assert bars == {"09:30": (100.0, 100.6, 12.0), "09:31": (100.5, 102.0, 20.0)}


def test_summary_high_low_and_segments():
    b = {"09:30": (100.0, 101.0, 10.0), "09:45": (101.0, 103.0, 5.0), "10:00": (103.0, 99.0, 7.0)}
    s = summarize(b, prev_close=100.0)
    assert s["open"] == 100.0 and s["close"] == 99.0 and s["close_vs_prev_pct"] == -1.0
    assert s["high_close"] == {"t": "09:45", "price": 103.0, "vs_prev_pct": 3.0}
    assert s["low_close"]["t"] == "10:00"
    assert [(g["t"], g["volume"]) for g in s["segments"]] == [("09:30", 15.0), ("10:00", 7.0)]
