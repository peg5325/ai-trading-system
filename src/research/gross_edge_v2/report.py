"""Deterministic Markdown/JSON rendering and no-overwrite writing for screen v2 results."""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from src.research.gross_edge_v2.run import canonical_json_bytes

GATES = ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8")
LEG_GATES = ("G3", "G4", "G5", "G7", "G8")
ARM_HEADER = (
    "| arm | status | trades | days | mean %p | G1 | G2 | G3 | G4 | G5 | G6 | G7 | G8 "
    "| raw p | Holm p |"
)
LEG_HEADER = (
    "| arm | leg | trades | days | mean %p | G3 | G4 | G5 | G7 | G8 |"
)
DIAG_HEADER = (
    "| arm | baseline mean %p | mean − baseline | best day | best-day contribution "
    "| after 0.20 | after 0.30 | after 0.34 | after 0.51 |"
)


def fmt(value: Any, spec: str = ".6f") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "Y" if value else "N"
    if isinstance(value, float):
        return format(value, spec)
    return str(value)


def arm_row(arm: Mapping[str, Any]) -> str:
    stats = arm["all"]
    cells = [
        arm["arm_id"], arm["status"], fmt(stats["trade_count"]), fmt(stats["trading_days"]),
        fmt(stats["mean_gross_pct_points"]),
        *(fmt(stats["gates"][gate]) for gate in GATES),
        fmt(arm["bootstrap_p"], ".6g"), fmt(arm["holm_p"], ".6g"),
    ]
    return "| " + " | ".join(cells) + " |"


def leg_row(arm_id: str, leg: str, stats: Mapping[str, Any]) -> str:
    cells = [
        arm_id, leg, fmt(stats["trade_count"]), fmt(stats["trading_days"]),
        fmt(stats["mean_gross_pct_points"]),
        *(fmt(stats["gates"][gate]) for gate in LEG_GATES),
    ]
    return "| " + " | ".join(cells) + " |"


def diag_row(arm: Mapping[str, Any]) -> str:
    diag = arm["diagnostics"]
    best = diag["best_trading_day"]
    net = arm["net_after_cost_pct_points"]
    cells = [
        arm["arm_id"], fmt(diag["unconditional_baseline_mean"]),
        fmt(diag["trade_mean_minus_baseline"]),
        "n/a" if best is None else best["day"],
        "n/a" if best is None else fmt(best["contribution_to_trade_mean"]),
        fmt(net["commission_only"]), fmt(net["realistic"]),
        fmt(net["charter_official"]), fmt(net["stress"]),
    ]
    return "| " + " | ".join(cells) + " |"


def render_markdown(result: Mapping[str, Any]) -> str:
    windows = result["windows"]
    lines = [
        "# P0-US30 Intraday Gross-Edge Screen v2 — 결과",
        "",
        f"- verdict: **{result['verdict']}**",
        f"- inconclusive reason: {result['inconclusive_reason'] or '없음'}",
        (
            f"- 거래 평가: {windows['trade_eval_start']}~{windows['trade_eval_end']} "
            f"({windows['trade_eval_trading_days']} 거래일, 제외 블록 "
            f"{windows['excluded_block_trading_days']}일, 평가 {windows['evaluated_trading_days']}일)"
        ),
        f"- data end: {windows['data_end']}",
        f"- blind OOS accessed: `{str(result['blind_oos_accessed']).lower()}`",
        f"- holdout H2 accessed: `{str(result['holdout_h2_accessed']).lower()}`",
        f"- prereg JSON SHA-256: `{result['prereg']['json_file_sha256']}`",
        f"- prereg MD SHA-256: `{result['prereg']['md_file_sha256']}`",
        f"- payload SHA-256: `{result['payload_sha256']}`",
        "",
        "## 판정",
        "",
        f"- SHORTLIST (LONG 후보): {', '.join(result['shortlisted_arms']) or '없음'}",
        f"- INVERSE_ONLY: {', '.join(result['inverse_only_arms']) or '없음'}",
        f"- SAMPLE (G1·G2 미달): {', '.join(result['sample_arms']) or '없음'}",
        "- 이 결과는 blind OOS 개봉·H2 접근·Paper 실행을 자동 승인하지 않는다.",
        "",
        "## 무결성",
        "",
    ]
    identity = result["data_identity"]
    if identity:
        for name in sorted(identity):
            lines.append(f"- {name}: `{identity[name]['status']}`")
    else:
        lines.append("- 검증 미완료")
    quality = result["data_quality"]
    if quality:
        lines.extend([
            "",
            "## 데이터 품질",
            "",
            (
                f"- 무효 symbol-day: {quality['invalid_symbol_days']} / "
                f"{quality['evaluated_symbol_days']} ({fmt(quality['invalid_share'])}, "
                f"한도 {quality['max_invalid_share']})"
            ),
            f"- max loaded day: {quality['max_loaded_day']}",
        ])
    if result["determinism"] is not None:
        lines.append(
            "- evaluation re-run (reversed input) identical: "
            f"`{str(result['determinism']['evaluation_rerun_reversed_input_identical']).lower()}`"
        )
    if result["arms"]:
        lines.extend(["", "## arm별 결과", "", ARM_HEADER, "|" + "---|" * 15])
        lines.extend(arm_row(arm) for arm in result["arms"])
        lines.extend(["", "## 인버스·비인버스 다리", "", LEG_HEADER, "|" + "---|" * 10])
        for arm in result["arms"]:
            lines.append(leg_row(arm["arm_id"], "non_inverse", arm["non_inverse_leg"]))
            if arm["inverse_leg"] is not None:
                lines.append(leg_row(arm["arm_id"], "inverse", arm["inverse_leg"]))
        lines.extend(["", "## 진단 (판정 비반영)", "", DIAG_HEADER, "|" + "---|" * 9])
        lines.extend(diag_row(arm) for arm in result["arms"])
        lines.extend(["", "## 결측·스킵 집계", ""])
        for arm_id, counts in sorted(quality["skip_counts_by_arm"].items()):
            text = ", ".join(f"{reason}={count}" for reason, count in counts.items()) or "없음"
            lines.append(f"- {arm_id}: {text}")
    return "\n".join(lines) + "\n"


def write_new(path: str, payload: bytes) -> None:
    """Write atomically and refuse to overwrite an existing file."""
    if os.path.exists(path):
        raise FileExistsError(f"refusing to overwrite existing result: {path}")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    partial = path + ".partial"
    with open(partial, "xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(partial, path)  # fails if ``path`` appeared meanwhile
    finally:
        os.unlink(partial)


def write_outputs(result: Mapping[str, Any], *, json_path: str, md_path: str) -> None:
    for path in (json_path, md_path):
        if os.path.exists(path):
            raise FileExistsError(f"refusing to overwrite existing result: {path}")
    write_new(json_path, canonical_json_bytes(result))
    write_new(md_path, render_markdown(result).encode("utf-8"))
