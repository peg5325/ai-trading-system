"""Post-close evaluation for immutable premarket FREEZE Daily Briefs.

This module is read-only with respect to market data and has no broker/order
authority.  It evaluates the frozen watchlist as a diagnostic direction proxy:
equal weight, regular-session first 1m open to last 1m close, with fixed
round-trip cost sensitivities.  It is not paper execution.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from src.history.exchange_calendar import expected_regular_minutes
from src.history.oos_guard import TRACK_INTRADAY, research_load
from src.models import ET, SESSION_REGULAR, Candle
from src.premarket.contract import sha256_payload
from src.premarket.report import json_bytes
from src.premarket.schedule import build_premarket_schedule, parse_iso

POSTCLOSE_SCHEMA = "premarket-postclose-evaluation/v1"
LEDGER_SCHEMA = "premarket-postclose-ledger/v1"
CUMULATIVE_SCHEMA = "premarket-postclose-cumulative/v1"
OFFICIAL_ROUND_TRIP_COST_RATE = 0.0034
STRESS_ROUND_TRIP_COST_RATE = 0.0068


@dataclass(frozen=True)
class RegularSessionResult:
    symbol: str
    status: str
    reason_codes: tuple[str, ...]
    direction: str | None = None
    direction_sign: int | None = None
    open: float | None = None
    close: float | None = None
    high: float | None = None
    low: float | None = None
    gross_return: float | None = None
    official_cost_return: float | None = None
    stress_cost_return: float | None = None
    net_return_official_cost: float | None = None
    net_return_stress_cost: float | None = None
    mfe_return: float | None = None
    mae_return: float | None = None
    observed_regular_minutes: int = 0
    expected_regular_minutes: int = 0
    first_bar_utc: str | None = None
    last_bar_utc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_postclose_evaluation(
    freeze_brief: Mapping[str, Any],
    *,
    market_db: str | Path,
    evaluation_as_of: str,
    freeze_input_file_sha256: str | None = None,
) -> dict[str, Any]:
    """Evaluate a FREEZE Daily Brief after regular close using local history only."""
    brief = _extract_brief(freeze_brief)
    trading_day = date.fromisoformat(_require_str(brief, "trading_day"))
    schedule = build_premarket_schedule(trading_day)
    requested_as_of_dt = parse_iso(evaluation_as_of)
    close_dt = parse_iso(schedule.market_close_utc)

    frozen_stage = _freeze_stage(freeze_brief, brief)
    freeze_artifact_sha256 = _artifact_sha256(freeze_brief)
    _validate_optional_sha256(freeze_input_file_sha256, "freeze_input_file_sha256")
    frozen_watchlist = _watchlist(brief)
    base = {
        "schema": POSTCLOSE_SCHEMA,
        "trading_day": trading_day.isoformat(),
        "evaluation_as_of": schedule.market_close_utc,
        "status": "INCOMPLETE_PRE_CLOSE",
        "reason_codes": ["PRE_CLOSE_EVALUATION_BLOCKED"],
        "schedule": schedule.to_dict(),
        "freeze": {
            "stage": frozen_stage,
            "run_id": brief.get("run_id"),
            "status": brief.get("status"),
            "snapshot_sha256": brief.get("snapshot_sha256"),
            "artifact_sha256": freeze_artifact_sha256,
            "input_file_sha256": freeze_input_file_sha256,
            "watchlist_count": len(frozen_watchlist),
            "decision_executable": bool(brief.get("executable", False)),
            "reason_codes": list(brief.get("reason_codes", [])),
        },
        "policy": {
            "proxy_name": "FREEZE_MAX3_EQUAL_WEIGHT_REGULAR_OPEN_TO_CLOSE",
            "official_round_trip_cost_rate": OFFICIAL_ROUND_TRIP_COST_RATE,
            "stress_round_trip_cost_rate": STRESS_ROUND_TRIP_COST_RATE,
            "not_paper_execution": True,
            "order_count": 0,
            "lookahead_guard": "requested evaluation time must be >= market_close_utc",
            "logical_evaluation_boundary": schedule.market_close_utc,
        },
        "benchmark": {
            "official_sp500_total_return_status": "INCONCLUSIVE_BENCHMARK",
            "reason": "S&P 500 Total Return source is not wired for this read-only evaluator",
            "spy_same_session_is_diagnostic_only": True,
        },
        "candidates": [],
        "portfolio_proxy": _empty_portfolio(),
        "spy_diagnostic": None,
        "notes": [
            "Read-only post-close evaluation; not a trading strategy approval.",
            "SPY diagnostic is same-session ETF open-to-close only, not S&P 500 total return.",
        ],
    }
    if frozen_stage != "FREEZE":
        base["status"] = "INCOMPLETE_INVALID_FREEZE"
        base["reason_codes"] = ["BRIEF_IS_NOT_FREEZE_STAGE"]
        return _with_artifact_sha256(base)
    if requested_as_of_dt < close_dt:
        return _with_artifact_sha256(base)

    expected = expected_regular_minutes(trading_day)
    start = parse_iso(schedule.market_open_utc)
    end = close_dt

    results: list[dict[str, Any]] = []
    for rank, item in enumerate(frozen_watchlist, start=1):
        symbol = str(item["symbol"]).upper()
        bars = research_load(
            str(market_db),
            symbol,
            "1m",
            start=start,
            end=end,
            purpose="performance",
            track=TRACK_INTRADAY,
            market="US",
        )
        session_result = evaluate_symbol_session(
            symbol=symbol,
            direction=str(item.get("bias", "")),
            bars=bars,
            trading_day=trading_day,
            expected_minutes=expected,
        ).to_dict()
        session_result["rank"] = rank
        session_result["freeze_score"] = item.get("score")
        session_result["strategy_template_id"] = item.get("strategy_template_id")
        session_result["strategy_template_approved"] = bool(
            item.get("strategy_template_approved", False)
        )
        session_result["evidence_event_ids"] = [
            evidence.get("event_id") for evidence in item.get("evidence", [])
        ]
        results.append(session_result)

    # SPY is always a long, same-session diagnostic. A FREEZE watchlist could
    # contain SPY with a SHORT_WATCH bias, which must not change this baseline.
    spy_bars = research_load(
        str(market_db),
        "SPY",
        "1m",
        start=start,
        end=end,
        purpose="performance",
        track=TRACK_INTRADAY,
        market="US",
    )
    spy = evaluate_symbol_session(
        symbol="SPY",
        direction="LONG_WATCH",
        bars=spy_bars,
        trading_day=trading_day,
        expected_minutes=expected,
    ).to_dict()

    portfolio = summarize_portfolio(results, spy)
    complete = all(row["status"] == "COMPLETE" for row in results)
    if not results:
        base["status"] = "COMPLETE_NO_WATCHLIST"
        base["reason_codes"] = ["NO_FREEZE_WATCHLIST"]
    else:
        base["status"] = "COMPLETE" if complete else "INCOMPLETE_MARKET_DATA"
        base["reason_codes"] = [] if complete else sorted({
            reason
            for row in results
            for reason in row["reason_codes"]
        })
    base["candidates"] = results
    base["portfolio_proxy"] = portfolio
    base["spy_diagnostic"] = spy
    return _with_artifact_sha256(base)


def evaluate_symbol_session(
    *,
    symbol: str,
    direction: str,
    bars: Sequence[Candle],
    trading_day: date,
    expected_minutes: int,
) -> RegularSessionResult:
    direction_sign = _direction_sign(direction)
    regular = tuple(
        bar for bar in bars
        if bar.session == SESSION_REGULAR and bar.ts_exchange.date() == trading_day
    )
    reasons: list[str] = []
    if direction_sign == 0:
        reasons.append("UNSUPPORTED_DIRECTION")
    if len(regular) != expected_minutes:
        reasons.append("INCOMPLETE_REGULAR_SESSION")
    if not regular:
        reasons.append("NO_REGULAR_BARS")
    reasons.extend(_session_grid_defects(regular, trading_day, expected_minutes))
    if _has_duplicate_minutes(regular):
        reasons.append("DUPLICATE_REGULAR_MINUTE")
    if reasons:
        return RegularSessionResult(
            symbol=symbol,
            status="INCOMPLETE",
            reason_codes=tuple(sorted(set(reasons))),
            direction=direction or None,
            direction_sign=direction_sign or None,
            observed_regular_minutes=len(regular),
            expected_regular_minutes=expected_minutes,
            first_bar_utc=_iso_z(regular[0].ts_utc) if regular else None,
            last_bar_utc=_iso_z(regular[-1].ts_utc) if regular else None,
        )

    first = regular[0]
    last = regular[-1]
    if first.open <= 0:
        return RegularSessionResult(
            symbol=symbol,
            status="INCOMPLETE",
            reason_codes=("NON_POSITIVE_OPEN",),
            direction=direction,
            direction_sign=direction_sign,
            observed_regular_minutes=len(regular),
            expected_regular_minutes=expected_minutes,
            first_bar_utc=_iso_z(first.ts_utc),
            last_bar_utc=_iso_z(last.ts_utc),
        )

    raw_return = (last.close / first.open) - 1.0
    gross = raw_return * direction_sign
    high = max(bar.high for bar in regular)
    low = min(bar.low for bar in regular)
    if direction_sign > 0:
        mfe = (high / first.open) - 1.0
        mae = (low / first.open) - 1.0
    else:
        # Keep excursions on the same entry-notional return convention as
        # ``gross_return``.  Reciprocal returns (open / price - 1) would
        # overstate a short's favorable move and understate its adverse move.
        mfe = (first.open - low) / first.open
        mae = (first.open - high) / first.open
    return RegularSessionResult(
        symbol=symbol,
        status="COMPLETE",
        reason_codes=(),
        direction=direction,
        direction_sign=direction_sign,
        open=first.open,
        close=last.close,
        high=high,
        low=low,
        gross_return=gross,
        official_cost_return=OFFICIAL_ROUND_TRIP_COST_RATE,
        stress_cost_return=STRESS_ROUND_TRIP_COST_RATE,
        net_return_official_cost=gross - OFFICIAL_ROUND_TRIP_COST_RATE,
        net_return_stress_cost=gross - STRESS_ROUND_TRIP_COST_RATE,
        mfe_return=mfe,
        mae_return=mae,
        observed_regular_minutes=len(regular),
        expected_regular_minutes=expected_minutes,
        first_bar_utc=_iso_z(first.ts_utc),
        last_bar_utc=_iso_z(last.ts_utc),
    )


def summarize_portfolio(
    candidates: Sequence[Mapping[str, Any]],
    spy_diagnostic: Mapping[str, Any] | None,
) -> dict[str, Any]:
    complete = [row for row in candidates if row.get("status") == "COMPLETE"]
    total = len(candidates)
    if total == 0:
        return _empty_portfolio(status="NO_CANDIDATES")
    if len(complete) != total:
        return _empty_portfolio(
            status="INCOMPLETE",
            candidate_count=total,
            complete_candidate_count=len(complete),
        )
    gross = _mean(float(row["gross_return"]) for row in complete)
    official = _mean(float(row["net_return_official_cost"]) for row in complete)
    stress = _mean(float(row["net_return_stress_cost"]) for row in complete)
    spy_return = (
        float(spy_diagnostic["gross_return"])
        if spy_diagnostic and spy_diagnostic.get("status") == "COMPLETE"
        else None
    )
    return {
        "status": "COMPLETE",
        "candidate_count": total,
        "complete_candidate_count": len(complete),
        "weighting": "equal",
        "gross_return": gross,
        "net_return_official_cost": official,
        "net_return_stress_cost": stress,
        "official_cost_excess_vs_spy_diagnostic": (
            official - spy_return if spy_return is not None else None
        ),
        "stress_cost_excess_vs_spy_diagnostic": (
            stress - spy_return if spy_return is not None else None
        ),
    }


def load_json(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise TypeError("JSON root must be an object")
    return data


def render_evaluation_markdown(evaluation: Mapping[str, Any]) -> str:
    lines = [
        "# 장전 판단 사후평가",
        "",
        f"- trading day: `{evaluation['trading_day']}`",
        f"- status: **{evaluation['status']}**",
        f"- evaluation as-of: `{evaluation['evaluation_as_of']}`",
        f"- freeze run: `{evaluation['freeze']['run_id']}`",
        f"- freeze artifact SHA-256: `{evaluation['freeze']['artifact_sha256']}`",
        f"- post-close artifact SHA-256: `{evaluation['artifact_sha256']}`",
        "- 주문 생성: **0건** (방향성 proxy, 모의/실거래 아님)",
        "",
        "## Equal-weight 방향성 proxy",
        "",
        f"- gross return: `{_pct(evaluation['portfolio_proxy'].get('gross_return'))}`",
        f"- official cost net: `{_pct(evaluation['portfolio_proxy'].get('net_return_official_cost'))}`",
        f"- stress cost net: `{_pct(evaluation['portfolio_proxy'].get('net_return_stress_cost'))}`",
        (
            "- SPY diagnostic excess (official cost): "
            f"`{_pct(evaluation['portfolio_proxy'].get('official_cost_excess_vs_spy_diagnostic'))}`"
        ),
        "",
        "## 후보별 결과",
        "",
        "| 순위 | 종목 | 방향 | 상태 | open | close | gross | official net | stress net | 사유 |",
        "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in evaluation["candidates"]:
        lines.append(
            f"| {row['rank']} | {row['symbol']} | {row.get('direction') or '-'} | "
            f"{row['status']} | {_num(row.get('open'))} | {_num(row.get('close'))} | "
            f"{_pct(row.get('gross_return'))} | {_pct(row.get('net_return_official_cost'))} | "
            f"{_pct(row.get('net_return_stress_cost'))} | "
            f"{', '.join(row.get('reason_codes') or []) or '-'} |"
        )
    spy = evaluation.get("spy_diagnostic")
    if spy:
        lines.extend([
            "",
            "## SPY 동시간대 진단",
            "",
            f"- status: `{spy['status']}`",
            f"- open→close gross: `{_pct(spy.get('gross_return'))}`",
            f"- reason codes: `{', '.join(spy.get('reason_codes') or []) or '-'}`",
        ])
    lines.extend([
        "",
        "## 경계",
        "",
        "- 이 결과는 FREEZE watchlist의 사후 방향성 진단이며 Paper 체결 성과가 아니다.",
        "- SPY는 동시간대 ETF 진단값이며 공식 S&P 500 Total Return 판정이 아니다.",
        "- 결측 후보는 제외하거나 0으로 채우지 않고 전체 평가를 incomplete로 닫는다.",
        "",
    ])
    return "\n".join(lines)


def load_or_create_ledger(path: str | Path) -> dict[str, Any]:
    ledger_path = Path(path)
    if not ledger_path.exists():
        return {"schema": LEDGER_SCHEMA, "entries": []}
    ledger = load_json(ledger_path)
    if ledger.get("schema") != LEDGER_SCHEMA:
        raise ValueError(f"ledger schema must be {LEDGER_SCHEMA}")
    if not isinstance(ledger.get("entries"), list):
        raise TypeError("ledger entries must be a list")
    _validate_unique_trading_days(ledger["entries"])
    return ledger


def upsert_ledger_entry(
    ledger: Mapping[str, Any],
    evaluation: Mapping[str, Any],
) -> dict[str, Any]:
    entry = _ledger_entry(evaluation)
    entries = list(ledger["entries"])
    replaced = False
    for idx, existing in enumerate(entries):
        if existing.get("trading_day") == entry["trading_day"]:
            if existing != entry:
                raise ValueError(f"conflicting ledger entry for {entry['trading_day']}")
            replaced = True
            break
    if not replaced:
        entries.append(entry)
    entries.sort(key=lambda item: item["trading_day"])
    return {"schema": LEDGER_SCHEMA, "entries": entries}


def build_cumulative_report(ledger: Mapping[str, Any]) -> dict[str, Any]:
    entries = list(ledger["entries"])
    _validate_unique_trading_days(entries)
    complete = [e for e in entries if e["status"] == "COMPLETE"]
    incomplete = [e for e in entries if e["status"] != "COMPLETE"]
    official_returns = [e["net_return_official_cost"] for e in complete]
    stress_returns = [e["net_return_stress_cost"] for e in complete]
    spy_excess = [
        e["official_cost_excess_vs_spy_diagnostic"]
        for e in complete
        if e["official_cost_excess_vs_spy_diagnostic"] is not None
    ]
    return {
        "schema": CUMULATIVE_SCHEMA,
        "status": "COMPLETE" if not incomplete else "INCOMPLETE_PROXY_COVERAGE",
        "ledger_entry_count": len(entries),
        "complete_entry_count": len(complete),
        "incomplete_entry_count": len(incomplete),
        "first_trading_day": entries[0]["trading_day"] if entries else None,
        "last_trading_day": entries[-1]["trading_day"] if entries else None,
        # A period headline is only valid when every recorded session has a
        # complete proxy. Keep complete-only values as diagnostics instead of
        # silently treating missing sessions as zero or omitting them.
        "cumulative_net_return_official_cost": (
            _compound(official_returns) if not incomplete else None
        ),
        "cumulative_net_return_stress_cost": (
            _compound(stress_returns) if not incomplete else None
        ),
        "complete_only_cumulative_net_return_official_cost": _compound(official_returns),
        "complete_only_cumulative_net_return_stress_cost": _compound(stress_returns),
        "mean_official_cost_excess_vs_spy_diagnostic": (
            _mean(spy_excess) if spy_excess else None
        ),
        "entries": entries,
    }


def render_cumulative_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# 장전 판단 사후평가 누적 장부",
        "",
        f"- status: `{report['status']}`",
        f"- entries: `{report['ledger_entry_count']}`",
        f"- complete: `{report['complete_entry_count']}`",
        f"- incomplete: `{report['incomplete_entry_count']}`",
        f"- period: `{report['first_trading_day']}` → `{report['last_trading_day']}`",
        (
            "- cumulative official-cost net (all recorded sessions): "
            f"`{_pct(report['cumulative_net_return_official_cost'])}`"
        ),
        (
            "- cumulative stress-cost net (all recorded sessions): "
            f"`{_pct(report['cumulative_net_return_stress_cost'])}`"
        ),
        (
            "- complete-only official-cost net (diagnostic): "
            f"`{_pct(report['complete_only_cumulative_net_return_official_cost'])}`"
        ),
        "",
        "| 일자 | 상태 | 후보 | official net | stress net | SPY excess | artifact |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in report["entries"]:
        lines.append(
            f"| {row['trading_day']} | {row['status']} | {row['candidate_count']} | "
            f"{_pct(row['net_return_official_cost'])} | "
            f"{_pct(row['net_return_stress_cost'])} | "
            f"{_pct(row['official_cost_excess_vs_spy_diagnostic'])} | "
            f"`{row['artifact_sha256'][:12]}` |"
        )
    lines.extend([
        "",
        "이 누적 장부는 사후 방향성 proxy만 집계한다. 모의 체결·잔고·위험 제어 성과는 별도 단계에서 산출한다.",
        "",
    ])
    return "\n".join(lines)


def write_json(path: str | Path, value: Mapping[str, Any]) -> None:
    write_bytes_atomic(path, json_bytes(value))


def write_text(path: str | Path, value: str) -> None:
    write_bytes_atomic(path, value.encode("utf-8"))


def write_bytes_atomic(path: str | Path, content: bytes) -> None:
    """Atomically replace a mutable derived artifact.

    Daily evaluations are write-once. The ledger and cumulative report grow as
    later days arrive, so they instead use a durable single-file replacement.
    A failed run can be retried: the immutable evaluation remains the source
    of truth and reconstructs the derived files.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.{os.getpid()}.partial")
    try:
        with open(partial, "xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(partial, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if partial.exists():
            partial.unlink()
        raise


def _extract_brief(value: Mapping[str, Any]) -> Mapping[str, Any]:
    brief = value.get("brief")
    if not isinstance(brief, Mapping):
        raise TypeError("freeze Daily Brief must contain a brief object")
    if brief.get("schema") != "premarket-daily-brief/v2":
        raise ValueError("brief schema must be premarket-daily-brief/v2")
    return brief


def _freeze_stage(value: Mapping[str, Any], brief: Mapping[str, Any]) -> str | None:
    payload = value.get("snapshot", {}).get("payload", {}) if isinstance(value.get("snapshot"), Mapping) else {}
    source_input = payload.get("input", {}) if isinstance(payload, Mapping) else {}
    stage = source_input.get("stage")
    if stage:
        return str(stage).upper()
    run_id = str(brief.get("run_id", "")).lower()
    expires_at = brief.get("expires_at")
    schedule = payload.get("schedule", {}) if isinstance(payload, Mapping) else {}
    if ".freeze" in run_id or (expires_at and expires_at == schedule.get("freeze_utc")):
        return "FREEZE"
    return None


def _watchlist(brief: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = brief.get("watchlist")
    if not isinstance(raw, list):
        raise TypeError("brief.watchlist must be a list")
    if len(raw) > 3:
        raise ValueError("freeze Daily Brief watchlist exceeds max 3 candidates")
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise TypeError(f"brief.watchlist[{index}] must be an object")
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError(f"brief.watchlist[{index}].symbol must be non-empty")
        direction = item.get("bias")
        if not isinstance(direction, str) or _direction_sign(direction) == 0:
            raise ValueError(f"brief.watchlist[{index}].bias is unsupported")
    return list(raw)


def _artifact_sha256(value: Mapping[str, Any]) -> str:
    existing = value.get("artifact_sha256")
    if not isinstance(existing, str) or len(existing) != 64:
        raise ValueError("freeze Daily Brief requires artifact_sha256")
    recomputed_payload = dict(value)
    recomputed_payload.pop("artifact_sha256", None)
    recomputed = sha256_payload(recomputed_payload)
    if existing != recomputed:
        raise ValueError("freeze Daily Brief artifact_sha256 mismatch")
    return existing


def _validate_optional_sha256(value: str | None, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a SHA-256 hex string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a SHA-256 hex string") from exc


def _with_artifact_sha256(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("artifact_sha256", None)
    result["artifact_sha256"] = sha256_payload(result)
    return result


def _ledger_entry(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    portfolio = evaluation["portfolio_proxy"]
    return {
        "trading_day": evaluation["trading_day"],
        "status": evaluation["status"],
        "reason_codes": list(evaluation["reason_codes"]),
        "candidate_count": portfolio["candidate_count"],
        "complete_candidate_count": portfolio["complete_candidate_count"],
        "gross_return": portfolio.get("gross_return"),
        "net_return_official_cost": portfolio.get("net_return_official_cost"),
        "net_return_stress_cost": portfolio.get("net_return_stress_cost"),
        "official_cost_excess_vs_spy_diagnostic": portfolio.get(
            "official_cost_excess_vs_spy_diagnostic"
        ),
        "freeze_artifact_sha256": evaluation["freeze"]["artifact_sha256"],
        "artifact_sha256": evaluation["artifact_sha256"],
    }


def _empty_portfolio(
    *,
    status: str = "INCOMPLETE",
    candidate_count: int = 0,
    complete_candidate_count: int = 0,
) -> dict[str, Any]:
    return {
        "status": status,
        "candidate_count": candidate_count,
        "complete_candidate_count": complete_candidate_count,
        "weighting": "equal",
        "gross_return": None,
        "net_return_official_cost": None,
        "net_return_stress_cost": None,
        "official_cost_excess_vs_spy_diagnostic": None,
        "stress_cost_excess_vs_spy_diagnostic": None,
    }


def _validate_unique_trading_days(entries: Sequence[Mapping[str, Any]]) -> None:
    days: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise TypeError(f"ledger.entries[{index}] must be an object")
        trading_day = entry.get("trading_day")
        if not isinstance(trading_day, str) or not trading_day:
            raise ValueError(f"ledger.entries[{index}].trading_day must be non-empty")
        date.fromisoformat(trading_day)
        days.append(trading_day)
    if len(days) != len(set(days)):
        raise ValueError("ledger contains duplicate trading_day entries")


def _direction_sign(direction: str) -> int:
    text = direction.upper()
    if text == "LONG_WATCH":
        return 1
    if text == "SHORT_WATCH":
        return -1
    return 0


def _has_duplicate_minutes(bars: Sequence[Candle]) -> bool:
    seen: set[datetime] = set()
    for bar in bars:
        if bar.ts_utc in seen:
            return True
        seen.add(bar.ts_utc)
    return False


def _session_grid_defects(
    bars: Sequence[Candle],
    trading_day: date,
    expected_minutes: int,
) -> list[str]:
    if not bars:
        return []
    defects: list[str] = []
    expected_first = datetime.combine(trading_day, time(9, 30), tzinfo=ET)
    expected_last = expected_first + timedelta(minutes=expected_minutes - 1)
    if bars[0].ts_exchange != expected_first:
        defects.append("REGULAR_SESSION_FIRST_MINUTE_MISMATCH")
    if bars[-1].ts_exchange != expected_last:
        defects.append("REGULAR_SESSION_LAST_MINUTE_MISMATCH")
    previous = bars[0].ts_exchange
    for current_bar in bars[1:]:
        current = current_bar.ts_exchange
        if current <= previous:
            defects.append("REGULAR_SESSION_NOT_STRICTLY_INCREASING")
            break
        if current - previous != timedelta(minutes=1):
            defects.append("REGULAR_SESSION_MINUTE_GAP")
            break
        previous = current
    return defects


def _compound(returns: Sequence[float | None]) -> float | None:
    values = [float(value) for value in returns if value is not None]
    if not values:
        return None
    total = 1.0
    for value in values:
        total *= 1.0 + value
    return total - 1.0


def _mean(values: Sequence[float] | Any) -> float:
    materialized = list(values)
    if not materialized:
        raise ValueError("cannot average empty values")
    return sum(materialized) / len(materialized)


def _require_str(value: Mapping[str, Any], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"missing required string: {key}")
    return raw


def _iso_z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.4f}%"


def _num(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.4f}"
