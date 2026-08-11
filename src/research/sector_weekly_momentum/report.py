"""결정론적 개발 결과의 Markdown 렌더러."""

from __future__ import annotations

from typing import Any


def _number(value: Any, digits: int = 6) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, bool):
        return "PASS" if value else "FAIL"
    if isinstance(value, int):
        return f"{value:,}"
    return f"{float(value):.{digits}f}"


def render_markdown(result: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add("# P0 섹터 ETF 주간 상대강도 로테이션 Development 결과")
    add("")
    add(f"- verdict: **{result['verdict']}**")
    add(f"- strategy: `{result['strategy_id']}`")
    add(f"- contract SHA-256: `{result['contract_sha256']}`")
    add(f"- daily data SHA-256: `{result['data_content_sha256']}`")
    window = result["development_window"]
    add(
        f"- development: {window['start']}~{window['end_inclusive']} "
        f"({window['calendar_days']:,} SPY 거래일)"
    )
    add(f"- symbols: {','.join(result['symbols'])}")
    add(f"- blind OOS opened: `{result['blind_oos_opened']}`")
    add("")
    add("## 1. 비용별 성과")
    add("")
    add("| portfolio | cost | episodes | net PnL | expectancy | PF | win rate | MDD | turnover | cost |")
    add("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for portfolio_name in ("primary", "sensitivity", "equal_all"):
        portfolio = result[portfolio_name]
        for level, metrics in portfolio["metrics"].items():
            add(
                f"| {portfolio_name} | {level} | {metrics['episode_count']:,} | "
                f"{_number(metrics['total_net_pnl'])} | "
                f"{_number(metrics['net_expectancy'])} | "
                f"{_number(metrics['profit_factor'])} | "
                f"{metrics['win_rate']:.4%} | "
                f"{_number(metrics['max_drawdown_pct'])}% | "
                f"{_number(metrics['turnover_notional'])} | "
                f"{_number(metrics['total_cost'])} |"
            )
    add("")
    add("## 2. 주간 신호·turnover 감사")
    add("")
    add("| spec | scheduled | warmup | valid | invalid | invalid rate | boundary | regime on | cash | digest |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for name in ("primary", "sensitivity"):
        audit = result["signal_week_audit"][name]
        add(
            f"| {name} | {audit['scheduled_weeks']:,} | {audit['warmup_weeks']:,} | "
            f"{audit['valid_weeks']:,} | {audit['invalid_weeks']:,} | "
            f"{audit['invalid_week_rate']:.4%} | {audit['boundary_weeks']:,} | "
            f"{audit['regime_on_weeks']:,} | {audit['cash_weeks']:,} | "
            f"`{result[name]['episode_digest_sha256'][:16]}…` |"
        )
    add("")
    add("### 공식 비용 실행 수치")
    add("")
    add("| portfolio | buys | sells | retained | mean hold | median hold | max loss streak | max symbol contribution | cost drag / gross |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name in ("primary", "sensitivity", "equal_all"):
        portfolio = result[name]
        metrics = portfolio["metrics"]["official"]
        add(
            f"| {name} | {metrics['buy_count']:,} | {metrics['sell_count']:,} | "
            f"{metrics['retained_count']:,} | {_number(metrics['holding_days_mean'])} | "
            f"{_number(metrics['holding_days_median'])} | "
            f"{metrics['max_consecutive_losses']:,} | "
            f"{_number(metrics['max_symbol_contribution'])} | "
            f"{_number(portfolio['official_cost_drag_vs_gross_profit'])} |"
        )
    add("")
    add("## 3. 데이터·경제 게이트")
    add("")
    add("### 데이터")
    add("")
    for name, passed in result["data_gates"].items():
        add(f"- {name}: **{'PASS' if passed else 'FAIL'}**")
    add("")
    add("### 경제·강건성")
    add("")
    for name, passed in result["economic_gates"].items():
        add(f"- {name}: **{'PASS' if passed else 'FAIL'}**")
    add("")
    add("## 4. 독립 시간 블록 — primary vs equal-all")
    add("")
    add("| fold | start | end | primary net | equal-all net | excess | positive |")
    add("|---:|---|---|---:|---:|---:|:---:|")
    for fold in result["folds_official"]:
        add(
            f"| {fold['fold']} | {fold['start']} | {fold['end']} | "
            f"{_number(fold['primary_net'])} | {_number(fold['equal_all_net'])} | "
            f"{_number(fold['excess'])} | {'Y' if fold['positive_excess'] else 'N'} |"
        )
    add("")
    add("## 5. 기준선과 공식 비용 기여도")
    add("")
    add(
        "- SPY buy-and-hold 참고 gross PnL(50,000 USD): "
        f"{_number(result['spy_buy_hold_reference_gross_pnl_50k'])}"
    )
    primary_official = result["primary"]["metrics"]["official"]
    equal_official = result["equal_all"]["metrics"]["official"]
    add(f"- primary official net: {_number(primary_official['total_net_pnl'])}")
    add(f"- equal-all official net: {_number(equal_official['total_net_pnl'])}")
    add("")
    add("### Primary 종목별 official net PnL")
    add("")
    add("| symbol | net PnL |")
    add("|---|---:|")
    for symbol, pnl in primary_official["net_pnl_by_symbol"].items():
        add(f"| {symbol} | {_number(pnl)} |")
    add("")
    add("## 6. 판정 범위")
    add("")
    add(
        "이 결과는 고정 8개 ETF의 development-only 판정이다. `DEVELOPMENT_GO`도 blind OOS, "
        "Paper 또는 실거래 승인을 뜻하지 않는다. `NO_SELECTION`은 사전 고정 게이트 실패이며 "
        "결과에 맞춰 신호·보유·비용을 수정하지 않는다."
    )
    add("")
    add("## 7. 실행 감사")
    add("")
    for item in result.get("execution_audit", []):
        add(
            f"- {item['phase']}: `{item['issue']}` → `{item['resolution']}`; "
            f"성과 사전열람={item['performance_values_inspected_before_fix']}, "
            f"전략 규칙 변경={item['strategy_rules_changed']}"
        )
    add("")
    return "\n".join(lines)
