"""Deterministic Markdown and JSON rendering for Gap+RVOL development."""
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
        raise TypeError("result payload must be an object")
    verdict = payload["verdict"]
    dev_gate = payload["dev_gate"]
    preconditions = payload["preconditions"]
    snapshot = payload["snapshot"]
    defect_ratios = payload["defect_ratios"]
    specs = payload["specs"]
    if not isinstance(specs, Sequence):
        raise TypeError("result specs must be an array")

    lines = [
        "# P0-US30 Gap+RVOL development walk-forward result",
        "",
        f"> generated: `{result['generated_at_utc']}`",
        f"> payload SHA-256: `{result['payload_sha256']}`",
        "> scope: development-only. blind OOS was not opened.",
        "",
        "## 1. Machine Verdict",
        "",
        f"- verdict: **{verdict['value']}**",
        f"- development gate: **{dev_gate['development_gate']}**",
        f"- failed: `{_json_inline(verdict['failed'])}`",
        f"- next stage: `{verdict['next_stage']}`",
        "",
        "| gate | criterion | actual | pass |",
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
        "## 2. Preconditions P1-P5",
        "",
        f"- P1 prereg merge: `{preconditions['P1']['prereg_merge_commit']}`",
        (
            "- P2 implementation merge: "
            f"`{preconditions['P2']['implementation_merge_commit']}`"
        ),
        (
            "- P2 implementation map: "
            f"`{preconditions['P2']['implementation_map_status']}`"
        ),
        f"- P2 implementation review: {preconditions['P2']['implementation_review_evidence_url']}",
        (
            "- P3 runner reviewed commit: "
            f"`{preconditions['P3']['runner_reviewed_commit']}`"
        ),
        (
            "- P3 runner code-reviewer/architect: "
            f"`{preconditions['P3']['runner_code_review_status']}` / "
            f"`{preconditions['P3']['runner_architect_status']}`"
        ),
        f"- P3 runner review evidence: {preconditions['P3']['runner_review_evidence_url']}",
        f"- P4 snapshot identity: `{preconditions['P4']['snapshot_identity']}`",
        f"- P4 tracked manifest: `{preconditions['P4']['tracked_manifest']}`",
        (
            "- P4 tracked manifest SHA-256: "
            f"`{preconditions['P4']['tracked_manifest_file_sha256']}`"
        ),
        (
            "- P5 loader boundary: "
            f"`{preconditions['P5']['loader_boundary_status']}` - "
            f"`{preconditions['P5']['loader_boundary_test']}`"
        ),
        "",
        "## 3. Input Identity",
        "",
        f"- snapshot: `{snapshot['basename']}`",
        f"- file: `{snapshot['snapshot_file_sha256']}`",
        f"- content: `{snapshot['snapshot_content_sha256']}`",
        f"- manifest: `{snapshot['manifest_data_sha256']}`",
        f"- raw: `{snapshot['raw_content_sha256']}`",
        f"- producer commit: `{payload['producer_commit']}`",
        f"- prereg merge: `{payload['prereg']['prereg_merge_commit']}`",
        "",
        "## 4. 9 Specs - 3 Costs",
        "",
        (
            "| spec | authority | gap | RVOL | retention | target | "
            "trades | days | net PnL | expectancy | PF | MDD |"
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
            f"| {_number(params['gap_threshold'])} "
            f"| {_number(params['rvol_threshold'])} "
            f"| {_number(params['retention_fraction'])} "
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
        "## 5. Primary Cost Sensitivity",
        "",
        "| cost | trades | net PnL | expectancy | PF | MDD | positive folds |",
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
        "## 6. Defects And Audit",
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
        f"- identity failures: `{_json_inline(defect_ratios['identity_failures'])}`",
        f"- excluded summary: `{_json_inline(payload['excluded'])}`",
        f"- filtered summary: `{_json_inline(payload['filtered'])}`",
        f"- skipped summary: `{_json_inline(payload['skipped'])}`",
        "",
        "## 7. Scope Limit",
        "",
        (
            f"- data window: `{payload['window']['data_start']} ~ "
            f"{payload['window']['data_end']}` "
            f"({payload['window']['data_trading_days']} trading days)"
        ),
        (
            f"- eval window: `{payload['window']['eval_start']} ~ "
            f"{payload['window']['eval_end']}` "
            f"({payload['window']['eval_trading_days']} trading days)"
        ),
        f"- loader: `{_json_inline(payload['loader'])}`",
        f"- blind OOS opened: `{payload['single_pass']['oos_opened']}`",
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
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    if os.path.exists(target):
        with open(target, "rb") as handle:
            existing = handle.read()
        if existing != content:
            raise FileExistsError(f"existing evidence differs: {target}")
        return
    partial = target + ".partial"
    if os.path.exists(partial):
        raise FileExistsError(f"stale partial exists: {partial}")
    try:
        with open(partial, "xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.exists(target):
            raise FileExistsError(f"evidence path appeared during write: {target}")
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
