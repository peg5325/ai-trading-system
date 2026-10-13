"""Engine configurations: V0 (spec V0, reproduced exactly), V1 / V1_LONGONLY (spec V1 §1~§3) and V2_QQQ (spec V2 §1).

V1 spec: docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V1.md. Every V1 change was chosen after seeing the V0
result on the same window (spec V1 §0): V1 results are in-sample post-hoc adjustments, not evidence of an edge.
V2 spec: docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V2.md (also post-hoc, spec V2 §0).
V3 spec: docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V3.md (V2_QQQ without the pre-market screener; post-hoc,
spec V3 §0).
V4 spec: docs/research/EXPLORE_ORB_VWAP_PULLBACK_2000USD_V4_SNIPER.md (seven large-cap stocks, the single highest-RVOL
qualifying stock per day; reopened by user instruction after the family close-out, post-hoc, spec V4 §0).
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace

from .universe import GAP_MAX, GAP_MIN, RVOL_MIN

GAP_BAND = (GAP_MIN, GAP_MAX)


@dataclass(frozen=True)
class EngineConfig:
    name: str
    universe: tuple[str, ...]
    max_price: float | None  # skip signals whose entry raw price is above this (None: no skip)
    fractional: bool  # False: integer shares (floor); True: shares rounded to 4 dp (spec V1 §3.4)
    direction_filter: bool  # daily EMA20 direction filter (spec V1 §3.2)
    breakout_volume_mult: float | None  # breakout bar volume >= mult * (OR 15-min volume / 15) (spec V1 §3.3)
    min_range_atr_mult: float | None  # exclude when ORB range < mult * prior daily ATR14 (spec V1 §3.3)
    allow_short: bool
    # RS reference symbol for the watchlist ranking and the "reference incomplete -> whole day excluded" rule.
    # None (spec V2 §1.3): no reference is loaded or needed, RS is 0 for every symbol, and the scored days start on
    # the first day a traded symbol is ready.
    rs_reference: str | None = "SPY"
    # Pre-market screener (spec §3.1): RVOL >= rvol_min and gap_band[0] <= |gap| <= gap_band[1]. Plain class
    # attributes here, not dataclass fields, so the V0 ~ V2 presets keep the exact fields (and recorded provenance)
    # they were run with; EngineConfigV3 turns them into fields that can be switched off (spec V3 §1).
    rvol_min = RVOL_MIN
    gap_band = GAP_BAND
    # Signal bar start window end and last fill minute (V0 spec §3.2: 14:59 / 15:00). Class attributes for the same
    # reason; EngineConfigV4 makes them fields (spec V4 §4.2: 14:29 / 14:30).
    signal_last = 899
    last_fill = 900

    @property
    def daily_filters(self) -> bool:
        """True when the daily EMA20 / ATR14 must be ready for a symbol-day to be a candidate (spec V1 §3.1)."""
        return self.direction_filter or self.min_range_atr_mult is not None

    @property
    def has_v1_filters(self) -> bool:
        return (self.daily_filters or self.breakout_volume_mult is not None or not self.allow_short)


V0 = EngineConfig(
    name="V0",
    universe=("QQQ", "SPY", "IWM", "AMD", "AAPL", "AMZN", "PLTR"),
    max_price=250.0,
    fractional=False,
    direction_filter=False,
    breakout_volume_mult=None,
    min_range_atr_mult=None,
    allow_short=True,
)

V1 = EngineConfig(
    name="V1",
    universe=("QQQ", "SPY", "AMD", "AAPL", "AMZN", "PLTR"),
    max_price=None,
    fractional=True,
    direction_filter=True,
    breakout_volume_mult=1.3,
    min_range_atr_mult=0.15,
    allow_short=True,
)

V1_LONGONLY = replace(V1, name="V1_LONGONLY", allow_short=False)

V2_QQQ = replace(V1_LONGONLY, name="V2_QQQ", universe=("QQQ",), breakout_volume_mult=1.2, rs_reference=None)



@dataclass(frozen=True)
class EngineConfigV3(EngineConfig):
    """EngineConfig with switchable screener filters (spec V3 §1). None disables a filter; with `rvol_min` None
    the 20-session RVOL base is not needed for a symbol-day to be ready or for the scored start (spec V3 §2)."""

    rvol_min: float | None = RVOL_MIN
    gap_band: tuple[float, float] | None = GAP_BAND


def _v3(name: str, symbol: str) -> EngineConfigV3:
    base = {f.name: getattr(V2_QQQ, f.name) for f in fields(V2_QQQ)}
    return EngineConfigV3(**{**base, "name": name, "universe": (symbol,)}, rvol_min=None, gap_band=None)


V3_QQQ = _v3("V3_QQQ", "QQQ")
V3_TQQQ = _v3("V3_TQQQ", "TQQQ")

@dataclass(frozen=True)
class EngineConfigV4(EngineConfigV3):
    """Single-stock RVOL sniper (spec V4 §4): screener = prior close > daily EMA20, gap >= `gap_up_min` (signed, no
    cap), RVOL >= `rvol_min`; the one highest-RVOL stock is picked, then the minimum ORB range is checked on it only
    (failing it means no trade, no fallback). `split_gap`: |open / prior close - 1| at or above it is a data break
    (spec V4 §3)."""

    gap_up_min: float = 0.008
    signal_last: int = 869  # signal bar start 14:29
    last_fill: int = 870  # fill at 14:30 at the latest
    split_gap: float = 0.30


V4_RVOL_SNIPER = EngineConfigV4(
    **{**{f.name: getattr(V2_QQQ, f.name) for f in fields(V2_QQQ)}, "name": "V4_RVOL_SNIPER",
       "universe": ("NVDA", "TSLA", "AAPL", "AMD", "MSFT", "AMZN", "META")},
    rvol_min=1.5, gap_band=None)
V4_BENCHMARK = "QQQ"  # loaded for the 1x buy-and-hold comparison only, never a candidate (spec V4 §3)

CONFIGS = {c.name: c for c in (V0, V1, V1_LONGONLY)}
# Every preset the CLI accepts. CONFIGS stays the V0/V1 set it was when the V1 results were produced.
PRESETS = {**CONFIGS, V2_QQQ.name: V2_QQQ}
# PRESETS stays the V0 ~ V2 set; the CLI accepts ALL_PRESETS.
ALL_PRESETS = {**PRESETS, V3_QQQ.name: V3_QQQ, V3_TQQQ.name: V3_TQQQ}
# ALL_PRESETS stays the V0 ~ V3 set; the CLI accepts CLI_PRESETS.
CLI_PRESETS = {**ALL_PRESETS, V4_RVOL_SNIPER.name: V4_RVOL_SNIPER}
