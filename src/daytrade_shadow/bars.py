"""Live 1m bars from Toss REST candles, in the Paper's Bar form (ET minute of the bar START)."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import Bar

NY = ZoneInfo("America/New_York")


def toss_bar(candle: dict) -> tuple[str, int, Bar]:
    """Toss labels a 1m bar with the minute it ENDS (design §8); the bar starts one minute earlier.

    Same rule as `src.daytrade_paper.store._bar` (which reads the collector's ET text); a test pins both.
    """
    start = datetime.fromisoformat(candle["timestamp"]).astimezone(NY) - timedelta(minutes=1)
    t = start.hour * 60 + start.minute
    bar = Bar(t, float(candle["openPrice"]), float(candle["highPrice"]), float(candle["lowPrice"]),
              float(candle["closePrice"]), float(candle.get("volume") or 0.0))
    return start.date().isoformat(), t, bar


class LiveBars:
    """Bars of one ET session per symbol, merged from repeated REST polls.

    At minute `now` a bar with t < now is complete (REST never revised a finished bar in R0: 0 of 514);
    bar `now` is in progress and only its open is final.
    """

    def __init__(self, day: str, close_t: int):
        self.day, self.close_t = day, close_t
        self.bars: dict[str, dict[int, Bar]] = {s: {} for s in R.SYMBOLS}
        self.in_progress: dict[str, Bar] = {}

    def merge(self, symbol: str, candles: list[dict], now: int) -> None:
        for c in candles:
            d, t, bar = toss_bar(c)
            if d != self.day:
                continue
            if t < now:
                self.bars[symbol][t] = bar
            elif t == now:
                self.in_progress[symbol] = bar

    def complete(self, now: int) -> dict[str, list[Bar]]:
        """Regular-session bars with t < now."""
        return {s: [b for t, b in sorted(v.items()) if R.OPEN <= t < min(now, self.close_t)]
                for s, v in self.bars.items()}

    def opens(self, now: int) -> dict[str, float | None]:
        return {s: (b.open if (b := self.in_progress.get(s)) is not None and b.t == now else None)
                for s in R.SYMBOLS}

    def qqq_pre(self) -> list[Bar]:
        return [b for t, b in sorted(self.bars[R.SIGNAL].items()) if t < R.OPEN]

    def missing(self, symbol: str, start: int, end: int) -> list[int]:
        return [t for t in range(start, end) if t not in self.bars[symbol]]

    def regular(self) -> dict[str, list[Bar]]:
        return self.complete(self.close_t)
