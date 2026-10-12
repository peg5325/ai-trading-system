"""Exit overlay on the entry study's E1..E5 trades (PREREG_DAYTRADE_REVERSAL_STUDY_V1 §7.5). Report only.

One combined exit rule: after the entry bar e, the first ref minute t <= 15:53 at which the T3-type OR the T4-type
reversal signal is true closes the trade at the open of bar t+1; no signal -> the 15:55 bar. Both on the same t is
reason "both" (same exit). Only ref bars with start <= t are read (VWAP is cumulated from 09:30, sigma uses the 14
prior sessions). No morning-trend or first-breakout condition is used.

  long ETF held (entry side "long"):    T3-type = run_bars bars (all >= e) with C < VWAP; T4-type = C(t) < L(t)
  inverse ETF held (entry side "short"): T3-type = run_bars bars with C > VWAP;            T4-type = C(t) > U(t)
"""
from __future__ import annotations

from dataclasses import dataclass

from src.entry_study_v1.config import hhmm
from src.entry_study_v1.rules import RefDay, sigma_at
from src.reversal_study_v1.rules import bands, vwap_iter

LAST_SIGNAL_T = hhmm("15:53")
FORCED_EXIT_T = hhmm("15:55")


@dataclass(frozen=True)
class OverlayExit:
    exit_t: int  # start minute of the ETF bar whose open is the exit fill
    reason: str  # "T3" | "T4" | "both" | "close"


def overlay_exit(day: RefDay, side: str, entry_t: int, t3_params: dict, t4_params: dict) -> OverlayExit:
    run = t3_params["run_bars"]
    n, mult = t4_params["sigma_sessions"], t4_params["multiplier"]
    t4_first, t4_last = hhmm(t4_params["first_check_bar_start"]), hhmm(t4_params["last_check_bar_start"])
    long_side = side == "long"
    hist: list[tuple[int, float, float | None]] = []
    for t, v in vwap_iter(day.bars):
        if t > LAST_SIGNAL_T:
            break
        c = day.bars[t].close
        hist.append((t, c, v))
        if t < entry_t:
            continue
        tail = hist[-run:]
        t3_hit = (len(tail) == run and tail[0][0] >= entry_t
                  and all(vv is not None and (cc < vv if long_side else cc > vv) for _, cc, vv in tail))
        t4_hit = False
        if max(entry_t, t4_first) <= t <= t4_last:
            sig = sigma_at(day.sigma_days, t, n)
            if sig is not None:
                upper, lower = bands(day, sig, mult)
                t4_hit = c < lower if long_side else c > upper
        if t3_hit or t4_hit:
            return OverlayExit(t + 1, "both" if t3_hit and t4_hit else ("T3" if t3_hit else "T4"))
    return OverlayExit(FORCED_EXIT_T, "close")
