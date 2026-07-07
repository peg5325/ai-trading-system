"""ExecutionModel — 체결 모델 (slippage + fee).

partial fill 은 MVP 에서 full fill 로 단순화하되, 이 클래스로 분리해 추후 확장 가능하게 둔다.
(예: 유동성 기반 부분체결은 execute() 를 오버라이드.)
"""
from __future__ import annotations

from dataclasses import dataclass

from src.risk.models import BUY


@dataclass(frozen=True)
class Fill:
    quantity: float
    price: float
    fee: float


@dataclass(frozen=True)
class ExecutionModel:
    slippage_bps: float = 5.0   # 체결 슬리피지 (bps)
    fee_bps: float = 2.0        # 수수료 (bps)

    def fill_price(self, ref_price: float, side: str) -> float:
        slip = ref_price * (self.slippage_bps / 10_000.0)
        return ref_price + slip if side == BUY else ref_price - slip

    def fee(self, notional: float) -> float:
        return abs(notional) * (self.fee_bps / 10_000.0)

    def execute(self, requested_qty: float, ref_price: float, side: str) -> Fill:
        """MVP: 요청 수량 전량 체결. 확장 시 여기서 부분체결 반영."""
        price = self.fill_price(ref_price, side)
        filled = requested_qty
        return Fill(filled, price, self.fee(filled * price))
