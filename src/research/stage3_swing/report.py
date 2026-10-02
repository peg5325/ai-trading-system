"""Result serialisation: JSON + Markdown."""
from __future__ import annotations

import json
from pathlib import Path

from . import contract as C


def write_json(result: dict, path: Path) -> None:
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


def _pct(x) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def render_markdown(result: dict) -> str:
    prov = result["provenance"]
    lines = [
        "# RESULT stage 3 swing v1",
        "",
        f"- status: **{result['status']}**",
        f"- prereg blob SHA: `{prov['prereg']['git_blob_sha']}`",
        f"- code commit: `{prov['code_commit']}` (dirty: {prov['code_dirty']})",
        (
            f"- data: {result['data']['first_row']} .. {result['data']['last_row']} "
            f"({result['data']['rows']} rows)"
        ),
        "",
        "## Raw file SHA-256",
        "",
    ]
    lines += [f"- `{name}`: `{digest}`" for name, digest in sorted(prov["raw_sha256"].items())]
    cal = result["calibration"]
    lines += ["", f"## Calibration gate: {'PASS' if cal['pass'] else 'CALIBRATION_FAIL'}", "",
              "| ETF | days | ann synthetic | ann actual | diff | corr | pass |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for etf, r in cal["etfs"].items():
        lines.append(
            f"| {etf} | {r['days']} | {_pct(r['ann_synthetic'])} | {_pct(r['ann_actual'])} | "
            f"{100 * r['ann_diff']:+.2f}pp | {r['corr']:.4f} | {r['pass']} |"
        )
    t = cal["sp500tr_vs_spy_plus_er"]
    lines += ["", f"^SP500TR vs SPY TR + ER: ann diff {100 * t['ann_diff']:+.2f}pp, corr {t['corr']:.4f}"]
    if result["status"] == "CALIBRATION_FAIL":
        lines += ["", "No strategy result was computed."]
        return "\n".join(lines) + "\n"

    cand = {(c["index"], c["rule"]): c for c in result["candidates"]}
    for index in C.INDICES:
        header = (
            "| rule | P_reach | P_stop | median days | TWR | MDD | orders | dP | p_holm | eras | "
            "stress c1+c2 | same-close P_reach | after-tax P_reach | verdict |"
        )
        lines += ["", f"## Index {index}", "", header, "|" + " --- |" * 14]
        for rule in C.RULE_IDS:
            r = result["per_index"][index][rule]
            fp = r["full_period"]
            c = cand.get((index, rule))
            med = r["median_days_to_reach"]
            lines.append(
                f"| {rule} | {_pct(r['p_reach'])} | {_pct(r['p_stop'])} | "
                f"{'-' if med is None else int(med)} | {_pct(fp['twr_annualized'])} | "
                f"{_pct(fp['max_drawdown'])} | {fp['orders']} | "
                + (
                    f"{100 * c['delta_p_reach']:+.1f}pp | {c['p_holm']:.4f} | "
                    f"{sum(c['eras_ge_1x'])}/3 | {c['stress_cost_c1_c2']} | "
                    f"{_pct(r['same_close']['p_reach'])} | {_pct(r['after_tax']['p_reach'])} | "
                    f"{'PASS' if c['PASS'] else 'fail'} |"
                    if c
                    else f"- | - | - | - | {_pct(r['same_close']['p_reach'])} | "
                    f"{_pct(r['after_tax']['p_reach'])} | baseline |"
                )
            )
        inv = {k: v["inverse"] for k, v in result["per_index"][index].items() if "inverse" in v}
        for rule, v in inv.items():
            lines.append(
                f"\nInverse holding {rule}: {v['inverse_days']} days, "
                f"log contribution {v['inverse_log_contribution']:+.3f}"
            )
    b = result["bootstrap"]
    lines += ["", "## Bootstrap", "",
              (
                  f"- stationary bootstrap, mean block {b['mean_block']}, reps {b['reps']}, "
                  f"seed {b['seed']}, Holm over {b['comparisons']} comparisons at alpha {b['alpha']}"
              ),
              "", f"## Verdict: {result['status']}", "",
              f"Passing: {', '.join(result['passing']) or 'none'}",
              "", "Passing does not authorise Paper or live orders (Charter section 4).",
              "", "## Limitations", ""]
    lines += [f"- {x}" for x in result["limitations"]]
    return "\n".join(lines) + "\n"
