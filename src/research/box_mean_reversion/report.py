"""박스권 평균회귀 development 결과 Markdown 렌더러."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def render(result: Mapping[str, Any]) -> str:
    gate = result["gate"]
    defects = result["defects"]
    lines = [
        "# P0-US30 박스권 RSI·거래량 평균회귀 development 결과",
        "",
        "> scope: **development-only** · blind OOS 미개봉",
        f"> payload SHA-256: `{result['payload_sha256']}`",
        "",
        "## 결론",
        "",
        f"- verdict: **{gate['verdict']}**",
        f"- failed gates: `{gate['failed']}`",
        f"- trades: **{result['trade_count']}**",
        "",
        "## 비용별 성과",
        "",
        "| 비용 | 거래 | 거래일 | 누적 net | 기대값 | PF | MDD | 양수 fold |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for level in ("low", "official", "stress"):
        s = result["stats"][level]
        pf = s["profit_factor"]
        pf_text = pf if isinstance(pf, str) else f"{pf:.6f}"
        lines.append(
            f"| {level} | {s['trade_count']} | {s['trading_days_with_trade']} | "
            f"{s['cumulative_net_pnl']:.6f} | {s['net_expectancy']:.6f} | "
            f"{pf_text} | {s['mdd']:.6f} | {s['positive_folds']} |"
        )
    lines += [
        "",
        "## 게이트",
        "",
        "| 게이트 | 통과 |",
        "| --- | --- |",
    ]
    lines.extend(f"| {name} | {passed} |" for name, passed in gate["items"].items())
    lines += [
        "",
        "## 데이터 품질",
        "",
        (
            f"- incomplete symbol-days: `{defects['incomplete_symbol_days']}/"
            f"{defects['total_symbol_days']} = {defects['incomplete_symbol_day_ratio']:.6%}`"
        ),
        f"- new conflicts: `{defects['new_conflicts']}`",
        f"- major defects: `{defects['major_defects']}`",
        "",
        "## 실행 규율",
        "",
        "- 사전등록 primary 한 개만 실행했다.",
        "- 2026-01-21 이후 blind OOS는 로드하지 않았다.",
        "- 이 결과는 Paper·실거래 승격이 아니다.",
        "- 결과 후 임계값이나 규칙을 수정하지 않았다.",
        "",
        "## 실행 감사",
        "",
    ]
    lines.extend(
        f"- {item['phase']}: `{item['issue']}` → `{item['resolution']}`; "
        f"성과 사전열람={item['performance_values_inspected_before_fix']}, "
        f"전략 규칙 변경={item['strategy_rules_changed']}"
        for item in result.get("execution_audit", [])
    )
    lines.append("")
    return "\n".join(lines)
