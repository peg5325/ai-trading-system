"""SPY 장중 모멘텀 결과 Markdown renderer."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _number(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.6f}"
    return str(value)


def render(result: Mapping[str, Any]) -> str:
    official = result["stats"]["official"]
    directions = result["official_direction_attribution"]
    lines = [
        "# 결과 — SPY 첫 30분 → 마지막 30분 장중 모멘텀",
        "",
        f"> 판정: **{result['gate']['verdict']}** · development-only · blind OOS 미개봉",
        "",
        "## 핵심 결과",
        "",
        f"- 평가 거래일: {result['window']['trading_days']}일",
        f"- 거래: {result['trade_count']}건 / 거래 발생일 {official['trading_days_with_trade']}일",
        (
            f"- 신호: LONG {result['signal_sign_counts'].get('LONG', 0)} / "
            f"SHORT {result['signal_sign_counts'].get('SHORT', 0)} / "
            f"ZERO {result['signal_sign_counts'].get('ZERO', 0)}"
        ),
        f"- official 순손익: {_number(official['cumulative_net_pnl'])}",
        f"- official 거래당 기대값: {_number(official['net_expectancy'])}",
        f"- official PF: {_number(official['profit_factor'])}",
        f"- official MDD: {_number(official['mdd'])}",
        f"- 양수 fold: {official['positive_folds']}/10",
        f"- 실패 게이트: {', '.join(result['gate']['failed']) or '없음'}",
        "",
        "## 비용별 결과",
        "",
        "| 비용 | 누적 순손익 | 기대값 | PF | MDD | 양수 fold |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for level in ("low", "official", "stress"):
        stats = result["stats"][level]
        lines.append(
            f"| {level} | {_number(stats['cumulative_net_pnl'])} | "
            f"{_number(stats['net_expectancy'])} | {_number(stats['profit_factor'])} | "
            f"{_number(stats['mdd'])} | {stats['positive_folds']}/10 |"
        )
    lines.extend([
        "",
        "## LONG / SHORT 기여 (official)",
        "",
        "| 방향 | 거래 | 누적 순손익 | 기대값 |",
        "|---|---:|---:|---:|",
    ])
    for direction in ("LONG", "SHORT"):
        item = directions[direction]
        lines.append(
            f"| {direction} | {item['trade_count']} | "
            f"{_number(item['cumulative_net_pnl'])} | {_number(item['net_expectancy'])} |"
        )
    defects = result["defects"]
    lines.extend([
        "",
        "## 데이터 품질",
        "",
        (
            f"- 불완전 SPY 거래일: {defects['incomplete_spy_days']} / "
            f"{defects['total_spy_days']} ({defects['incomplete_spy_day_ratio']:.4%})"
        ),
        (
            f"- 직전 거래일 일봉 종가 누락: {defects['missing_previous_close_days']} "
            f"({defects['missing_previous_close_ratio']:.4%})"
        ),
        f"- 신규 충돌: {defects['new_conflicts']}",
        f"- 중대 결함: {defects['major_defects']}",
        "",
        "## 해석 경계",
        "",
        "- 결과 전에 커밋한 primary 한 개만 실행했다. 추가 필터·민감도 탐색은 하지 않았다.",
        "- 논문의 역사적 성과나 비용을 재현한다는 뜻이 아니다. 프로젝트의 보수적 비용을 적용했다.",
        "- SPY 단일 종목, 2023-05-18~2026-01-20 development 구간에만 해당한다.",
        "- 2026-01-21~2026-07-21 blind OOS는 로드하지 않았다.",
        "- Paper/실거래·주문·위험·config 변경은 없다.",
        f"- 결과 payload SHA-256: `{result['payload_sha256']}`",
        "- payload SHA-256은 해당 해시 필드를 넣기 전 canonical payload에 대해 계산했다.",
        "",
        "## 실행 감사",
        "",
        (
            f"- {result['execution_audit']['review_phase']}: "
            f"{result['execution_audit']['issues']} → "
            f"{result['execution_audit']['fixes']}; "
            "성과 사전열람="
            f"{result['execution_audit']['performance_values_inspected_before_fix']}, "
            "전략 규칙 변경="
            f"{result['execution_audit']['strategy_rules_changed']}"
        ),
        "",
    ])
    return "\n".join(lines)
