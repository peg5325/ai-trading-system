"""Exit Signal Generator — 청산 신호 생성 독립 모듈.

live/paper/backtest 가 동일한 청산 규칙(Stop Loss / Take Profit / Time Exit / EOD Flatten)을
공유하도록 하는 단일 소스. AI 는 청산에 관여하지 않는다(순수 결정론 코드).
"""
from src.exits.generator import (
    ExitConfig,
    ExitContext,
    ExitSignalGenerator,
    PositionView,
)

__all__ = [
    "ExitConfig",
    "ExitContext",
    "ExitSignalGenerator",
    "PositionView",
]
