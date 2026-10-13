"""Metrics, Markdown tables and the JSON record (spec §4). Numbers are rounded only in the Markdown output."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date as Date
from pathlib import Path

from .strategy import BacktestResult, Trade


def drawdown(start: float, curve: list[float]) -> tuple[float, float, int]:
    """(max drawdown USD, max drawdown fraction, longest run of trading days below the running peak) on the
    end-of-day equity curve, with `start` as the peak before the first day."""
    peak = start
    mdd = mdd_pct = 0.0
    run = longest = 0
    for e in curve:
        if e >= peak:
            peak = e
            run = 0
        else:
            run += 1
            longest = max(longest, run)
            mdd = max(mdd, peak - e)
            mdd_pct = max(mdd_pct, (peak - e) / peak if peak > 0 else 0.0)
    return mdd, mdd_pct, longest


def trade_stats(trades: list[Trade], key: str = "net_pnl") -> dict:
    pnl = [getattr(t, key) for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "trades": len(pnl),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(pnl) if pnl else None,
        "net_pnl": sum(pnl),
        "gross_profit": gross_win,
        "gross_loss": gross_loss,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "avg_win": gross_win / len(wins) if wins else None,
        "avg_loss": -gross_loss / len(losses) if losses else None,
    }


def cagr(start: float, final: float, first: str, last: str) -> float | None:
    years = ((Date.fromisoformat(last) - Date.fromisoformat(first)).days + 1) / 365.25
    if years <= 0 or start <= 0 or final <= 0:
        return None
    return (final / start) ** (1 / years) - 1


def filter_counts(r: BacktestResult) -> dict:
    """V1 filter counters (spec V1 §4): symbol-days removed before the watchlist (per symbol and total) and
    signals / beyond-ORB closes rejected inside the day loop (watchlist symbols only)."""
    fsd = r.filter_symbol_days or {}
    out: dict = {}
    for k in ("daily_not_ready", "min_range", "direction_equal"):
        per = fsd.get(k, {})
        out[f"{k}_symbol_days"] = {"total": sum(per.values()), "per_symbol": dict(sorted(per.items()))}
    out["breakout_volume_rejected_closes"] = r.skips.get("breakout_volume", 0)
    out["direction_filter_signals"] = r.skips.get("direction_filter", 0)
    out["short_disabled_signals"] = r.skips.get("short_disabled", 0)
    return out


def sniper_metrics(r: BacktestResult) -> dict:
    """V4 (spec V4 §6): pick counts per symbol, pick days, split breaks (only symbols that had one)."""
    sn = r.sniper
    return {
        "pick_days": len(sn["picks"]),
        "per_symbol": sn["per_symbol"],
        "split_breaks": {s: v for s, v in sn["split_breaks"].items() if v},
    }


class BacktestReporter:
    def __init__(self, result: BacktestResult) -> None:
        self.r = result

    def metrics(self) -> dict:
        r = self.r
        days = r.days
        if not days:
            return {"period": None, "total_trading_days": 0, "trade_count": 0}
        first, last = days[0].day, days[-1].day
        curve = [d.end_equity for d in days]
        ref_curve = [d.ref_end_equity for d in days]
        final, ref_final = curve[-1], ref_curve[-1]
        mdd, mdd_pct, dd_days = drawdown(r.start_equity, curve)
        rmdd, rmdd_pct, rdd_days = drawdown(r.start_equity, ref_curve)
        status: dict[str, int] = {}
        for d in days:
            status[d.status] = status.get(d.status, 0) + 1
        trade_days = sum(1 for d in days if d.trades > 0)
        reasons: dict[str, int] = {}
        for t in r.trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        per_symbol = {s: trade_stats([t for t in r.trades if t.symbol == s])
                      for s in sorted({t.symbol for t in r.trades})}
        sides = {"long": trade_stats([t for t in r.trades if t.side > 0]),
                 "short": trade_stats([t for t in r.trades if t.side < 0])}
        years = {}
        for y in sorted({d.day[:4] for d in days}):
            yd = [d for d in days if d.day[:4] == y]
            yt = [t for t in r.trades if t.day[:4] == y]
            s0, s1 = yd[0].start_equity, yd[-1].end_equity
            prev_ref = r.start_equity
            for d in days:
                if d.day[:4] < y:
                    prev_ref = d.ref_end_equity
            years[y] = {
                "first_day": yd[0].day, "last_day": yd[-1].day, "trading_days": len(yd),
                "trade_days": sum(1 for d in yd if d.trades > 0),
                "start_equity": s0, "end_equity": s1, "return": s1 / s0 - 1 if s0 > 0 else None,
                "ref_start_equity": prev_ref, "ref_end_equity": yd[-1].ref_end_equity,
                "ref_return": yd[-1].ref_end_equity / prev_ref - 1 if prev_ref > 0 else None,
                **{f"trades_{k}": v for k, v in trade_stats(yt).items()},
            }
        return {
            "period": {"first_day": first, "last_day": last},
            "total_trading_days": len(days),
            "trade_days": trade_days,
            "no_trade_days": len(days) - trade_days,
            "day_status": status,
            "start_equity": r.start_equity,
            "final_equity": final,
            "cumulative_return": final / r.start_equity - 1,
            "cagr": cagr(r.start_equity, final, first, last),
            "mdd_usd": mdd,
            "mdd_pct": mdd_pct,
            "longest_drawdown_trading_days": dd_days,
            "trade_count": len(r.trades),
            **{f"trade_{k}": v for k, v in trade_stats(r.trades).items()},
            "tp1_hits": sum(1 for t in r.trades if t.tp1),
            "per_symbol": per_symbol,
            "long_short": sides,
            "exit_reasons": dict(sorted(reasons.items())),
            "qqq_buy_hold": r.qqq_buy_hold,
            "reference_cost": {
                "rule": "0.10% of notional per side, no slippage, same trades",
                "final_equity": ref_final,
                "cumulative_return": ref_final / r.start_equity - 1,
                "cagr": cagr(r.start_equity, ref_final, first, last),
                "mdd_usd": rmdd, "mdd_pct": rmdd_pct, "longest_drawdown_trading_days": rdd_days,
                **trade_stats(r.trades, "ref_pnl"),
            },
            "per_year": years,
            "excluded_symbol_days": r.excluded_symbol_days,
            "excluded_symbol_days_total": sum(r.excluded_symbol_days.values()),
            "not_ready_symbol_days": r.not_ready_symbol_days,
            "signal_skips": dict(sorted(r.skips.items())),
            **({"filter_counts": filter_counts(r)} if r.filter_symbol_days is not None else {}),
            **({"sniper": sniper_metrics(r)} if r.sniper is not None else {}),
        }

    def to_json(self, path: Path, provenance: dict) -> dict:
        doc = {
            "status": "exploratory, not a decision basis (spec §0)",
            "provenance": provenance,
            "metrics": self.metrics(),
            "days": [asdict(d) for d in self.r.days],
            "trades": [asdict(t) for t in self.r.trades],
        }
        if self.r.sniper is not None:
            doc["sniper_picks"] = self.r.sniper["picks"]
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        return doc

    def markdown(self) -> str:
        m = self.metrics()
        if not m.get("period"):
            return "No scored days.\n"
        f2 = _fmt
        out = ["## Summary", "", "| item | value |", "|---|---|"]
        rows = [
            ("period", f"{m['period']['first_day']} ~ {m['period']['last_day']}"),
            ("total trading days", m["total_trading_days"]),
            ("trade days", m["trade_days"]),
            ("no-trade days", m["no_trade_days"]),
            ("day status", ", ".join(f"{k} {v}" for k, v in sorted(m["day_status"].items()))),
            ("start equity (USD)", f2(m["start_equity"])),
            ("final equity (USD)", f2(m["final_equity"])),
            ("cumulative return", _pct(m["cumulative_return"])),
            ("CAGR", _pct(m["cagr"])),
            ("MDD (USD)", f2(m["mdd_usd"])),
            ("MDD (%)", _pct(m["mdd_pct"])),
            ("longest drawdown (trading days)", m["longest_drawdown_trading_days"]),
            ("trades", m["trade_count"]),
            ("win rate", _pct(m["trade_win_rate"])),
            ("profit factor", f2(m["trade_profit_factor"])),
            ("avg win (USD)", f2(m["trade_avg_win"])),
            ("avg loss (USD)", f2(m["trade_avg_loss"])),
            ("TP1 hits", m["tp1_hits"]),
            ("excluded symbol-days (incomplete)", m["excluded_symbol_days_total"]),
        ]
        fc = m.get("filter_counts")
        if fc:
            rows += [
                ("V1 excluded symbol-days: daily not ready", fc["daily_not_ready_symbol_days"]["total"]),
                ("V1 excluded symbol-days: min range", fc["min_range_symbol_days"]["total"]),
                ("V1 excluded symbol-days: prior close = daily EMA20", fc["direction_equal_symbol_days"]["total"]),
                ("V1 rejected breakout closes (volume)", fc["breakout_volume_rejected_closes"]),
                ("V1 signals dropped: direction filter", fc["direction_filter_signals"]),
                ("V1 signals dropped: shorts disabled", fc["short_disabled_signals"]),
            ]
        out += [f"| {k} | {v} |" for k, v in rows]
        out += ["", "## By symbol", "", "| symbol | trades | win rate | net P&L (USD) | PF |", "|---|---|---|---|---|"]
        for s, st in m["per_symbol"].items():
            out.append(f"| {s} | {st['trades']} | {_pct(st['win_rate'])} | {f2(st['net_pnl'])} | "
                       f"{f2(st['profit_factor'])} |")
        sn = m.get("sniper")
        if sn:
            out += ["", "## V4 picks by symbol", "",
                    ("| symbol | days passing screener | picked (RVOL #1) | picked but min range failed | trades | "
                     "net P&L (USD) |"), "|---|---|---|---|---|---|"]
            for s, c in sn["per_symbol"].items():
                st = m["per_symbol"].get(s, {"trades": 0, "net_pnl": 0.0})
                out.append(f"| {s} | {c['passing']} | {c['picked']} | {c['min_range_after_pick']} | {st['trades']} | "
                           f"{f2(st['net_pnl'])} |")
            out.append(f"\nsplit breaks (|gap| >= 30%): {sn['split_breaks'] or 'none'}")
        out += ["", "## Long / short", "", "| side | trades | win rate | net P&L (USD) | PF |", "|---|---|---|---|---|"]
        for s, st in m["long_short"].items():
            out.append(f"| {s} | {st['trades']} | {_pct(st['win_rate'])} | {f2(st['net_pnl'])} | "
                       f"{f2(st['profit_factor'])} |")
        out += ["", "## Exit reasons (final leg)", "", "| reason | count |", "|---|---|"]
        out += [f"| {k} | {v} |" for k, v in m["exit_reasons"].items()]
        qb = m["qqq_buy_hold"]
        rc = m["reference_cost"]
        out += ["", "## Comparison (report only, not a decision)", "", "| item | value |", "|---|---|"]
        if qb:
            out.append(f"| QQQ 1x buy & hold {qb['first_day']} open ~ {qb['last_day']} close | {_pct(qb['return'])} |")
        else:
            out.append("| QQQ 1x buy & hold | n/a |")
        out += [
            f"| reference cost (0.10%/side, no slippage) final equity (USD) | {f2(rc['final_equity'])} |",
            f"| reference cost cumulative return | {_pct(rc['cumulative_return'])} |",
            f"| reference cost MDD (%) | {_pct(rc['mdd_pct'])} |",
            f"| reference cost PF | {f2(rc['profit_factor'])} |",
        ]
        out += ["", "## By year", "",
                "| year | days | trade days | trades | win rate | net P&L (USD) | return | ref return |",
                "|---|---|---|---|---|---|---|---|"]
        for y, v in m["per_year"].items():
            out.append(f"| {y} | {v['trading_days']} | {v['trade_days']} | {v['trades_trades']} | "
                       f"{_pct(v['trades_win_rate'])} | {f2(v['trades_net_pnl'])} | {_pct(v['return'])} | "
                       f"{_pct(v['ref_return'])} |")
        return "\n".join(out) + "\n"


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.2f}%"
