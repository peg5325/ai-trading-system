"""TossMarketDataProvider — Toss raw 캔들을 Canonical Candle 로 변환.

지표 계산/판단은 여기 없다. 오직 Toss 응답 -> Canonical 변환만.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Iterator

from src.config import Settings
from src.models import Candle
from src.providers.toss_client import TossClient

# raw_sink 콜백 시그니처(계약 §4.1): provider 응답 payload 를 **parse 전에** 넘겨받는다.
# RawSnapshotStore.emit 이 이 시그니처를 만족한다. Any 로 두어 순환 import 를 피한다.
RawSink = Callable[..., object]


def _to_float(v: object) -> float:
    return float(str(v))


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TossMarketDataProvider:
    def __init__(self, settings: Settings):
        self._client = TossClient(settings)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TossMarketDataProvider":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _parse(self, symbol: str, interval: str, raw: dict) -> Candle:
        # Toss timestamp 예: "2026-07-06T13:47:00.000+09:00" (offset 포함)
        moment = datetime.fromisoformat(raw["timestamp"])
        return Candle.from_moment(
            symbol=symbol, interval=interval, moment=moment,
            o=_to_float(raw["openPrice"]), h=_to_float(raw["highPrice"]),
            low=_to_float(raw["lowPrice"]), c=_to_float(raw["closePrice"]),
            v=_to_float(raw.get("volume", 0)),
            currency=str(raw.get("currency", "")),
            adjusted=True,  # candles 는 기본 adjusted=true
        )

    def fetch_page(self, symbol: str, interval: str, count: int = 200,
                   before: str | None = None, *,
                   raw_sink: RawSink | None = None) -> tuple[list[Candle], str | None]:
        """한 페이지를 canonical 캔들 리스트 + nextBefore 로 반환.

        raw_sink 가 주어지면 canonical 변환 **전에** 응답 payload 원본을 넘긴다(계약 §4.1
        immutable raw 스냅샷). raw_sink 는 파싱 성공 여부와 무관하게 응답 body 를 보존한다.
        """
        requested_at = _utc_iso()
        result = self._client.get_candles(symbol, interval, count=count, before=before)
        received_at = _utc_iso()
        if raw_sink is not None:
            # ★parse 전 보존: 아래 _parse 리스트컴프리헨션보다 먼저 원본 payload 를 넘긴다.
            raw_sink(symbol=symbol, interval=interval, before=before,
                     requested_at=requested_at, received_at=received_at,
                     source="toss", payload=result)
        raws = result.get("candles", []) or []
        candles = [self._parse(symbol, interval, r) for r in raws]
        return candles, result.get("nextBefore")

    def recent(self, symbol: str, interval: str, count: int = 300) -> list[Candle]:
        """최근 캔들을 시간 오름차순으로 반환 (실시간 루프용)."""
        candles, _ = self.fetch_page(symbol, interval, count=count)
        return sorted(candles, key=lambda c: c.ts_utc)

    def iter_history(self, symbol: str, interval: str, *,
                     max_pages: int, page_size: int = 200,
                     sleep_s: float = 0.4) -> Iterator[Candle]:
        """nextBefore 를 따라 과거로 페이징하며 canonical 캔들을 yield.

        max_pages 로 호출 수를 제한한다(rate limit 보호).
        """
        import time

        before: str | None = None
        for _ in range(max_pages):
            candles, nxt = self.fetch_page(symbol, interval, count=page_size, before=before)
            if not candles:
                break
            yield from candles
            if not nxt:
                break
            before = nxt
            time.sleep(sleep_s)
