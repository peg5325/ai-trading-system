"""Deterministic human-readable DEMA/Stochastic development report."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def render_report(result: Mapping[str, Any]) -> str:
    gate = result["gate"]
    lines = [
        "# P0-US30 DEMA(60) + Stochastic %D(60) development 결과",
        "",
        "> scope: **development-only** · blind OOS 미개봉",
        f"> payload SHA-256: `{result['payload_sha256']}`",
        "",
        "## 결론",
        "",
        f"- verdict: **{gate['verdict']}**",
        f"- failed gates: `{gate['failed']}`",
        (
            "- candidates/trades: "
            f"**{result['candidate_count_before_portfolio']}/{result['trade_count']}**"
        ),
        f"- direction: `{result['direction_counts']}`",
        "",
        "## Primary combined 비용별 성과",
        "",
        "| 비용 | 거래 | 거래일 | 누적 net | 기대값 | PF | 승률 | MDD | 양수 fold |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for level in ("low", "official", "stress"):
        stat = result["stats"]["combined"][level]
        lines.append(
            f"| {level} | {stat['trade_count']} | {stat['trading_days_with_trade']} | "
            f"{stat['cumulative_net_pnl']:.6f} | {stat['net_expectancy']:.6f} | "
            f"{stat['profit_factor']} | {stat['win_rate']:.4%} | "
            f"{stat['mdd']:.4%} | {stat['positive_folds']}/10 |"
        )
    lines.extend([
        "",
        "## 방향별 official 성과",
        "",
        "| 방향 | 거래 | 누적 net | 기대값 | PF | 승률 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    for name in ("long", "short"):
        stat = result["stats"][name]["official"]
        lines.append(
            f"| {name.upper()} | {stat['trade_count']} | "
            f"{stat['cumulative_net_pnl']:.6f} | {stat['net_expectancy']:.6f} | "
            f"{stat['profit_factor']} | {stat['win_rate']:.4%} |"
        )
    defects = result["defects"]
    lines.extend([
        "",
        "## 데이터 품질",
        "",
        (
            f"- incomplete symbol-days: `{defects['incomplete_symbol_days']} / "
            f"{defects['total_symbol_days']} = "
            f"{defects['incomplete_symbol_day_ratio']:.6%}`"
        ),
        f"- new conflicts: `{defects['new_conflicts']}`",
        f"- major defects: `{defects['major_defects']}`",
        "",
        "## 해석 경계",
        "",
        "- 사진에 없던 청산은 결과 전에 60분 고정 보유로 잠갔다.",
        "- SHORT는 borrow/locate/borrow fee를 모델링하지 않은 연구용 수치다.",
        "- 2026-01-21 이후 blind OOS를 로드하지 않았다.",
        "- 외부 공급자·Paper·실거래·주문·위험·config 변경은 없다.",
        "",
    ])
    return "\n".join(lines)
