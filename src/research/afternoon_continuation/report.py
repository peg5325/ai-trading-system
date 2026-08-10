"""오후 추세 지속 결과 Markdown 렌더러."""
from __future__ import annotations

from typing import Any, Mapping


def render(result: Mapping[str, Any]) -> str:
    gate = result["gate"]
    defects = result["defects"]
    lines = [
        "# P0-US30 오전 강세·점심 눌림·오후 재상승 development 결과",
        "",
        "> scope: **development-only** · blind OOS 미개봉",
        f"> payload SHA-256: `{result['payload_sha256']}`",
        "",
        "## 결론",
        "",
        f"- verdict: **{gate['verdict']}**",
        f"- failed gates: `{gate['failed']}`",
        f"- candidates/trades: **{result['candidate_count_before_portfolio']}/"
        f"{result['trade_count']}**",
        "",
        "## 비용별 성과",
        "",
        "| 비용 | 거래 | 거래일 | 누적 net | 기대값 | PF | MDD | 양수 fold |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for level in ("low", "official", "stress"):
        stats = result["stats"][level]
        pf = stats["profit_factor"]
        pf_text = pf if isinstance(pf, str) else f"{pf:.6f}"
        lines.append(
            f"| {level} | {stats['trade_count']} | {stats['trading_days_with_trade']} | "
            f"{stats['cumulative_net_pnl']:.6f} | {stats['net_expectancy']:.6f} | "
            f"{pf_text} | {stats['mdd']:.6f} | {stats['positive_folds']} |"
        )
    lines += ["", "## 청산·포트폴리오", ""]
    lines.append(f"- exit reasons: `{result['exit_reasons']}`")
    lines.append(f"- portfolio skips: `{result['portfolio_skips']}`")
    lines += ["", "## 게이트", "", "| 게이트 | 통과 |", "| --- | --- |"]
    lines.extend(f"| {name} | {passed} |" for name, passed in gate["items"].items())
    lines += [
        "", "## 데이터 품질", "",
        f"- incomplete symbol-days: `{defects['incomplete_symbol_days']}/"
        f"{defects['total_symbol_days']} = {defects['incomplete_symbol_day_ratio']:.6%}`",
        f"- new conflicts: `{defects['new_conflicts']}`",
        f"- major defects: `{defects['major_defects']}`",
        "", "## 실행 규율", "",
        "- 사전등록 primary 한 개만 실행했다.",
        "- 2026-01-21 이후 blind OOS를 로드하지 않았다.",
        "- 결과 후 임계값·목표·손절을 수정하지 않았다.",
        "- Paper·실거래 승격 결과가 아니다.",
        "",
    ]
    return "\n".join(lines)
