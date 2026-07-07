"""AI Learning Dataset 빌드 배치 진입점 (read-only).

runtime yaml 의 journal/candle DB 를 읽어 feature+label 데이터셋을 만들고 JSONL/Parquet 로 내보낸다.
실시간 매매에 관여하지 않는다. 원본(Journal/Candle)을 변경하지 않는다.

사용:
  ./.venv/bin/python scripts/build_dataset.py config/runtime_mixed.yaml
  ./.venv/bin/python scripts/build_dataset.py config/runtime_mixed.yaml \
      --out-dir datasets --format both --primary-window 30 \
      --start 2026-07-07T00:00:00Z --end 2026-07-08T00:00:00Z
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.dataset.builder import DatasetBuilder, LabelConfig  # noqa: E402
from src.dataset.exporters import JsonlExporter, ParquetExporter  # noqa: E402
from src.journal.sqlite_store import SqliteJournalStore  # noqa: E402
from src.runtime.config import load_yaml  # noqa: E402
from src.storage.candle_store import CandleStore  # noqa: E402


def _parse_ts(s: str | None):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description="AI Learning Dataset Builder (read-only batch)")
    ap.add_argument("config", help="runtime yaml (journal/candle DB 경로 획득)")
    ap.add_argument("--out-dir", default="datasets", help="출력 디렉터리 (기본 datasets)")
    ap.add_argument("--format", choices=["jsonl", "parquet", "both"], default="both")
    ap.add_argument("--name", default=None, help="출력 파일 basename (기본: dataset_<UTC>)")
    ap.add_argument("--start", default=None, help="cycle_ts 하한 (ISO Z)")
    ap.add_argument("--end", default=None, help="cycle_ts 상한 (ISO Z)")
    ap.add_argument("--symbols", default=None, help="쉼표구분 심볼 필터")
    ap.add_argument("--primary-window", type=int, default=30, help="대표 라벨 창(분)")
    ap.add_argument("--windows", default="5,15,30,60", help="forward 창(분) 쉼표구분")
    ap.add_argument("--win-threshold", type=float, default=0.002)
    ap.add_argument("--loss-threshold", type=float, default=0.002)
    ap.add_argument("--drop-incomplete", action="store_true",
                    help="primary 창을 채우지 못한 관찰(최근 데이터) 제외")
    ap.add_argument("--strategy-version", default="screener-1.0")
    args = ap.parse_args(argv)

    cfg = load_yaml(args.config)
    journal = SqliteJournalStore(cfg.journal_db)
    candles = CandleStore(cfg.candle_db)

    windows = tuple(int(x) for x in args.windows.split(",") if x.strip())
    label_cfg = LabelConfig(
        windows_min=windows, primary_window_min=args.primary_window,
        win_threshold=args.win_threshold, loss_threshold=args.loss_threshold,
        drop_incomplete_labels=args.drop_incomplete,
    )
    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    builder = DatasetBuilder(journal, candles, config=label_cfg,
                             strategy_version=args.strategy_version, built_at=built_at)

    symbols = set(s.strip() for s in args.symbols.split(",")) if args.symbols else None
    dataset = builder.build(start=_parse_ts(args.start), end=_parse_ts(args.end),
                            symbols=symbols, source=cfg.journal_db)

    stamp = built_at.replace(":", "").replace("-", "")
    base = args.name or f"dataset_{stamp}"
    out_dir = args.out_dir
    written = []
    if args.format in ("jsonl", "both"):
        p = os.path.join(out_dir, base + ".jsonl")
        n = JsonlExporter().export(dataset, p)
        written.append(f"{p} ({n} rows)")
    if args.format in ("parquet", "both"):
        p = os.path.join(out_dir, base + ".parquet")
        n = ParquetExporter().export(dataset, p)
        written.append(f"{p} ({n} rows)")

    complete = sum(1 for r in dataset if r.label.complete)
    print(f"[build_dataset] records={len(dataset)} (label 완성={complete}) "
          f"windows={windows} primary={args.primary_window}m")
    for w in written:
        print(f"[build_dataset] wrote {w}")
    journal.close()
    candles.close()


if __name__ == "__main__":
    main(sys.argv[1:])
