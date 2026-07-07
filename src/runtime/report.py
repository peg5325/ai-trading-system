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


def generate_markdown(metrics, health: dict, cfg, *, report_date: str,
                      run_duration_s: float, realized: dict | None = None) -> str:
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
            "",
        ]
    return "\n".join(lines)


def write_report(content: str, reports_dir: str, report_date: str) -> str:
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, f"paper_report_{report_date}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
