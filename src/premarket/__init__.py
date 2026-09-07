"""Read-only premarket Daily Brief.

This package turns normalized point-in-time evidence into an auditable shadow
watchlist.  It deliberately has no broker, order, live-account, or network
integration.
"""

from src.premarket.planner import build_daily_brief

__all__ = ["build_daily_brief"]
