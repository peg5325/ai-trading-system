"""TossBrokerProvider — 실주문 skeleton (요구사항 3).

이 단계에서는 실제 주문 API 를 호출하지 않는다(요구사항 4).
Paper Trading 통과 후 별도 승인 단계에서 구현한다.

실사(FEASIBILITY_SPIKE)에서 확인된 실주문 엔드포인트(향후 구현 대상):
  POST /api/v1/orders   (orderType: LIMIT/MARKET, side: BUY/SELL)
  X-Tossinvest-Account 헤더 필요, OAuth2 client_credentials.
연장시간 수량주문 가능 여부는 실계좌 소액 테스트로 별도 확인 필요.
"""
from __future__ import annotations

from src.order.models import BrokerOrderResult, OrderRequest


class TossBrokerProvider:
    def __init__(self, *args, **kwargs):
        # TODO: TossClient 주입, 계좌 seq, 인증 등.
        pass

    def place_order(self, request: OrderRequest) -> BrokerOrderResult:
        # TODO(paper-trading 통과 + 승인 후):
        #   1) OrderRequest → Toss POST /api/v1/orders 페이로드 변환
        #   2) 연장시간이면 지정가 강제 / 유동성 게이트 확인
        #   3) 응답 → BrokerOrderResult (체결/부분체결/거부) 매핑
        raise NotImplementedError(
            "TossBrokerProvider 실주문은 Paper Trading 통과 후 승인 단계에서 구현합니다.")
