"""Confirm-result Markdown (JSON is written with stage3_swing.report.write_json)."""
from __future__ import annotations

from . import contract as C


def _pct(x) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _stats_row(name: str, s: dict) -> str:
    reach = s["reached_4000_date"] if s["reached_4000"] else "no"
    return (
        f"| {name} | {s['final_usd']:,.0f} | {_pct(s['twr_annualized'])} | {_pct(s['max_drawdown'])} "
        f"| {s['net_log_growth']:.4f} | {reach} |"
    )


_STATS_HEAD = [
    "| path | final USD | TWR/yr | max DD | net log growth | USD 4,000 reached |",
    "| --- | --- | --- | --- | --- | --- |",
]


def render_markdown(result: dict) -> str:
    prov = result["provenance"]
    w = result["confirm_window"]
    lines = [
        f"# RESULT stage 3 regime {C.VERSION}",
        "",
        f"- verdicts: {', '.join(f'{k}: **{v}**' for k, v in result['verdicts'].items())}",
        f"- stop recommendation (criterion 3 fails for both indices): **{result['stop_recommendation']}**",
        f"- confirm window: {w['start']} .. {w['end']} (fresh USD {C.START_EQUITY:,.0f})",
        f"- prereg blob SHA: `{prov['prereg']['git_blob_sha']}`",
        f"- selection blob SHA: `{prov.get('selection_git_blob_sha')}`",
        f"- code commit: `{prov['code_commit']}` (dirty: {prov['code_dirty']})",
        f"- timing: {result['timing']}",
        "",
    ]
    for index, row in result["indices"].items():
        lines += [f"## {index}: {row['verdict']}", ""]
        ex = row["explore"]
        if row["verdict"] in ("PASS", "FAIL"):
            combo = ", ".join(f"{k}={v}" for k, v in row["combo"].items())
            cr = row["criteria"]
            lines += [
                f"- combo: {combo}",
                (
                    f"- criteria: c1 TWR > 1x **{cr['c1_twr_beats_1x']}**, c2 max DD < 35% "
                    f"**{cr['c2_mdd_below_35']}**, c3 some regime > 1x **{cr['c3_some_regime_beats_1x']}**"
                ),
                "",
            ]
            for name in ("base", "stress_cost", "same_close"):
                v = row[name]
                lines += [f"### {name} (mode {v['mode']}, cost {v['cost_one_way']:.2%})", "", *_STATS_HEAD,
                          _stats_row("strategy", v["strategy"]), _stats_row("1x", v["one_x"]), "",
                          "| regime | days | strategy log | 1x log | excess | beats 1x |",
                          "| --- | --- | --- | --- | --- | --- |"]
                for reg, r in v["by_regime"].items():
                    lines.append(
                        f"| {reg} | {r['days']} | {r['log_return']:.4f} | {r['log_return_1x']:.4f} "
                        f"| {r['excess_vs_1x']:+.4f} | {r['beats_1x']} |"
                    )
                lines.append("")
        lines += [f"### explore ({ex['status']}, feasible {ex['n_feasible']}/625)", "", *_STATS_HEAD]
        if ex.get("explore"):
            lines.append(_stats_row("strategy", ex["explore"]))
        lines += [_stats_row("1x", ex["one_x"]), ""]
    lines += ["## Raw file SHA-256", ""]
    lines += [f"- `{name}`: `{digest}`" for name, digest in sorted(prov["raw_sha256"].items())]
    lines += ["", "Passing is not order approval (Charter section 4). Blind OOS was not opened.", ""]
    return "\n".join(lines)
