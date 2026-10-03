"""Rule constants for CONTRACT_EXPLORATORY_PAPER_DAYTRADE_V1 (rules v1.0).

Single source in code for the contract thresholds. Changing any value means a new rules
version that takes effect from the next session (contract header); never edit in place.
"""
from __future__ import annotations

RULES_VERSION = "v1.0"
LABEL = "EXPLORATORY_PAPER"
CONTRACT_PATH = "docs/research/CONTRACT_EXPLORATORY_PAPER_DAYTRADE_V1.md"

SIGNAL = "QQQ"
LONG = "TQQQ"
SHORT = "SQQQ"
SYMBOLS = (SIGNAL, LONG, SHORT)

START_EQUITY = 2000.0


def hm(hour: int, minute: int) -> int:
    """ET wall-clock minute of day. A 1m bar labelled t covers [t, t+1)."""
    return hour * 60 + minute


OPEN = hm(9, 30)
GAP_PRE_CUTOFF = hm(9, 25)  # last PRE bar strictly before 09:25
DECISION = hm(9, 45)  # S1/S2 decision; opening window = [09:30, decision)
DECISION_INDICATOR_DAY = hm(10, 15)  # CPI/NFP/PCE days: S1/S2/S3 decided at 10:15
S3_DECISION = hm(10, 0)
S3_LOW_CUTOFF_OFFSET = 10  # session low must form before decision - 10 min (09:50)
S1S2_EXIT_CHECK_FROM = hm(10, 30)  # 5m buckets starting at/after 10:30
S3_EXIT_CHECK_FROM = hm(11, 0)
S5_DECISION = hm(14, 45)
FORCED_EXIT = hm(15, 55)
LAST_BAR = hm(15, 59)
EARLY_CLOSE_LAST_BAR = hm(12, 59)

GAP_STRONG = 0.003
S3_GAP = -0.010
RVOL_MIN = 1.2
RVOL_LOOKBACK = 20
RANGE_ATR_MULT = 0.4
ATR_DAYS = 14
SMA_DAYS = 50
S5_MOVE = 0.02
S3_STOP_BUFFER = 0.002
MAX_STOP_FRAC = 0.015
S5_STOP_FRAC = 0.015

VIX_HIGH = 30.0
VIX9D_RATIO_HIGH = 1.1
EXTERNAL_MAX_AGE_DAYS = 7

RISK_PER_TRADE = 0.005
CAP_SINGLE = 0.35
CAP_COMBINED_SAME_DIR = 0.50
HIGH_SIZE_MULT = 0.5

COMMISSION = 0.0010  # each side
SEC_FEE = 0.0000206  # sells only
SLIPPAGE = 0.0005  # one way, applied to the fill price

DAILY_LOSS_HALT = 0.03

FULL_SESSION_BARS = 390
EARLY_CLOSE_BARS = 210
