"""Arms, costs and the v1.3 sizing rule (PREREG_DAYTRADE_V1_3_SIZING §2).

Three arms are compared (§4, §5, §9):

- `v1.0+`    v1.0 sizing (0.5%, 35%/50%, 1.5% stop-distance refusal) + the §3 risk controls + finite-price checks.
- `v1.3-cap` refusal removed; 0.5%, 35%/50%; cash limit and HIGH entry budget (§2 steps 5-6).
- `v1.3`     refusal removed; 1%, 70%/100%; steps 5-6.

`V10_EXACT` is v1.0 itself (no step-7 lock, no account halt, no steps 5-6); it exists only so the simulator can be
checked against `src.daytrade_paper.simulate.run_day`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from src.daytrade_paper import rules as R

PREREG_PATH = "docs/research/PREREG_DAYTRADE_V1_3_SIZING.md"

ACCOUNT_HALT_FRAC = 0.35  # Charter §5: liquidation-value account <= high-water mark x (1 - 0.35)
HIGH_ENTRY_BUDGET = 0.50  # §2 step 6: entry budget in HIGH/UNKNOWN = equity x 50%


@dataclass(frozen=True)
class Costs:
    commission: float = R.COMMISSION  # each side, also the cash-limit rate c (§2 step 5)
    sec_fee: float = R.SEC_FEE  # sells only
    slippage: float = R.SLIPPAGE  # one way, in the fill price

    def scaled(self, k: float) -> Costs:
        return Costs(self.commission * k, self.sec_fee * k, self.slippage * k)


BASE_COSTS = Costs()

# Named cost models (Charter v2.6 §3, reporting labels only; the arithmetic above is unchanged).
# CM030: the research default (commission 0.10%/side, slippage 0.05%/side in the fill, SEC on sells); a research
# assumption, not a verified broker rate. CM020: same commission without slippage, a diagnostic that never decides.
COST_CONTRACT_VERSION = "cost_contract/v2 (Charter v2.6)"
COST_MODELS = {
    "CM030_v1": BASE_COSTS,
    "CM020_diag_v1": Costs(R.COMMISSION, R.SEC_FEE, 0.0),
}
DIAGNOSTIC_COST_MODELS = frozenset({"CM020_diag_v1"})


def cost_model_id(costs: Costs) -> str:
    """Registered id of `costs`, `<id>x<k>` for an exact scaled copy, else a CUSTOM label with the values."""
    for cid, c in COST_MODELS.items():
        if costs == c:
            return cid
        for k in (1.5, 2.0):
            if costs == c.scaled(k):
                return f"{cid}x{k}"
    return f"CUSTOM(c={costs.commission!r},f={costs.sec_fee!r},s={costs.slippage!r})"


@dataclass(frozen=True)
class Policy:
    name: str
    risk: float
    cap_single: float
    cap_same_dir: float
    max_stop_frac: float | None  # None: no stop-distance refusal (§2 change 1)
    cash_limits: bool  # §2 steps 5-6
    risk_controls: bool  # §3 step-7 daily lock and account halt (the 14:45 pre-entry check is v1.0 and always on)


V10_EXACT = Policy("v1.0", R.RISK_PER_TRADE, R.CAP_SINGLE, R.CAP_COMBINED_SAME_DIR, R.MAX_STOP_FRAC, False, False)
V10_PLUS = Policy("v1.0+", R.RISK_PER_TRADE, R.CAP_SINGLE, R.CAP_COMBINED_SAME_DIR, R.MAX_STOP_FRAC, False, True)
V13_CAP = Policy("v1.3-cap", R.RISK_PER_TRADE, R.CAP_SINGLE, R.CAP_COMBINED_SAME_DIR, None, True, True)
V13 = Policy("v1.3", 0.01, 0.70, 1.00, None, True, True)
ARMS = (V10_PLUS, V13_CAP, V13)


def with_stop_cap(policy: Policy, frac: float = R.MAX_STOP_FRAC) -> Policy:
    """§9 test ②: the same arm with the 1.5% refusal restored."""
    return replace(policy, name=f"{policy.name}+stopcap", max_stop_frac=frac)


def finite_positive(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def size(policy: Policy, costs: Costs, equity: float, stop_frac: float, high: bool, symbol: str,
         open_positions: list[dict]) -> dict:
    """§2 steps 1-7. `equity` = E_T, `open_positions` = trades still open at the T open (after step 2).

    Arithmetic for steps 3-4 follows `simulate.size_notional` and run_day's S5 room in the same order, so the v1.0
    arms give bit-identical notionals. Step 4 applies only when a same-direction position is open (v1.0 §6); with
    none open it could never bind (room >= single cap).
    """
    base = min(equity * policy.risk / stop_frac, equity * policy.cap_single)
    single_cap_binds = equity * policy.risk / stop_frac >= equity * policy.cap_single
    if high:
        base *= R.HIGH_SIZE_MULT
    limits = {"base": base}
    open_same = sum(p["notional"] for p in open_positions if p["symbol"] == symbol)
    if open_same > 0:
        cap = equity * policy.cap_same_dir
        limits["same_dir_room"] = cap * (R.HIGH_SIZE_MULT if high else 1.0) - open_same
    if policy.cash_limits:
        x = sum(p["notional"] for p in open_positions)
        c = sum(p["buy_commission"] for p in open_positions)
        limits["cash"] = (equity - x - c) / (1 + costs.commission)
        if high:
            limits["high_entry_budget"] = (equity * HIGH_ENTRY_BUDGET - x - c) / (1 + costs.commission)
    notional = min(limits.values())
    binding = [k for k, v in limits.items() if v == notional]
    return {"notional": max(notional, 0.0), "limits": limits, "binding": binding,
            "single_cap_binds": single_cap_binds}
