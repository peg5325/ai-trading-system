"""Report-only figures (PREREG_DAYTRADE_V1_3_SIZING §4 "보고", §5). None of these feed the verdict."""
from __future__ import annotations

from src.daytrade_v13.day import STOP_REASONS
from src.daytrade_v13.stats import MIN_RUN, runs_from_mask

MORNING = ("S1", "S2", "S3")
TOP_DAYS = 5


def _morning(day: dict) -> dict | None:
    return next((t for t in day["trades"] if t["kind"] in MORNING), None)


def _s5(day: dict) -> dict | None:
    return next((t for t in day["trades"] if t["kind"] == "S5"), None)


def _aftermath_key(name: str, a: dict, b: dict) -> str:
    """segment / S5 kind of difference / S5 direction for a nonzero-d day without an added morning trade."""
    sa, sb = _s5(a), _s5(b)
    if sa is None and sb is None:
        return f"{name}/no_S5"
    if sa is not None and sb is not None:
        return f"{name}/S5_both/{sa['symbol']}"
    return f"{name}/S5_only_v1.3-cap/{sa['symbol']}" if sa is not None else f"{name}/S5_only_v1.0+/{sb['symbol']}"


def decompose_d(cap: dict, plus: dict, d: dict[str, dict[str, float]], mask: dict) -> dict:
    """§4: d split into days with a morning trade v1.0+ did not take (by signal, direction, segment) and the
    nonzero-d rest (S5 / lock / equity aftermath, by segment, S5 presence in each arm and S5 direction),
    nonzero-d days, inference-population days, top-day concentration."""
    added: dict[str, dict] = {}
    after: dict[str, dict] = {}
    nonzero = 0
    all_d: list[float] = []
    for name, acc in cap["segments"].items():
        for a, b in zip(acc["days"], plus["segments"][name]["days"], strict=True):
            x = d[name][a["date"]]
            all_d.append(x)
            nonzero += x != 0
            ma, mb = _morning(a), _morning(b)
            if ma is not None and mb is None:
                key = f"{name}/{ma['kind']}/{ma['symbol']}"
                g = added.setdefault(key, {"days": 0, "d_sum": 0.0})
                g["days"] += 1
                g["d_sum"] += x
            elif x != 0:
                g = after.setdefault(_aftermath_key(name, a, b), {"days": 0, "d_sum": 0.0})
                g["days"] += 1
                g["d_sum"] += x
    inference = sum(len(r) for name in d for r in runs_from_mask(mask["segments"][name]) if len(r) > MIN_RUN)
    pos = sorted((x for x in all_d if x > 0), reverse=True)
    absd = sorted((abs(x) for x in all_d), reverse=True)
    return {"added_morning": dict(sorted(added.items())), "aftermath_nonzero": dict(sorted(after.items())),
            "nonzero_d_days": nonzero, "days": len(all_d), "inference_days": inference,
            "top_days_share_of_positive_d": (sum(pos[:TOP_DAYS]) / sum(pos)) if pos else None,
            "top_days_share_of_abs_d": (sum(absd[:TOP_DAYS]) / sum(absd)) if sum(absd) else None}


def arm_summary(arm: dict) -> dict:
    """§5 figures for one arm: G, D, worst day, locks and overshoot, account halt, longest run of stop exits,
    planned (stop-price) vs actual (gap + costs) loss on stop exits."""
    out = {"G": arm["G"], "D": arm["D"], "segments": {}}
    for name, acc in arm["segments"].items():
        worst = min(acc["days"], key=lambda x: x["pnl"]["day_return"], default=None)
        locks = [x for x in acc["days"] if x["lock"] is not None]
        streak = best = 0
        planned = actual = 0.0
        stops = 0
        for day in acc["days"]:
            for tr in day["trades"]:
                if tr["exit_reason"] in STOP_REASONS:
                    streak += 1
                    best = max(best, streak)
                    stops += 1
                    planned += tr["shares"] * (tr["entry_fill"] - tr["stop"])
                    actual += -tr["pnl"]
                else:
                    streak = 0
        out["segments"][name] = {
            "R": acc["R"], "D": acc["D"],
            "worst_day": worst and {"date": worst["date"], "day_return": worst["pnl"]["day_return"]},
            "lock_days": len(locks),
            "lock_by_source": {s: sum(1 for x in locks if x["lock"]["source"] == s) for s in ("pre_entry", "close")},
            "max_overshoot_worst": max((x["lock"]["overshoot_worst"] for x in locks), default=0.0),
            "max_overshoot_final": max((x["lock"]["overshoot_final"] for x in locks), default=0.0),
            "account_halt": acc["account_halt"], "max_consecutive_stops": best, "stop_exits": stops,
            "planned_stop_loss": planned, "actual_stop_loss": actual,
            "blocked_entries": sum(len(x["blocked_entries"]) for x in acc["days"]),
            "trades": sum(len(x["trades"]) for x in acc["days"]),
        }
    return out


def not_double(v13: dict, cap: dict, rel: float = 1e-9) -> dict:
    """§5: days where r(v1.3) != 2 x r(v1.3-cap), with the first applicable reason."""
    reasons: dict[str, int] = {}
    days: list[dict] = []
    for name, acc in v13["segments"].items():
        for a, b in zip(acc["days"], cap["segments"][name]["days"], strict=True):
            ra, rb = a["pnl"]["day_return"], b["pnl"]["day_return"]
            if abs(ra - 2 * rb) <= rel * max(abs(ra), abs(2 * rb), 1e-12):
                continue
            ka = [(t["kind"], t["symbol"]) for t in a["trades"]]
            kb = [(t["kind"], t["symbol"]) for t in b["trades"]]
            if ka != kb:
                why = "trades_differ(lock/halt/size<=0)"
            elif any(set(t["sizing"]["binding"]) & {"cash", "high_entry_budget"} for t in a["trades"]):
                why = "cash_or_entry_budget_binds_at_1pct"
            elif any(t["sizing_equity"] != a["equity_start"] for t in a["trades"]):
                why = "sizing_equity_includes_realized_pnl"
            else:
                why = "other"
            reasons[why] = reasons.get(why, 0) + 1
            days.append({"segment": name, "date": a["date"], "why": why})
    return {"days": len(days), "by_reason": reasons, "list": days}
