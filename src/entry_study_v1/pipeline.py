"""Glue: load segments, check them against the fixed manifest, and turn mask days into trades (PREREG §3, §4, §7)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date

from src.entry_study_v1 import rules as RL
from src.entry_study_v1 import trades as TR
from src.entry_study_v1.bars import DataMismatch, InvalidRun
from src.entry_study_v1.config import Cell, hhmm
from src.entry_study_v1.data import DEV_SEGMENTS, Segment, SymbolData, load_symbol
from src.entry_study_v1.mask import pc_bar, prev_session, sigma_dates

UNCONDITIONAL_RULES = ("E1", "E2", "E4", "E5")  # E3 enters at a different minute every day


def load_dev_segments(db, symbols, *, purpose: str, segments: tuple[Segment, ...] = DEV_SEGMENTS
                      ) -> dict[str, dict[str, SymbolData]]:
    """{segment name: {symbol: SymbolData}} for the development data segments (upper bound enforced)."""
    return {seg.name: {s: load_symbol(db, s, seg.data_start, seg.data_end, purpose=purpose, dev=True)
                       for s in symbols} for seg in segments}


def verify_against_manifest(seg_data: dict[str, dict[str, SymbolData]], manifest: dict) -> None:
    """Refuse unless every loaded (symbol, segment) row hash and row count equal the fixed manifest."""
    for seg_name, syms in seg_data.items():
        for sym, sd in syms.items():
            want = manifest["rows"].get(sym, {}).get(seg_name)
            if want is None:
                raise DataMismatch(f"manifest has no entry for {sym} segment {seg_name}")
            if want["sha256"] != sd.row_sha256 or want["count"] != sd.row_count:
                raise DataMismatch(f"{sym} segment {seg_name}: DB rows differ from data_manifest "
                                   f"(count {sd.row_count} vs {want['count']}, sha256 {sd.row_sha256} vs "
                                   f"{want['sha256']})")


def by_t(bars) -> dict[int, object]:
    return {b.t: b for b in bars}


def make_ref_day(day: Date, seg: Segment, ref: SymbolData) -> RL.RefDay:
    """Reference inputs for a scored day. The mask guarantees PC bar and 14 sigma sessions exist."""
    prev = prev_session(day)
    pc = pc_bar(ref.regular.get(prev.isoformat(), []), prev)
    sig = sigma_dates(day, seg)
    if pc is None or sig is None:
        raise InvalidRun(f"{day}: PC bar or sigma sessions missing on a scored day")
    return RL.RefDay(day.isoformat(), by_t(ref.regular.get(day.isoformat(), [])),
                     tuple(ref.pre.get(day.isoformat(), [])), prev.isoformat(), pc,
                     tuple(by_t(ref.regular.get(d.isoformat(), [])) for d in sig))


@dataclass(frozen=True)
class TradeRec:
    k: int
    day: str
    seg: str
    event_day: bool
    entry_t: int
    res: dict  # hold name -> {"exit_t", "mae", "r": {cost name: r}}


def _rec(cell: Cell, entry_t: int, iso: str, seg_name: str, event: bool, etf_bars: dict) -> TradeRec:
    return TradeRec(cell.k, iso, seg_name, event, entry_t, TR.trade(cell, entry_t, etf_bars[cell.etf], iso))


def pair_trades(pair: str, cells: list[Cell], mask_pair: dict, seg_data: dict[str, dict[str, SymbolData]],
                segments: tuple[Segment, ...]) -> dict:
    """All trades of one pair's 10 cells over its scored days.

    Returns {"main": {k: [TradeRec]}, "e5_alt": {threshold: {k: [TradeRec]}},
             "unconditional": {k: [TradeRec]}} - the last for E1/E2/E4/E5 cells (same ETF, same minute, every day).
    """
    mine = [c for c in cells if c.pair == pair]
    by_key = {(c.rule, c.side): c for c in mine}
    ref_sym, long_sym, short_sym = mask_pair["ref"], mask_pair["long"], mask_pair["short"]
    main: dict[int, list[TradeRec]] = {c.k: [] for c in mine}
    uncond: dict[int, list[TradeRec]] = {c.k: [] for c in mine if c.rule in UNCONDITIONAL_RULES}
    e5p = by_key[("E5", "long")].params
    alt: dict[float, dict[int, list[TradeRec]]] = {
        thr: {by_key[("E5", s)].k: [] for s in ("long", "short")} for thr in e5p["sensitivity_thresholds"]}
    for seg in segments:
        m = mask_pair["segments"][seg.name]
        data = seg_data[seg.name]
        if {long_sym, short_sym, ref_sym} - set(data):
            raise InvalidRun(f"segment {seg.name} lacks symbols for {pair}")
        for iso in m["scored"]:
            day = Date.fromisoformat(iso)
            rd = make_ref_day(day, seg, data[ref_sym])
            event = iso in m["event_days"]
            etf_bars = {s: by_t(data[s].regular.get(iso, [])) for s in (long_sym, short_sym)}

            for rule in RL.RULE_FUNCS:
                params = by_key[(rule, "long")].params
                res = RL.evaluate(rule, rd, params)
                if res.signal is not None:
                    c = by_key[(rule, res.signal.side)]
                    main[c.k].append(_rec(c, res.signal.entry_t, iso, seg.name, event, etf_bars))
                if rule in UNCONDITIONAL_RULES:
                    for side in ("long", "short"):
                        c = by_key[(rule, side)]
                        uncond[c.k].append(_rec(c, hhmm(params["entry"]), iso, seg.name, event, etf_bars))
                if rule == "E5":
                    for thr, store in alt.items():
                        r5 = RL.e5(rd, params, threshold=thr)
                        if r5.signal is not None:
                            c = by_key[("E5", r5.signal.side)]
                            store[c.k].append(_rec(c, r5.signal.entry_t, iso, seg.name, event, etf_bars))
    return {"main": main, "e5_alt": alt, "unconditional": uncond}
