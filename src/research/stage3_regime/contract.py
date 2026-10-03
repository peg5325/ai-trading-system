"""Frozen constants of PREREG_STAGE3_REGIME_V1. Do not tune after seeing results.

Costs, start equity and product definitions are reused from the stage-3 swing contract
(prereg section 1: product returns follow the v1 synthetic-leverage formula).
"""
from __future__ import annotations

from datetime import date

from src.research.stage3_swing import contract as SC

VERSION = "v1"
PREREG_PATH = "docs/research/PREREG_STAGE3_REGIME_V1.md"
SELECTION_PATH = "docs/research/SELECTION_STAGE3_REGIME_V1.json"
RESULT_NAME = "RESULT_STAGE3_REGIME_V1"
CODE_PATHS = ("src/research/stage3_regime", "src/research/stage3_swing", "scripts/run_stage3_regime.py")

EXPLORE_START = date(2006, 7, 3)
EXPLORE_END = date(2019, 12, 31)  # explore never reads a row after this date
CONFIRM_START = date(2020, 1, 2)
CONFIRM_END = date(2025, 7, 18)  # == swing DATA_END; blind OOS (2025-07-21~) is never read

# regime thresholds (prereg section 2)
SMA_N = 200
VIX_LEVEL = 20.0  # A: VIX < 20, B: VIX >= 20
RSI_N = 2
RSI_LEVEL = 10.0  # C: RSI(2) >= 10, D: RSI(2) < 10
REGIMES = ("A", "B", "C", "D")
UNDEFINED = -1  # indicator warm-up not complete

# exposure choices (prereg section 3); this order also defines the final tie-break
EXPOSURES = (0, 1, 2, 3, -1)
EXPOSURE_PRODUCT = {0: SC.CASH, 1: SC.X1, 2: SC.X2, 3: SC.X3, -1: SC.INV}
MDD_LIMIT = 0.35  # selection and confirm criterion 2: max drawdown strictly below 35%
TIE_ATOL = 1e-12  # net log growth within this distance counts as a tie

COST_ONE_WAY = SC.COST_ONE_WAY
COST_STRESS = SC.COST_STRESS
START_EQUITY = SC.START_EQUITY
TARGET_EQUITY = SC.TARGET_EQUITY
INDICES = SC.INDICES
