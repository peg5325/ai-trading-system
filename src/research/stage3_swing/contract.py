"""Frozen constants of PREREG_STAGE3_SWING_V1. Do not tune after seeing results."""
from __future__ import annotations

from datetime import date

PREREG_PATH = "docs/research/PREREG_STAGE3_SWING_V1.md"
DATA_END = date(2025, 7, 18)  # blind OOS (2025-07-21~) is never read
PREP_START = date(1993, 2, 1)  # indicator-prep slice start
TRADE_START = date(1994, 2, 1)
START_FIRST = (1994, 2)  # monthly account starts, inclusive
START_LAST = (2015, 7)
N_STARTS = 258
HORIZON_DAYS = 2520  # 10 years x 252 trading days
START_EQUITY = 2000.0
TARGET_EQUITY = 4000.0
STOP_FRACTION = 0.65  # stop at 0.65 x running peak (-35%)
P_STOP_MAX = 0.25

SPREAD = 0.0050  # s, borrowing spread per year
ER = {"sp_1x": 0.000945, "ndx_1x": 0.0020, "lev": 0.0095}
NDX_QQQ_ER = 0.0020  # added back to QQQ TR to form index TR r_u
QQQ_START = date(1999, 3, 10)
COST_ONE_WAY = 0.0015
COST_STRESS = 0.0025

CAL_MAX_ANN_DIFF = 0.015
CAL_MIN_CORR = 0.99

BOOT_REPS = 1000
BOOT_SEED = 20261002
BOOT_MEAN_BLOCK = 250
ALPHA = 0.05

ERAS = (
    ("1994-02-01", "2004-12-31"),
    ("2005-01-03", "2014-12-31"),
    ("2015-01-02", "2025-07-18"),
)

TAX_RATE = 0.22
TAX_DEDUCTION_USD = 1786.0

INDICES = ("sp", "ndx")
RULE_IDS = ("1", "2", "3", "3i", "4", "5", "5i", "6", "6i", "7", "8", "9", "10", "11", "12")
CANDIDATE_IDS = tuple(r for r in RULE_IDS if r != "1")

# product codes used in position arrays
CASH, X1, X2, X3, INV = 0, 1, 2, 3, 4
LEVERAGE = {X1: 1.0, X2: 2.0, X3: 3.0, INV: -1.0}

# (ETF, index, leverage)
CALIBRATION_PAIRS = (
    ("SPY", "sp", 1.0),
    ("QQQ", "ndx", 1.0),
    ("SSO", "sp", 2.0),
    ("UPRO", "sp", 3.0),
    ("SH", "sp", -1.0),
    ("QLD", "ndx", 2.0),
    ("TQQQ", "ndx", 3.0),
    ("PSQ", "ndx", -1.0),
)

YAHOO_SYMBOLS = ("^GSPC", "^NDX", "^SP500TR", "^VIX", "SPY", "QQQ", "SSO", "UPRO", "SH", "QLD", "TQQQ", "PSQ")
CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
FRENCH_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_Factors_daily_CSV.zip"
)
