"""운영 종료 Report 자동 생성 (markdown)."""
from __future__ import annotations

import os


def _resources() -> tuple[str, str]:
    """best-effort memory/cpu (실패 시 'n/a')."""
    mem, cpu = "n/a", "n/a"
    try:
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF)
        # macOS: bytes, Linux: KB
        raw = ru.ru_maxrss
        mb = raw / (1024 * 1024) if raw > 10_000_000 else raw / 1024
        mem = f"{mb:.1f} MB"
        cpu = f"{(ru.ru_utime + ru.ru_stime):.2f} s (user+sys)"
    except Exception:
        pass
    return mem, cpu


def _ratio_num(ratio) -> str:
    """cost_ratio 숫자 표시. gross≈0 로 비율이 극단(≥10)이면 정수로 축약(가독성)."""
    if ratio is None:
        return "unstable"
    return f"{ratio:.0f}" if ratio >= 10 else f"{ratio:.3f}"


def _fmt_ratio(ratio, severity: str) -> str:
    """cost_ratio 표시. None(unstable) 이면 gross≤0 안내 문구."""
    if ratio is None:
        return "unstable / not meaningful (gross ≤ 0)"
    return f"{_ratio_num(ratio)} [{severity}]"


def _cost_section(cost: dict | None) -> list[str]:
    """마찰비용 관측 섹션(계좌 요약 + 종목별 Cost Drag 표). read-only 리포팅."""
    if not cost:
        return []
    lines = [
        "## Cost Analysis (마찰비용 관측)",
        f"- Commission: {cost.get('commission', 0.0):,.2f} | "
        f"Slippage: {cost.get('slippage', 0.0):,.2f} | "
        f"Total cost: {cost.get('total_cost', 0.0):,.2f}",
        f"- Gross PnL: {cost.get('gross_pnl', 0.0):,.2f} | "
        f"Net PnL: {cost.get('net_pnl', 0.0):,.2f}",
        f"- Cost ratio (cost/gross): {_fmt_ratio(cost.get('cost_ratio'), cost.get('severity', 'n/a'))}",
    ]
    impact = cost.get("cost_impact_net")
    if impact is not None:
        lines.append(f"- Cost impact (cost/|net|): {impact:.3f}")
    lines.append(
        f"- 기준: WARNING ≥ {cost.get('warning_ratio', 0.30):.2f}, "
        f"SEVERE ≥ {cost.get('severe_ratio', 0.50):.2f} · "
        "gross ≤ 0 은 비율 무의미(unstable) 처리")
    per_symbol = cost.get("per_symbol") or []
    if per_symbol:
        lines += [
            "",
            "### Cost Drag by Symbol",
            "| Symbol | Trades | Gross | Comm | Slippage | Net | CostRatio | AvgHold(m) | Turnover | Flag |",
            "|---|--:|--:|--:|--:|--:|:--:|--:|--:|:--:|",
        ]
        for s in per_symbol:
            ratio_cell = _ratio_num(s.get("cost_ratio"))
            hold = s.get("avg_holding_minutes")
            hold_cell = f"{hold:.1f}" if hold is not None else "n/a"
            lines.append(
                f"| {s.get('symbol')} | {s.get('trade_count', 0)} | "
                f"{s.get('gross_pnl', 0.0):,.2f} | {s.get('commission', 0.0):,.2f} | "
                f"{s.get('slippage', 0.0):,.2f} | {s.get('net_pnl', 0.0):,.2f} | "
                f"{ratio_cell} | {hold_cell} | {s.get('turnover', 0.0):,.0f} | "
                f"{s.get('severity', 'n/a')} |")
    lines.append("")
    return lines


def generate_markdown(metrics, health: dict, cfg, *, report_date: str,
                      run_duration_s: float, realized: dict | None = None,
                      performance: dict | None = None) -> str:
    m = metrics
    mem, cpu = _resources()
    downtime = round((m.api_errors + m.pipeline_errors + m.exceptions) * cfg.poll_interval_s, 1)
    lines = [
        f"# Paper Trading Report — {report_date}",
        "",
        f"- Window run duration: {run_duration_s:.1f}s (poll {cfg.poll_interval_s}s)",
        f"- Symbols: {', '.join(cfg.symbols)}",
        f"- Broker: PaperBrokerProvider (실주문 없음)",
        f"- Health(final): {health.get('status', 'n/a')}",
        "",
        "## Cycles",
        f"- Total iterations: {m.iterations}",
        f"- Cycles run: {m.cycles_run}",
        f"- Skipped (benign): {m.skipped}",
        "",
        "## Decisions",
        f"- Candidates: {m.candidates}",
        f"- AI Approve: {m.ai_approve}",
        f"- AI Wait: {m.ai_wait}",
        f"- AI Reject: {m.ai_reject}",
        f"- Risk Block: {m.risk_block}",
        "",
        "## Orders (Paper)",
        f"- Order: {m.orders}",
        f"- Buy: {m.buys}",
        f"- Sell: {m.sells}",
        f"- Exit: {m.exits}",
        f"- EOD: {m.eods}",
        "",
        "## Stability",
        f"- Exception: {m.exceptions}",
        f"- API Error: {m.api_errors}",
        f"- Pipeline Error: {m.pipeline_errors}",
        f"- SQLite Lock: {m.sqlite_locks}",
        f"- Loop Downtime (approx): {downtime}s",
        f"- Avg Loop time: {m.avg_loop_ms()} ms",
        f"- Avg AI response: {m.avg_ai_ms()} ms",
        "",
        "## Resources",
        f"- Memory: {mem}",
        f"- CPU: {cpu}",
        "",
    ]
    if realized is not None:
        pf = realized.get("profit_factor")
        lines += [
            "## Realized PnL (FIFO, 청산 완료 거래)",
            f"- Closed trades: {realized.get('trades', 0)} "
            f"(W {realized.get('wins', 0)} / L {realized.get('losses', 0)} / BE {realized.get('breakeven', 0)})",
            f"- Win rate: {realized.get('win_rate', 0.0) * 100:.1f}%",
            f"- Net PnL (cum): {realized.get('total_net_pnl', 0.0):,.2f}",
            f"- Gross PnL: {realized.get('total_gross_pnl', 0.0):,.2f} | "
            f"Commission: {realized.get('total_commission', 0.0):,.2f} | "
            f"Slippage: {realized.get('total_slippage', 0.0):,.2f}",
            f"- Expectancy (avg/trade): {realized.get('expectancy', 0.0):,.4f}",
            f"- Profit Factor: {pf if pf is not None else 'n/a'}",
            f"- Avg holding: {realized.get('avg_holding_minutes')} min / "
            f"{realized.get('avg_holding_bars')} bars",
            f"- Open positions: {realized.get('open_positions', 0)} "
            f"(qty {realized.get('open_quantity', 0.0)})",
            "- 주: 통화 혼합 계좌는 금액 합산에 주의(관찰/검증용).",
        ]
        exit_reasons = realized.get("exit_reasons") or {}
        if exit_reasons:
            breakdown = ", ".join(f"{k}={v}" for k, v in sorted(exit_reasons.items()))
            lines.append(f"- Exit reasons: {breakdown}")
        lines.append("")
        lines += _cost_section(realized.get("cost"))
    if performance is not None:
        sharpe = performance.get("sharpe_ratio")
        pf = performance.get("profit_factor")
        lines += [
            "## Performance",
            f"- 초기자산: {performance.get('initial_capital', 0.0):,.2f}",
            f"- 최종자산: {performance.get('final_equity', 0.0):,.2f}",
            f"- 수익률: {performance.get('total_return_pct', 0.0):.2f}%",
            f"- 승률: {performance.get('win_rate', 0.0) * 100:.1f}%",
            f"- Profit Factor: {pf if pf is not None else 'n/a'}",
            f"- Expectancy: {performance.get('expectancy', 0.0):,.4f}",
            f"- MDD: {performance.get('max_drawdown_pct', 0.0):.2f}%",
            f"- Sharpe: {sharpe if sharpe is not None else 'n/a'}",
            "",
        ]
    return "\n".join(lines)


def write_report(content: str, reports_dir: str, report_date: str) -> str:
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, f"paper_report_{report_date}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
