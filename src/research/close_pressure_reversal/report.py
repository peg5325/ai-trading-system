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
    add("# P0-US30 장 마감 과잉압력 반전 Development 결과")
    add("")
    add(f"- verdict: **{result['verdict']}**")
    add(f"- strategy: `{result['strategy_id']}`")
    add(f"- contract SHA-256: `{result['contract_sha256']}`")
    add(f"- daily data SHA-256: `{result['data_content_sha256']}`")
    add(f"- payload SHA-256: `{result['payload_sha256']}`")
    window = result["development_window"]
    add(
        f"- development: {window['start']}~{window['end_inclusive']} "
        f"({window['calendar_days']:,} SPY 거래일)"
    )
    add(f"- symbols: {result['symbol_count']} (레버리지·인버스 제외)")
    add(f"- blind OOS OHLCV touched historically: `{result['blind_oos_opened']}`")
    add(f"- blind OOS performance accessed: `{result['blind_oos_performance_accessed']}`")
    add(
        "- corrected execution touched blind OOS OHLCV: "
        f"`{result['current_execution_blind_oos_ohlcv_accessed']}`"
    )
    add(f"- prereg compliance: `{result['prereg_compliance']}`")
    add(f"- erratum: `{result['erratum_path']}`")
    add("")
    add("## 1. 비용별 결과")
    add("")
    add("| spec | cost | trades | total net | expectancy | PF | win rate | MDD | max loss streak |")
    add("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for spec_name, spec in result["specs"].items():
        all_levels = {"gross": spec["gross"], **spec["cost_levels"]}
        for level, metrics in all_levels.items():
            add(
                f"| {spec_name} | {level} | {_number(metrics['trade_count'], 0)} | "
                f"{_number(metrics['total_net_pnl'])} | {_number(metrics['net_expectancy'])} | "
                f"{_number(metrics['profit_factor'])} | {_number(metrics['win_rate'])} | "
                f"{_number(metrics['max_drawdown_pct'])}% | "
                f"{metrics['max_consecutive_losses']:,} |"
            )
    add("")
    add("## 2. 데이터·실행 감사")
    add("")
    add("| spec | qualifying | prepared | data excluded | exclusion rate | capacity skip | duplicate skip | trade digest |")
    add("|---|---:|---:|---:|---:|---:|---:|---|")
    for spec_name, spec in result["specs"].items():
        audit = spec["candidate_audit"]
        skips = spec["portfolio_skips"]
        add(
            f"| {spec_name} | {audit['qualifying_signals']:,} | "
            f"{audit['prepared_candidates']:,} | {audit['data_exclusions']:,} | "
            f"{audit['data_exclusion_rate']:.4%} | {skips['capacity']:,} | "
            f"{skips['duplicate_symbol']:,} | `{spec['trades_sha256'][:16]}…` |"
        )
    add("")
    add("### 보유·청산 감사")
    add("")
    add("| spec | boundary skip | quantity skip | mean hold | median hold | gap stop | intraday stop | time exit |")
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
    add("## 4. 시간 블록")
    add("")
    add("| fold | start | end | trades | official net | positive |")
    add("|---:|---|---|---:|---:|:---:|")
    for fold in result["specs"]["primary_3d"]["folds_official"]:
        add(
            f"| {fold['fold']} | {fold['start']} | {fold['end']} | "
            f"{fold['trades']:,} | {_number(fold['net_pnl'])} | "
            f"{'Y' if fold['positive'] else 'N'} |"
        )
    add("")
    add("## 5. 공식 비용 기여도")
    add("")
    primary_official = result["specs"]["primary_3d"]["cost_levels"]["official"]
    add("### 종목별")
    add("")
    add("| symbol | net PnL |")
    add("|---|---:|")
    for symbol, pnl in primary_official["net_pnl_by_symbol"].items():
        add(f"| {symbol} | {_number(pnl)} |")
    add("")
    add("### 진입일별 상위 절대 기여 20일")
    add("")
    add("| entry day | net PnL |")
    add("|---|---:|")
    entry_days = sorted(
        primary_official["net_pnl_by_entry_day"].items(),
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
        "Paper 또는 실거래 승인을 뜻하지 않는다. `NO_SELECTION`은 사전 고정 경제 게이트 실패이며 "
        "규칙을 결과에 맞춰 수정하지 않는다."
    )
    add("")
    add("### 병합 전 실행 감사")
    add("")
    add(f"- 결함: `{result['execution_audit']['issue']}`")
    add(f"- 수정: `{result['execution_audit']['fix']}`")
    add(
        "- 전략 규칙 변경: "
        f"`{str(result['execution_audit']['strategy_rules_changed']).lower()}`"
    )
    add(
        "- 결과 재생성: "
        f"`{str(result['execution_audit']['result_regenerated']).lower()}`"
    )
    add("")
    return "\n".join(lines)
