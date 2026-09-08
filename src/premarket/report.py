"""Deterministic JSON/Markdown rendering and immutable evidence writes."""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                       allow_nan=False) + "\n").encode("utf-8")


def render_markdown(result: Mapping[str, Any]) -> str:
    brief = result["brief"]
    schedule = result["snapshot"]["payload"]["schedule"]
    lines = [
        "# 장전 Daily Brief (read-only shadow)",
        "",
        f"- trading day: `{brief['trading_day']}`",
        f"- as-of: `{brief['as_of']}` · freeze: `{brief['expires_at']}`",
        f"- status: **{brief['status']}**",
        f"- policy: `{brief['direction_policy']}` · executable: `{brief['executable']}`",
        f"- run: `{brief['run_id']}`",
        f"- snapshot SHA-256: `{brief['snapshot_sha256']}`",
        f"- artifact SHA-256: `{result['artifact_sha256']}`",
        "",
        "## NYSE 상대 스케줄",
        "",
        f"- primary: `{schedule['primary_run_utc']}`",
        f"- verification: `{schedule['verification_utc']}`",
        f"- freeze: `{schedule['freeze_utc']}`",
        f"- open: `{schedule['market_open_utc']}`",
        "",
        "## 판정",
        "",
        f"- reason codes: `{json.dumps(brief['reason_codes'], ensure_ascii=False)}`",
        "- 주문 생성: **0건** (이 산출물은 주문 권한이 없다)",
        "",
        "## LONG_WATCH 후보 (최대 3)",
        "",
        "| 순위 | 종목 | 점수 | 승인 전략 | 증거 URL |",
        "| ---: | --- | ---: | --- | --- |",
    ]
    for rank, item in enumerate(brief["watchlist"], start=1):
        links = "<br>".join(_markdown_cell(evidence["source_url"])
                            for evidence in item["evidence"])
        lines.append(
            f"| {rank} | {item['symbol']} | {item['score']:.6f} | "
            f"{item['strategy_template_id'] or '-'} "
            f"({'approved' if item['strategy_template_approved'] else 'not approved'}) | {links} |"
        )
    if not brief["watchlist"]:
        lines.append("| - | - | - | - | - |")
    lines.extend([
        "",
        "## 안전 경계",
        "",
        "- 실제 뉴스/지표 API 수집은 이 PR 범위 밖이다. 입력은 정규화된 로컬 artifact만 받는다.",
        "- SHORT 판단, 주문, 브로커, 실계좌, 자동 승격은 구현하지 않았다.",
        "- 누락·지연·충돌·미래 정보 또는 승인 전략 부재 시 `NO_TRADE`로 닫힌다.",
        "",
    ])
    return "\n".join(lines)


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "%7C").replace("\r", " ").replace("\n", " ")


def write_immutable(path: str | Path, content: bytes) -> None:
    """Write once atomically; identical reruns are idempotent."""
    target = os.path.abspath(os.fspath(path))
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    if os.path.exists(target):
        with open(target, "rb") as stream:
            if stream.read() != content:
                raise FileExistsError(f"immutable artifact differs: {target}")
        return
    partial = target + ".partial"
    if os.path.exists(partial):
        raise FileExistsError(f"stale partial exists: {partial}")
    try:
        with open(partial, "xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, target)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if os.path.exists(partial):
            os.remove(partial)
        raise


def write_immutable_pair(first: tuple[str | Path, bytes],
                         second: tuple[str | Path, bytes]) -> None:
    """Preflight both outputs before either write, then persist idempotently.

    A process crash between writes is recoverable because the completed first
    file must be byte-identical on retry.  A pre-existing conflicting sibling is
    rejected before any new file is created.
    """
    for path, content in (first, second):
        target = os.path.abspath(os.fspath(path))
        if os.path.exists(target):
            with open(target, "rb") as stream:
                if stream.read() != content:
                    raise FileExistsError(f"immutable artifact differs: {target}")
    write_immutable(*first)
    write_immutable(*second)
