"""Replay a finished session through ShadowDay minute by minute and diff it against run_day."""
from __future__ import annotations

from src.daytrade_paper import rules as R
from src.daytrade_paper.core import Bar, PriorSession
from src.daytrade_paper.simulate import run_day
from src.daytrade_shadow.engine import ShadowDay

# data_quality differs by design: Paper checks the full session after the close, the shadow run only
# what exists at the decision time.
IGNORED = ("data_quality",)


def replay_day(day: str, regular: dict[str, list[Bar]], qqq_pre: list[Bar], prior: list[PriorSession],
               external: dict, events: dict, equity_start: float, *, prev_times: dict[str, set[int]],
               expected_prev: str, prev_early_close: bool = False) -> tuple[dict, list[dict]]:
    """Feed the session as a live process would see it: at minute now, bars t < now and bar now's open."""
    eng = ShadowDay(day, prior, external, events, equity_start, prev_times=prev_times,
                    expected_prev=expected_prev, prev_early_close=prev_early_close)
    ordered = {s: sorted(regular.get(s, []), key=lambda b: b.t) for s in R.SYMBOLS}
    by = {s: {b.t: b for b in ordered[s]} for s in R.SYMBOLS}
    complete: dict[str, list[Bar]] = {s: [] for s in R.SYMBOLS}
    idx = dict.fromkeys(R.SYMBOLS, 0)
    log: list[dict] = []
    for now in range(R.OPEN, R.LAST_BAR + 2):
        for s in R.SYMBOLS:
            while idx[s] < len(ordered[s]) and ordered[s][idx[s]].t < now:
                complete[s].append(ordered[s][idx[s]])
                idx[s] += 1
        opens = {s: (by[s][now].open if now in by[s] else None) for s in R.SYMBOLS}
        log.extend(eng.step(now, {s: list(v) for s, v in complete.items()}, opens, qqq_pre))
    return eng.finish(regular), log


def diff(a, b, path: str = "") -> list[str]:
    """Paths where a and b differ (exact equality, IGNORED keys skipped at the top level)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            if not path and k in IGNORED:
                continue
            if k not in a or k not in b:
                out.append(f"{path}/{k} (only in {'paper' if k in a else 'shadow'})")
            else:
                out.extend(diff(a[k], b[k], f"{path}/{k}"))
        return out
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return [f"{path} (length {len(a)} vs {len(b)})"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out.extend(diff(x, y, f"{path}[{i}]"))
        return out
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


def paper_vs_shadow(day: str, regular, qqq_pre, prior, external, events, equity_start, *, prev_times,
                    expected_prev, prev_early_close=False) -> dict:
    kw = {"prev_times": prev_times, "expected_prev": expected_prev, "prev_early_close": prev_early_close}
    paper = run_day(day, regular, qqq_pre, prior, external, events, equity_start, **kw)
    shadow, log = replay_day(day, regular, qqq_pre, prior, external, events, equity_start, **kw)
    return {"date": day, "paper_quality_ok": paper["data_quality"]["ok"],
            "shadow_quality_ok": shadow.get("data_quality", {}).get("ok"),
            "paper_situations": paper["situations"], "shadow_situations": shadow["situations"],
            "diffs": diff(paper, shadow), "events": log}
