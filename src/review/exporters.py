"""ReviewStore → 파일 Export.

ReviewStore(SQLite)가 Single Source of Truth 이고, JSONL 은 Export 포맷으로만 쓴다.
용도: GPT/Claude 분석, 외부 공유, 회귀 테스트, LLM Batch 입력.
각 줄 = 한 ReviewRecord dict (review_source 포함). 옵션으로 meta 헤더 라인.
"""
from __future__ import annotations

import json
from pathlib import Path


class ReviewJsonlExporter:
    def __init__(self, include_meta_header: bool = True, ensure_ascii: bool = False):
        self._meta_header = include_meta_header
        self._ensure_ascii = ensure_ascii

    def export(self, records, path: str | Path, *, meta: dict | None = None) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        records = list(records)
        with open(path, "w", encoding="utf-8") as f:
            if self._meta_header:
                header = {"__meta__": {**(meta or {}), "n_records": len(records)}}
                f.write(json.dumps(header, ensure_ascii=self._ensure_ascii) + "\n")
            for rec in records:
                f.write(json.dumps(rec.to_dict(), ensure_ascii=self._ensure_ascii) + "\n")
        return len(records)
