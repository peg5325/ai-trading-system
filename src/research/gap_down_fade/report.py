"""Gap-down fade result Markdown renderer."""
from __future__ import annotations

from typing import Any, Mapping


def _number(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.6f}"
    return str(value)


def render(result: Mapping[str, Any]) -> str:
    official = result["stats"]["official"]
    lines = [
        "# 결과 — P0-US30 큰 갭하락 후 VWAP 회복 development",
        "",
        f"> 판정: **{result['gate']['verdict']}** · blind OOS 미개봉",
        "",
        "## 핵심 결과",
        "",
        f"- 거래: {result['trade_count']}건 / 거래 발생일 {official['trading_days_with_trade']}일",
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
        "## 데이터 품질",
        "",
        f"- 불완전 종목-일: {result['defects']['incomplete_symbol_days']} / "
        f"{result['defects']['total_symbol_days']} "
        f"({result['defects']['incomplete_symbol_day_ratio']:.4%})",
        f"- 직전 거래일 종가 누락: {result['defects']['missing_previous_close_symbol_days']} "
        f"({result['defects']['missing_previous_close_ratio']:.4%})",
        f"- 신규 충돌: {result['defects']['new_conflicts']}",
        f"- 중대 결함: {result['defects']['major_defects']}",
        "",
        "## 규율",
        "",
        "- 결과 전에 커밋한 primary 하나만 실행했다.",
        "- 2026-01-21~2026-07-21 blind OOS는 로드하지 않았다.",
        "- 외부 공급자, Paper/실거래, 주문·위험·config 변경은 없었다.",
        f"- 결과 payload SHA-256: `{result['payload_sha256']}`",
        "",
    ])
    return "\n".join(lines)
