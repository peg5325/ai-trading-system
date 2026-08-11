"""결정론적 JSON 결과를 사람이 검토할 Markdown으로 렌더링한다."""

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
    add("# P0-US30 52주 고점·상대강도 돌파 Development 결과")
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
    add(f"- symbols: {result['symbol_count']} (레버리지·인버스 제외)")
    add(f"- blind OOS opened: `{result['blind_oos_opened']}`")
    add("")
    add("## 1. 비용별 결과")
    add("")
    add("| spec | cost | trades | total net | expectancy | PF | win rate | MDD | max loss streak |")
    add("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for spec_name, spec in result["specs"].items():
        all_levels = {"gross": spec["gross"], **spec["cost_levels"]}
        for level, metrics in all_levels.items():
            add(
                f"| {spec_name} | {level} | {metrics['trade_count']:,} | "
                f"{_number(metrics['total_net_pnl'])} | "
                f"{_number(metrics['net_expectancy'])} | "
                f"{_number(metrics['profit_factor'])} | "
                f"{_number(metrics['win_rate'])} | "
                f"{_number(metrics['max_drawdown_pct'])}% | "
                f"{metrics['max_consecutive_losses']:,} |"
            )
    add("")
    add("## 2. 데이터·실행 감사")
    add("")
    add("| spec | cross-section days | insufficient days | qualifying | prepared | data excluded | exclusion rate | capacity | duplicate | digest |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
    for spec_name, spec in result["specs"].items():
        audit = spec["candidate_audit"]
        skips = spec["portfolio_skips"]
        add(
            f"| {spec_name} | {audit['cross_section_days']:,} | "
            f"{audit['insufficient_cross_section_days']:,} | "
            f"{audit['qualifying_signals']:,} | {audit['prepared_candidates']:,} | "
            f"{audit['data_exclusions']:,} | {audit['data_exclusion_rate']:.4%} | "
            f"{skips['capacity']:,} | {skips['duplicate_symbol']:,} | "
            f"`{spec['trades_sha256'][:16]}…` |"
        )
    add("")
    add("### 보유·청산 감사")
    add("")
    add("| spec | boundary | quantity skip | mean hold | median hold | gap stop | intraday stop | time exit |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|")
    for spec_name, spec in result["specs"].items():
        audit = spec["candidate_audit"]
        skips = spec["portfolio_skips"]
        reasons = spec["exit_reasons"]
        add(
            f"| {spec_name} | {audit['boundary_skips']:,} | "
            f"{skips['quantity_lt_one']:,} | {_number(spec['holding_days_mean'])} | "
            f"{_number(spec['holding_days_median'])} | {reasons.get('gap_stop', 0):,} | "
            f"{reasons.get('intraday_stop', 0):,} | {reasons.get('time_exit', 0):,} |"
        )
    add("")
    add("## 3. 게이트")
    add("")
    add("### 데이터 게이트")
    add("")
    for name, passed in result["data_gates"].items():
        add(f"- {name}: **{'PASS' if passed else 'FAIL'}**")
    add("")
    add("### 경제·강건성 게이트")
    add("")
    for name, passed in result["economic_gates"].items():
        add(f"- {name}: **{'PASS' if passed else 'FAIL'}**")
    add("")
    add("## 4. Primary 시간 블록")
    add("")
    add("| fold | start | end | trades | official net | positive |")
    add("|---:|---|---|---:|---:|:---:|")
    for fold in result["specs"]["primary_5d"]["folds_official"]:
        add(
            f"| {fold['fold']} | {fold['start']} | {fold['end']} | "
            f"{fold['trades']:,} | {_number(fold['net_pnl'])} | "
            f"{'Y' if fold['positive'] else 'N'} |"
        )
    add("")
    add("## 5. 공식 비용 기여도")
    add("")
    official = result["specs"]["primary_5d"]["cost_levels"]["official"]
    add("### 종목별")
    add("")
    add("| symbol | net PnL |")
    add("|---|---:|")
    for symbol, pnl in official["net_pnl_by_symbol"].items():
        add(f"| {symbol} | {_number(pnl)} |")
    add("")
    add("### 진입일별 상위 절대 기여 20일")
    add("")
    add("| entry day | net PnL |")
    add("|---|---:|")
    entry_days = sorted(
        official["net_pnl_by_entry_day"].items(),
        key=lambda item: (-abs(item[1]), item[0]),
    )[:20]
    for entry_day, pnl in entry_days:
        add(f"| {entry_day} | {_number(pnl)} |")
    add("")
    add("전체 일별 equity는 동형 JSON의 `daily_equity`에 보존한다.")
    add("")
    add("## 6. 판정 범위")
    add("")
    add(
        "이 결과는 고정 P0 26종목의 development-only 판정이다. `DEVELOPMENT_GO`도 blind OOS, "
        "Paper 또는 실거래 승인을 뜻하지 않는다. `NO_SELECTION`은 사전 고정 게이트 실패이며 "
        "규칙을 결과에 맞춰 수정하지 않는다."
    )
    add("")
    return "\n".join(lines)
