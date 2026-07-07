# AI Decision Engine

## Role (MVP: entry filter only)
- Rule Engine 후보(BUY candidate)에 대해서만 호출.
- confidence + reason 반환. 주문을 직접 결정하지 않음.
- 지표 간 충돌 해석 / 비정형 정보(뉴스·섹터 분위기) 해석 / WAIT 사유 설명.
- 장 마감 후 매매 회고 및 전략 개선 제안 (비실시간).

## Authority
- CAN block: 신규 매수 진입, 추가 매수, 물타기 (confidence < threshold).
- CANNOT: 후보 없는 BUY 강제, Risk Manager 금지 주문 실행, 손절/청산 차단,
  일일 손실 한도 무시, 킬 스위치 무시.

## Output
decision (BUY / WAIT)
confidence_score
reason[]
risk_level
entry_quality

## Failure Policy (entry only)
- timeout / api_error / rate_limit / invalid_json / low_confidence -> WAIT.
- rate_limit 시 알림 발송.
- Exit 경로에는 절대 적용 안 함 (손절/청산은 코드가 수행).

## Determinism Guards
- temperature 낮게.
- JSON schema 강제.
- 동일 입력 캐싱.
- confidence threshold 적용.

## Cadence
- 5분봉 마감 시 호출. 응답 1~3초 허용 (다음 봉 시가 진입이므로 지연 흡수).
- 주기 내 무응답 -> 해당 사이클 WAIT.
