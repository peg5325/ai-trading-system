"""과거 캔들 백필: Toss 에서 페이징 수집 -> Canonical 변환 -> SQLite 저장.

사용:
  ./.venv/bin/python -m src.ingest.backfill SOXL 1m --pages 3
  ./.venv/bin/python -m src.ingest.backfill SOXL 1d --pages 5

주문은 없다. 읽기 + 저장만.
"""
from __future__ import annotations

import argparse

from src.config import load_settings
from src.providers.toss_market_data import TossMarketDataProvider
from src.storage.candle_store import CandleStore


def run(symbol: str, interval: str, pages: int, page_size: int = 200) -> None:
    settings = load_settings()
    if not settings.has_credentials:
        raise SystemExit(".env 의 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 이 비어있습니다.")

    print(f"[backfill] {symbol} {interval}  pages={pages} page_size={page_size}")
    total = 0
    with TossMarketDataProvider(settings) as md, CandleStore(settings.db_path) as store:
        batch: list = []
        for candle in md.iter_history(symbol, interval, max_pages=pages, page_size=page_size):
            batch.append(candle)
            if len(batch) >= 500:
                total += store.upsert_many(batch)
                batch.clear()
        if batch:
            total += store.upsert_many(batch)

        stored = store.count(symbol, interval)
        lo, hi = store.range(symbol, interval)
        print(f"[backfill] 수집/저장 {total}개 (upsert)")
        print(f"[store]    {symbol} {interval} 총 {stored}개")
        print(f"[store]    범위(UTC): {lo}  ~  {hi}")
        print("[store]    최신 3개:")
        for c in store.latest(symbol, interval, limit=3):
            print(f"           {c.ts_exchange.isoformat()}  [{c.session:9}] "
                  f"O={c.open} H={c.high} L={c.low} C={c.close} V={c.volume} {c.currency}")


def main() -> None:
    p = argparse.ArgumentParser(description="Toss 캔들 백필")
    p.add_argument("symbol")
    p.add_argument("interval", choices=["1m", "1d"])
    p.add_argument("--pages", type=int, default=3, help="페이징 최대 페이지 수(호출 제한 보호)")
    p.add_argument("--page-size", type=int, default=200)
    args = p.parse_args()
    run(args.symbol, args.interval, args.pages, args.page_size)


if __name__ == "__main__":
    main()
