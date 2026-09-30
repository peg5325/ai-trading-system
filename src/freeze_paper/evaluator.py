"""10-session evaluator of the FREEZE Paper contract (``evaluation`` section, v1.2).

Inputs are only verified evidence: the validated first-session receipt, the signed and
chained session artifacts, SPY regular closes and verified SPY cash dividends.  Missing,
incomplete or invalid sessions are reported, never dropped or zero-filled.  The result is
a pure function of those inputs (no wall clock).  It is not a live-trading approval and
never promotes anything automatically.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from src.freeze_paper.binding import ContractBinding
from src.freeze_paper.costs import derived_cost_drags
from src.freeze_paper.engine import UNRESOLVED_EXIT, fill_event
from src.freeze_paper.runner import paper_paths, verify_session_artifact
from src.freeze_paper.spy import (
    SPY_CLOSE_SOURCE,
    capture_defect,
    discontinuity_defects,
)
from src.freeze_paper.spy import (
    shift_trading_days as _shift_trading_days,
)
from src.pnl.fifo import FifoMatcher
from src.pnl.metrics import summarize
from src.premarket.contract import sha256_payload
from src.premarket.operations import write_atomic
from src.premarket.report import json_bytes, write_immutable_pair

EVALUATION_SCHEMA = "premarket-freeze-paper/evaluation/v1"
DIVIDENDS_SCHEMA = "spy-dividends/v1"
DIVIDENDS_RELATIVE = Path("docs/research/SPY_DIVIDENDS.json")
FINAL = "FINAL"
INTERIM = "INTERIM"
HALT_AND_REVIEW = "HALT_AND_REVIEW"
EXTEND_OBSERVATION = "EXTEND_OBSERVATION"
NO_PROMOTION = "NO_PROMOTION"
ELIGIBLE_FOR_LIVE_REVIEW = "ELIGIBLE_FOR_LIVE_REVIEW"
INCONCLUSIVE_BENCHMARK = "INCONCLUSIVE_BENCHMARK"
LEGS = ("LONG", "INVERSE")
NOTICE = ("Paper evaluation only. Not a live-trading approval; no automatic promotion. "
          "SPY total return is a practical benchmark and is not the S&P 500 Total Return index.")


class DividendFileError(ValueError):
    """SPY_DIVIDENDS.json is malformed."""


def load_spy_dividends(path: Path) -> list[dict[str, Any]]:
    """Entries with a verified amount carry ``verified=True``; malformed files raise."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != DIVIDENDS_SCHEMA or not isinstance(
            data.get("dividends"), list):
        raise DividendFileError("SPY_DIVIDENDS_SCHEMA")
    out = []
    for item in data["dividends"]:
        if not isinstance(item, Mapping):
            raise DividendFileError("SPY_DIVIDENDS_ENTRY")
        try:
            ex_date = date.fromisoformat(item["ex_date"]).isoformat()
        except (KeyError, TypeError, ValueError) as exc:
            raise DividendFileError("SPY_DIVIDENDS_EX_DATE") from exc
        amount = item.get("amount_usd")
        verified = (isinstance(amount, (int, float)) and not isinstance(amount, bool)
                    and amount > 0 and str(item.get("source_url", "")).startswith("https://")
                    and _is_date(item.get("verified_on")))
        out.append({"ex_date": ex_date, "amount_usd": amount if verified else None,
                    "source_url": item.get("source_url"), "verified": verified})
    return sorted(out, key=lambda d: d["ex_date"])


def expected_quarterly_ex_windows(start_day: str, end_day: str) -> list[tuple[str, str, str]]:
    """(third Friday, window start, window end) for Mar/Jun/Sep/Dec: third Friday ± 2 NYSE
    trading days, kept when the range intersects (start_day, end_day]."""
    out = []
    first, last = date.fromisoformat(start_day), date.fromisoformat(end_day)
    for year in range(first.year, last.year + 1):
        for month in (3, 6, 9, 12):
            day = date(year, month, 15)
            friday = day + timedelta(days=(4 - day.weekday()) % 7)
            lo, hi = _shift_trading_days(friday, -2), _shift_trading_days(friday, 2)
            if lo <= last and hi > first:
                out.append((friday.isoformat(), lo.isoformat(), hi.isoformat()))
    return out


def spy_total_return(*, start_day: str, end_day: str, closes: Mapping[str, Any],
                     dividends: list[Mapping], required_ex_dates: list[str]) -> dict[str, Any]:
    """As-traded close-to-close with each cash dividend reinvested at its ex-date close.
    ``closes`` maps ISO day -> close evidence ({close, captured_at, updated_at, ...})."""
    reasons: list[str] = []
    for friday, lo, hi in expected_quarterly_ex_windows(start_day, end_day):
        if not any(lo <= d <= hi for d in required_ex_dates):
            reasons.append(f"UNLISTED_EXPECTED_EX_DATE:{friday}")
    by_date = {d["ex_date"]: d for d in dividends}
    ex_dates = sorted({d for d in required_ex_dates if start_day < d <= end_day}
                      | {d for d in by_date if start_day < d <= end_day})
    for day in (start_day, end_day, *ex_dates):
        defect = capture_defect(day, closes.get(day))
        if defect is not None:
            reasons.append(defect)
    reasons += discontinuity_defects(start_day, end_day, closes)
    for day in ex_dates:
        if day not in by_date or not by_date[day]["verified"]:
            reasons.append(f"UNVERIFIED_DIVIDEND:{day}")
    if reasons:
        return {"label": "SPY total return", "close_source": SPY_CLOSE_SOURCE,
                "status": INCONCLUSIVE_BENCHMARK, "reasons": sorted(set(reasons)),
                "total_return": None, "ex_dates": ex_dates}
    price = {day: float(closes[day]["close"]) for day in (start_day, end_day, *ex_dates)}
    shares = 1.0
    reinvested = []
    for day in ex_dates:
        amount = float(by_date[day]["amount_usd"])
        shares *= 1.0 + amount / price[day]
        reinvested.append({"ex_date": day, "amount_usd": amount, "ex_close": price[day]})
    total = shares * price[end_day] / price[start_day] - 1.0
    evidence = {day: dict(closes[day]) for day in sorted(price)}
    return {"label": "SPY total return", "close_source": SPY_CLOSE_SOURCE,
            "status": "OK", "reasons": [], "close_evidence": evidence,
            "start_close": price[start_day], "end_close": price[end_day],
            "reinvested": reinvested, "ex_dates": ex_dates, "total_return": total}


def evaluate_window(*, root: Path, receipt: Mapping, binding: ContractBinding,
                    spy_closes: Mapping[str, float | None], dividends: list[Mapping],
                    through: date | None = None) -> dict[str, Any]:
    sessions = [date.fromisoformat(d) for d in receipt["scheduled_sessions"]]
    through = through or sessions[-1]
    if through not in sessions:
        raise ValueError("through must be a scheduled session")
    considered = [d for d in sessions if d <= through]
    rows, artifacts = _session_rows(root, considered, receipt, binding)
    counts = _counts(rows, len(sessions), len(considered))
    evaluable = counts["invalid"] == 0
    window_complete = counts["recorded"] == counts["considered"]
    # FINAL only when all scheduled sessions (the last one included) exist and verify.
    label = FINAL if through == sessions[-1] and window_complete else INTERIM

    metrics = diagnostics = legs = None
    completed_trades = 0
    leg_mismatch = False
    if evaluable and artifacts:
        metrics, diagnostics, legs, completed_trades, leg_mismatch = _account(
            artifacts, receipt, binding, recorded=counts["recorded"], scheduled=len(sessions))
    benchmark = spy_total_return(
        start_day=receipt["window_start_close_date"], end_day=through.isoformat(),
        closes=spy_closes, dividends=dividends,
        required_ex_dates=list(receipt["spy_ex_dividend_dates_in_window"]))
    if metrics is not None:
        if not window_complete:
            # Account covers fewer sessions than the benchmark window: never compare.
            metrics["excess_vs_spy_tr_pct_points"] = None
            metrics["benchmark_comparison"] = "WINDOW_INCOMPLETE"
        elif benchmark["total_return"] is None:
            metrics["benchmark_comparison"] = INCONCLUSIVE_BENCHMARK
        else:
            metrics["excess_vs_spy_tr_pct_points"] = 100.0 * (
                metrics["official_cost_return"] - benchmark["total_return"])
            metrics["benchmark_comparison"] = "OK"

    halt = _halt_reasons(artifacts)
    if leg_mismatch:
        halt = sorted(set(halt) | {"LEG_PNL_MISMATCH"})
    missing = [f"SESSION_{r['status']}:{r['trading_day']}" for r in rows
               if r["status"] in {"MISSING", "INCOMPLETE", "INVALID"}]
    if label == INTERIM:
        missing.append("WINDOW_NOT_COMPLETE")
    if not evaluable or not artifacts:
        missing.append("NOT_EVALUABLE")
    elif completed_trades == 0:
        missing.append("NO_COMPLETED_TRADE")
    if benchmark["status"] != "OK":
        missing.append(INCONCLUSIVE_BENCHMARK)
    if not _paths_verified(receipt):
        missing.append("REQUIRED_PATH_UNVERIFIED")
    economic = [] if metrics is None else _economic_failures(metrics)
    if halt:
        outcome = HALT_AND_REVIEW
    elif missing:
        outcome = EXTEND_OBSERVATION
    elif economic:
        outcome = NO_PROMOTION
    else:
        outcome = ELIGIBLE_FOR_LIVE_REVIEW
    flags = []
    if outcome == ELIGIBLE_FOR_LIVE_REVIEW and legs["LONG"]["official_net_pnl"] <= 0:
        flags.append("ELIGIBILITY_DEPENDS_ON_INVERSE_LEG")

    result = {
        "schema": EVALUATION_SCHEMA,
        "label": label,
        "strategy_template_id": binding.strategy_template_id,
        "contract_sha256": binding.sha256,
        "first_session_receipt_sha256": receipt["artifact_sha256"],
        "window": {"start_close_date": receipt["window_start_close_date"],
                   "end_close_date": through.isoformat(),
                   "first_session": sessions[0].isoformat(),
                   "last_scheduled_session": sessions[-1].isoformat()},
        "session_counts": counts,
        "sessions": rows,
        "evaluable": evaluable,
        "metrics": metrics,
        "diagnostics": diagnostics,
        "legs": legs,
        "benchmark": {
            "spy_total_return": benchmark,
            "official_sp500_total_return": {"status": INCONCLUSIVE_BENCHMARK,
                                            "reasons": ["NO_SOURCE_WIRED"]},
        },
        "halt_reasons": halt,
        "missing_evidence": missing,
        "economic_failures": economic,
        "outcome": outcome,
        "flags": flags,
        "notice": NOTICE,
    }
    result["artifact_sha256"] = sha256_payload(result)
    return result


def evaluation_paths(root: Path, evaluation: Mapping) -> tuple[Path, Path]:
    first = evaluation["window"]["first_session"].replace("-", "")
    last = evaluation["window"]["end_close_date"].replace("-", "")
    kind = "" if evaluation["label"] == FINAL else "interim."
    stem = f"premarket-freeze-paper-evaluation.{kind}{first}_{last}"
    return root / "reports" / f"{stem}.json", root / "reports" / f"{stem}.md"


def write_evaluation(root: Path, evaluation: Mapping) -> tuple[Path, Path]:
    """FINAL: immutable write-once (a differing rerun raises FileExistsError).
    INTERIM: regenerable, replaced atomically."""
    json_path, md_path = evaluation_paths(root, evaluation)
    content = json_bytes(evaluation)
    markdown = render_markdown(evaluation).encode("utf-8")
    if evaluation["label"] == FINAL:
        write_immutable_pair((json_path, content), (md_path, markdown))
    else:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(json_path, content)
        write_atomic(md_path, markdown)
    return json_path, md_path


def render_markdown(evaluation: Mapping) -> str:
    m = evaluation["metrics"]
    spy = evaluation["benchmark"]["spy_total_return"]

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{100.0 * value:+.4f}%"

    window = evaluation["window"]
    lines = [
        f"# FREEZE Paper 평가 ({evaluation['label']})",
        "",
        f"- 구간: {window['start_close_date']} 종가 → {window['end_close_date']} 종가",
        f"- 판정: **{evaluation['outcome']}**",
        "",
        "## 핵심 지표",
        "",
    ]
    if m is None:
        lines.append("- 평가 불가(유효한 세션 artifact 없음 또는 검증 실패)")
    else:
        excess = m["excess_vs_spy_tr_pct_points"]
        excess_text = "n/a" if excess is None else f"{excess:+.4f}%p"
        pnl = m["net_pnl"]
        lines += [
            (f"1. 순 계좌 수익률(기록 장부): {pct(m['net_account_return'])} · 공식 비용 후 "
             f"{pct(m['official_cost_return'])} · 운영비까지 {pct(m['official_opex_return'])}"),
            f"2. SPY 총수익 대비 초과(공식 비용 후): {excess_text}",
            (f"3. 순손익: {pnl['pnl']:+.6f} USD (시작 {pnl['starting_equity']:.2f} → "
             f"{pnl['ending_equity']:.6f}) · stress 손익 {m['stress_pnl']:+.6f}"),
            f"4. 최대 낙폭(장중 포함): {pct(m['max_drawdown'])}",
        ]
        d = evaluation["diagnostics"]
        lines += ["", "## 보조 지표", "",
                  (f"- 완료 거래 {d['trades']} · 승률 {d['win_rate']:.4f} · 평균 이익 "
                   f"{d['avg_win']:.6f} · 평균 손실 {d['avg_loss']:.6f} · profit factor "
                   f"{d['profit_factor']}"),
                  "", "## 다리별(LONG / INVERSE 분리)", "",
                  "| 다리 | 완료 거래 | 실현 순손익 | 공식 비용 후 | stress 후 |",
                  "| --- | ---: | ---: | ---: | ---: |"]
        for leg, row in evaluation["legs"].items():
            lines.append(f"| {leg} | {row['trades']} | {row['realized_net_pnl']:+.6f} | "
                         f"{row['official_net_pnl']:+.6f} | {row['stress_net_pnl']:+.6f} |")
    lines += [
        "",
        "## 벤치마크",
        "",
        f"- SPY 총수익: {spy['status']} {pct(spy['total_return'])} {spy['reasons']}",
        "- 공식 S&P 500 Total Return: INCONCLUSIVE_BENCHMARK (미연결). SPY는 S&P 500 TR이 아니다.",
        "",
        "## 판정 근거",
        "",
        f"- 세션: {evaluation['session_counts']}",
        f"- halt: {evaluation['halt_reasons']}",
        f"- 부족한 증거: {evaluation['missing_evidence']}",
        f"- 경제성 미달: {evaluation['economic_failures']}",
        "",
        f"> {evaluation['notice']}",
        "",
    ]
    return "\n".join(lines)


def _session_rows(root: Path, considered: list[date], receipt: Mapping,
                  binding: ContractBinding) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    artifacts: list[dict] = []
    prev_sha: str | None = None
    chain_ok = True
    for index, day in enumerate(considered):
        paths = paper_paths(root, day)
        if not paths["session"].is_file():
            receipt_doc = _read(paths["operation"])
            status = "INCOMPLETE" if receipt_doc and receipt_doc.get("status") == "INCOMPLETE" \
                else "MISSING"
            rows.append({"trading_day": day.isoformat(), "status": status})
            chain_ok = False
            continue
        artifact = _read(paths["session"])
        defect = verify_session_artifact(
            artifact, day=day, index=index, receipt_sha256=receipt["artifact_sha256"],
            contract_sha256=binding.sha256, prev_sha=prev_sha, check_prev=True)
        if defect is None and not chain_ok:
            defect = "SESSION_AFTER_GAP"
        if defect is None and not isinstance(
                artifact["end_state"].get("session", {}).get("equity_curve"), list):
            defect = "SESSION_EQUITY_CURVE_MISSING"
        if defect is not None:
            rows.append({"trading_day": day.isoformat(), "status": "INVALID", "reason": defect})
            chain_ok = False
            continue
        prev_sha = artifact["artifact_sha256"]
        artifacts.append(artifact)
        rows.append({"trading_day": day.isoformat(), "status": artifact["status"],
                     "artifact_sha256": artifact["artifact_sha256"]})
    return rows, artifacts


def _counts(rows: list[Mapping], scheduled: int, considered: int) -> dict[str, int]:
    status = [r["status"] for r in rows]
    return {
        "scheduled": scheduled, "considered": considered,
        "recorded": sum(s not in {"MISSING", "INCOMPLETE", "INVALID"} for s in status),
        "traded": status.count("TRADED"), "no_trade": status.count("NO_TRADE"),
        "halted": status.count("HALTED"), "unresolved_exit": status.count(UNRESOLVED_EXIT),
        "incomplete": status.count("INCOMPLETE"), "missing": status.count("MISSING"),
        "invalid": status.count("INVALID"),
    }


def _account(artifacts: list[dict], receipt: Mapping, binding: ContractBinding, *,
             recorded: int, scheduled: int):
    start = binding.initial_equity
    end = artifacts[-1]["equity"]["end"]
    fills = artifacts[-1]["end_state"]["fills"]
    drags = derived_cost_drags(fills, binding)
    opex = float(receipt["operating_costs"]["total_usd"])
    official_end = end - drags["official_drag"]
    stress_end = end - drags["stress_drag"]
    curve = [start] + [e for a in artifacts for _, e in a["end_state"]["session"]["equity_curve"]]
    peak, mdd = start, 0.0
    for equity in curve:
        peak = max(peak, equity)
        mdd = max(mdd, (peak - equity) / peak)
    matcher = FifoMatcher(bar_minutes=1.0)
    matcher.ingest_many(fill_event(f) for f in fills)
    summary = summarize(matcher.trades, matcher.open_lots())
    metrics = {
        "net_account_return": end / start - 1.0,
        "official_cost_return": official_end / start - 1.0,
        "official_opex_return": (official_end - opex) / start - 1.0,
        "excess_vs_spy_tr_pct_points": None,
        "net_pnl": {"starting_equity": start, "ending_equity": end, "pnl": end - start},
        "max_drawdown": mdd,
        "stress_pnl": stress_end - start,
        "official_drag": drags["official_drag"],
        "stress_drag": drags["stress_drag"],
        "operating_costs_usd": opex,
        "filled_notional": drags["filled_notional"],
    }
    diagnostics = {"trades": summary.trades, "wins": summary.wins, "losses": summary.losses,
                   "win_rate": summary.win_rate, "avg_win": summary.avg_win,
                   "avg_loss": summary.avg_loss, "profit_factor": summary.profit_factor,
                   "open_positions": summary.open_positions,
                   # secondary only: the contract result above subtracts the full total
                   "operating_costs_prorated_usd": opex * recorded / scheduled,
                   "official_opex_return_prorated": (
                       (official_end - opex * recorded / scheduled) / start - 1.0)}
    legs = {}
    for leg in LEGS:
        leg_fills = [f for f in fills if f["leg"] == leg]
        leg_matcher = FifoMatcher(bar_minutes=1.0)
        leg_matcher.ingest_many(fill_event(f) for f in leg_fills)
        leg_drags = derived_cost_drags(leg_fills, binding)
        realized = sum(t.net_pnl for t in leg_matcher.trades)
        legs[leg] = {
            "trades": len(leg_matcher.trades),
            "realized_net_pnl": realized,
            "fees": sum(f["fee"] for f in leg_fills),
            "official_net_pnl": realized - leg_drags["official_drag"],
            "stress_net_pnl": realized - leg_drags["stress_drag"],
            "open_positions": len({lot.symbol for lot in leg_matcher.open_lots()}),
        }
    leg_mismatch = abs(sum(row["realized_net_pnl"] for row in legs.values())
                       - summary.total_net_pnl) > 1e-9
    return metrics, diagnostics, legs, summary.trades, leg_mismatch


def _halt_reasons(artifacts: list[Mapping]) -> list[str]:
    reasons: set[str] = set()
    for artifact in artifacts:
        halted = artifact.get("halted")
        if halted:
            reasons.add(f"RISK_HALT:{halted.get('reason')}")
        if artifact["status"] == UNRESOLVED_EXIT:
            reasons.add(UNRESOLVED_EXIT)
        if not artifact["reconciliation"]["ok"]:
            reasons.add("RECONCILIATION_MISMATCH")
        types = {e["type"] for e in artifact["events"]}
        if "DUPLICATE_CLIENT_ORDER_ID" in types:
            reasons.add("DUPLICATE_ORDER")
        if "LEDGER_INCONSISTENT" in types:
            reasons.add("LEDGER_INCONSISTENT")
    return sorted(reasons)


def _economic_failures(metrics: Mapping) -> list[str]:
    failures = []
    if not metrics["official_cost_return"] > 0:
        failures.append("OFFICIAL_COST_RETURN_NOT_POSITIVE")
    if not metrics["official_opex_return"] > 0:
        failures.append("OFFICIAL_AND_OPEX_RETURN_NOT_POSITIVE")
    excess = metrics["excess_vs_spy_tr_pct_points"]
    if excess is None or not excess > 0:
        failures.append("EXCESS_VS_SPY_TR_NOT_POSITIVE")
    if not metrics["stress_pnl"] >= 0:
        failures.append("STRESS_PNL_NEGATIVE")
    return failures


def _paths_verified(receipt: Mapping) -> bool:
    paths = receipt.get("verified_paths") or {}
    return (all(paths.get(k) for k in ("restart", "eod", "reconciliation", "duplicate"))
            and (receipt.get("test_run") or {}).get("passed") is True)


def _read(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _is_date(value: Any) -> bool:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True

