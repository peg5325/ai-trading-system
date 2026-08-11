"""Deterministic Markdown/JSON rendering for gross-edge screen results."""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from src.research.gross_edge.run import canonical_json_bytes


def render_markdown(result: Mapping[str, Any]) -> str:
    lines = [
        "# P0-US30 Intraday Gross-Edge Screen — 결과",
        "",
        f"- verdict: **{result['verdict']}**",
        (
            f"- development: {result['development_window']['start']}~"
            f"{result['development_window']['end']} "
            f"({result['development_window']['trading_days']} 거래일)"
        ),
        f"- universe/candidates: {result['universe_count']} / {result['candidate_count']}",
        f"- blind OOS accessed: `{str(result['blind_oos_accessed']).lower()}`",
        f"- payload SHA-256: `{result['payload_sha256']}`",
        "",
        "## 후보별 결과",
        "",
        "| candidate | trades | days | gross mean %p | positive folds | adjusted p | after 0.204 | after 0.340 | after 0.680 | gross pass | economic pass |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|:---:|:---:|",
    ]
    for candidate in result["candidates"]:
        costs = candidate["net_after_cost_pct_points"]
        lines.append(
            f"| {candidate['candidate_id']} | {candidate['trade_count']} | "
            f"{candidate['trading_days']} | {candidate['mean_daily_gross_pct_points']:.6f} | "
            f"{candidate['positive_folds']}/10 | {candidate['sign_test_bonferroni_p']:.6g} | "
            f"{costs['published_floor']:.6f} | {costs['conservative']:.6f} | "
            f"{costs['stress']:.6f} | {'YES' if candidate['gross_edge_pass'] else 'NO'} | "
            f"{'YES' if candidate['economic_pass'] else 'NO'} |"
        )
    lines.extend(
        [
            "",
            "## 판정",
            "",
            f"- conservative-cost shortlist: {', '.join(result['shortlisted_candidates']) or '없음'}",
            f"- gross-only candidates: {', '.join(result['gross_edge_only_candidates']) or '없음'}",
            "- 이 결과는 blind OOS 개봉·Paper 실행을 자동 승인하지 않는다.",
            "",
            "## 무결성",
            "",
        ]
    )
    for name in (
        "snapshot_file",
        "snapshot_receipt_file",
        "snapshot_content_receipt",
        "manifest_data_receipt",
        "raw_content_index",
    ):
        check = result["data_identity"][name]
        lines.append(f"- {name}: `{check['status']}`")
    audit = result.get("execution_audit")
    if audit:
        lines.extend(
            [
                "",
                "## 병합 전 실행 감사",
                "",
                f"- 발견 단계: `{audit['review_phase']}`",
                f"- 결함: `{audit['issue']}`",
                f"- 수정: `{audit['fix']}`",
                f"- 전략 규칙 변경: `{str(audit['strategy_rules_changed']).lower()}`",
                f"- 결과 재생성: `{str(audit['result_regenerated']).lower()}`",
            ]
        )
    return "\n".join(lines) + "\n"


def write_result(path: str, payload: bytes) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    partial = path + ".partial"
    with open(partial, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(partial, path)


def write_outputs(result: Mapping[str, Any], *, json_path: str, md_path: str) -> None:
    write_result(json_path, canonical_json_bytes(result))
    write_result(md_path, render_markdown(result).encode("utf-8"))
