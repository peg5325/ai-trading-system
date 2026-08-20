"""Deterministic Markdown renderer for box/order-block results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _number(value: Any, digits: int = 6) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return f"{value:,}"
    return f"{float(value):.{digits}f}"


def render_markdown(result: Mapping[str, Any]) -> str:
    lines = [
        "# P0-US30 박스 돌파 + 오더블록 리테스트 Development 결과",
        "",
        f"- verdict: **{result['verdict']}**",
        f"- scope: `{result['scope']}`",
        f"- payload SHA-256: `{result['payload_sha256']}`",
        (
            f"- development: {result['window']['start']}~{result['window']['end']} "
            f"({result['window']['trading_days']} 거래일)"
        ),
        f"- raw breakout signals: {result['signal_count']:,}",
        f"- blind OOS opened: `{str(result['blind_oos_opened']).lower()}`",
        f"- promotion allowed: `{str(result['promotion_allowed']).lower()}`",
        "",
        "## Arm별 official 결과",
        "",
        "| arm | candidates | trades | net | expectancy | PF | exp/R | MDD | folds |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm_name in ("base", "primary", "partial"):
        arm = result["arms"][arm_name]
        official = arm["cost_levels"]["official"]
        audit = arm["candidate_audit"]
        lines.append(
            f"| {arm_name} | {audit['prepared_candidates']:,} | "
            f"{official['trade_count']:,} | {_number(official['cumulative_net_pnl'])} | "
            f"{_number(official['net_expectancy'])} | "
            f"{_number(official['profit_factor'])} | "
            f"{_number(official['expectancy_per_initial_r'])} | "
            f"{_number(official['max_drawdown_pct'])}% | "
            f"{arm['positive_folds_official']}/10 |"
        )
    lines.extend(["", "## Primary 비용별 결과", ""])
    lines.extend(
        [
            "| cost | trades | net | expectancy | PF | win rate | exp/R | MDD |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    primary = result["arms"]["primary"]
    for level in ("gross", "low", "official", "stress"):
        metrics = (
            primary["gross"]
            if level == "gross"
            else primary["cost_levels"][level]
        )
        lines.append(
            f"| {level} | {metrics['trade_count']:,} | "
            f"{_number(metrics['cumulative_net_pnl'])} | "
            f"{_number(metrics['net_expectancy'])} | "
            f"{_number(metrics['profit_factor'])} | "
            f"{metrics['win_rate']:.4%} | "
            f"{_number(metrics['expectancy_per_initial_r'])} | "
            f"{_number(metrics['max_drawdown_pct'])}% |"
        )
    lines.extend(["", "## 데이터·후보 감사", ""])
    lines.append(f"- signal audit: `{result['signal_audit']}`")
    for arm_name in ("base", "primary"):
        audit = result["arms"][arm_name]["candidate_audit"]
        lines.append(
            f"- {arm_name}: prepared={audit['prepared_candidates']}, "
            f"excluded={audit['data_exclusions']}, "
            f"rate={audit['data_exclusion_rate']:.6%}, "
            f"boundary={audit['boundary_skips']}, filters={audit['strategy_filters']}"
        )
    lines.append(f"- canonical defects: `{result['defects']}`")
    lines.extend(["", "## Turnover·비용·보유 감사", ""])
    for arm_name in ("base", "primary", "partial"):
        official = result["arms"][arm_name]["cost_levels"]["official"]
        lines.append(
            f"- {arm_name}: entry_turnover={official['entry_turnover_notional']:.6f}, "
            f"exit_turnover={official['exit_turnover_notional']:.6f}, "
            f"cost={official['total_cost']:.6f}, "
            f"hold_mean={official['holding_days_mean']:.6f}, "
            f"hold_median={official['holding_days_median']:.6f}, "
            f"max_loss_streak={official['max_consecutive_losses']}, "
            f"exits={result['arms'][arm_name]['exit_reasons']}"
        )
    lines.extend(["", "## 게이트", "", "### 데이터", ""])
    for name, passed in result["data_gates"].items():
        lines.append(f"- {name}: **{'PASS' if passed else 'FAIL'}**")
    lines.extend(["", "### 경제·강건성", ""])
    for name, passed in result["economic_gates"].items():
        lines.append(f"- {name}: **{'PASS' if passed else 'FAIL'}**")
    lines.extend(
        [
            "",
            "## 해석 경계",
            "",
            "- Primary만 판정 권한이 있으며 base/partial로 사후 교체하지 않는다.",
            "- 같은 development 데이터의 결과는 exploratory-only다.",
            "- all-pass도 untouched forward 데이터의 새 사전등록이 필요하다.",
            "- 이 결과는 blind OOS, Paper 또는 live 승격을 승인하지 않는다.",
            "",
        ]
    )
    return "\n".join(lines)
