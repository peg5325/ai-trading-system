"""ORB development 결과 JSON의 결정론적 tracked Markdown 렌더러."""
from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from typing import Any


def _number(value: Any, *, digits: int = 6) -> str:
    if value in ("Infinity", "-Infinity"):
        return str(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return "Infinity" if value > 0 else "-Infinity"
    return f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def _json_inline(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def render_development_report(result: Mapping[str, Any]) -> str:
    payload = result["payload"]
    if not isinstance(payload, Mapping):
        raise TypeError("result payload가 object가 아니다")
    verdict = payload["verdict"]
    dev_gate = payload["dev_gate"]
    preconditions = payload["preconditions"]
    snapshot = payload["snapshot"]
    defect_ratios = payload["defect_ratios"]
    specs = payload["specs"]
    if not isinstance(specs, Sequence):
        raise TypeError("result specs가 배열이 아니다")

    lines = [
        "# P0-US30 ORB development walk-forward 결과",
        "",
        f"> 생성: `{result['generated_at_utc']}`  ",
        f"> payload SHA-256: `{result['payload_sha256']}`  ",
        "> 범위: development-only. blind OOS는 열지 않았다.",
        "",
        "## 1. 기계 판정",
        "",
        f"- verdict: **{verdict['value']}**",
        f"- development gate: **{dev_gate['development_gate']}**",
        f"- failed: `{_json_inline(verdict['failed'])}`",
        f"- next stage: `{verdict['next_stage']}`",
        "",
        "| 게이트 | 기준 | 실제 | 통과 |",
        "| --- | --- | --- | --- |",
    ]
    for item in dev_gate["items"]:
        lines.append(
            f"| {item['name']} | `{_json_inline(item['criterion'])}` "
            f"| `{_json_inline(item['actual'])}` | "
            f"{'PASS' if item['passed'] else 'FAIL'} |"
        )

    lines.extend([
        "",
        "## 2. 실행 전 절차 P1~P3",
        "",
        (
            f"- P1 review: `{preconditions['P1']['review_status']}` — "
            f"{preconditions['P1']['review_evidence_url']}"
        ),
        (
            "- P1 implementation merge: "
            f"`{preconditions['P1']['implementation_merge_commit']}`"
        ),
        (
            "- P2 loader AST boundary: "
            f"`{preconditions['P2']['loader_boundary_status']}` — "
            f"`{preconditions['P2']['loader_boundary_test']}`"
        ),
        f"- P3 snapshot identity: `{preconditions['P3']['snapshot_identity']}`",
        f"- P3 tracked manifest: `{preconditions['P3']['tracked_manifest']}`",
        (
            "- P3 tracked manifest SHA-256: "
            f"`{preconditions['P3']['tracked_manifest_file_sha256']}`"
        ),
        "",
        "## 3. 입력 identity",
        "",
        f"- snapshot: `{snapshot['basename']}`",
        f"- file: `{snapshot['snapshot_file_sha256']}`",
        f"- content: `{snapshot['snapshot_content_sha256']}`",
        f"- manifest: `{snapshot['manifest_data_sha256']}`",
        f"- raw: `{snapshot['raw_content_sha256']}`",
        f"- producer commit: `{payload['producer_commit']}`",
        f"- prereg merge: `{payload['prereg']['prereg_merge_commit']}`",
        "",
        "## 4. 8개 파라미터화 — 공식 비용",
        "",
        (
            "| spec | 권한 | OR | gap | RVOL | target | trades | days | "
            "net PnL | expectancy | PF | MDD |"
        ),
        (
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | "
            "---: | ---: | ---: | ---: |"
        ),
    ])
    for spec in specs:
        stats = spec["stats"]["official"]
        params = spec["params"]
        lines.append(
            f"| {spec['name']} | {spec['authority']} "
            f"| {params['opening_range_minutes']} "
            f"| {_number(params['gap_threshold_abs'])} "
            f"| {_number(params['rvol_threshold'])} "
            f"| {_number(params['target_r_multiple'])} "
            f"| {stats['trade_count']} "
            f"| {stats['trading_days_with_trade']} "
            f"| {_number(stats['cumulative_net_pnl'])} "
            f"| {_number(stats['net_expectancy'])} "
            f"| {_number(stats['profit_factor'])} "
            f"| {_number(stats['mdd'])} |"
        )

    lines.extend([
        "",
        "## 5. Primary 비용 민감도",
        "",
        "| 비용 | trades | net PnL | expectancy | PF | MDD | positive folds |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for level in ("low", "official", "stress"):
        stats = payload["stats"][level]
        lines.append(
            f"| {level} | {stats['trade_count']} "
            f"| {_number(stats['cumulative_net_pnl'])} "
            f"| {_number(stats['net_expectancy'])} "
            f"| {_number(stats['profit_factor'])} "
            f"| {_number(stats['mdd'])} "
            f"| {stats['positive_folds']} |"
        )

    lines.extend([
        "",
        "## 6. 결함 및 감사",
        "",
        (
            "- data ratio: "
            f"`{defect_ratios['data']['numerator']}/"
            f"{defect_ratios['data']['denominator']} = "
            f"{_number(defect_ratios['data']['ratio'])}`"
        ),
        (
            "- operational ratio: "
            f"`{defect_ratios['operational']['numerator']}/"
            f"{defect_ratios['operational']['denominator']} = "
            f"{_number(defect_ratios['operational']['ratio'])}`"
        ),
        f"- major defects: `{defect_ratios['major_defect_count']}`",
        f"- tracked conflict total: `{defect_ratios['conflict_total']}`",
        f"- excluded summary: `{_json_inline(payload['excluded'])}`",
        f"- skipped summary: `{_json_inline(payload['skipped'])}`",
        "",
        "## 7. 범위 제한",
        "",
        (
            f"- window: `{payload['window']['start']} ~ "
            f"{payload['window']['end']}` "
            f"({payload['window']['trading_days']} 거래일)"
        ),
        f"- loader: `{_json_inline(payload['loader'])}`",
        f"- blind OOS opened: `{payload['single_pass']['oos_opened']}`",
        "- 이 결과가 development gate를 통과하지 못하면 blind OOS를 열지 않는다.",
        "",
    ])
    return "\n".join(lines)


def result_json_bytes(result: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def write_evidence_file(path: str, content: bytes) -> None:
    """기존 증거는 byte-identical일 때만 재사용하고 다른 내용으로 덮지 않는다."""
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    if os.path.exists(target):
        with open(target, "rb") as handle:
            existing = handle.read()
        if existing != content:
            raise FileExistsError(f"기존 증거와 내용이 다르다: {target}")
        return
    partial = target + ".partial"
    if os.path.exists(partial):
        raise FileExistsError(f"stale partial 존재: {partial}")
    try:
        with open(partial, "xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.exists(target):
            raise FileExistsError(f"증거 경로가 실행 중 생성됐다: {target}")
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
