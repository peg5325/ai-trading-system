# Risk Manager

리스크 관리는 AI 보다 항상 우선하며, 청산 경로는 AI 와 완전히 독립된 순수 코드다.
다종목이므로 **종목별(per-symbol)** 과 **포트폴리오(portfolio)** 두 층에서 관리한다.

## Position Sizing (leverage-adjusted, per-symbol)
- 종목별 leverage_factor 로 실질 노출 관리 (SOXL=3, 일반주=1, 2× ETF=2 ...).
- effective_exposure = position_notional * leverage_factor.
- 예: 계좌 명목 10% 를 SOXL 매수 -> 실질 방향 노출 ~30%.
- 초기 1회 진입: 계좌 명목 3~5% 부터.
- 단일 종목 실질 노출: 15~30% 이내.

## Portfolio Limits (신규, 종목별 위에)
- 동시 포지션 수 상한 N.
- 전체 자동매매 실질 노출 합계: 50% 이하.
- 섹터 집중도 한도 (한 섹터 과다 편중 방지).
- 상관관계 고려 (고상관 종목 동시 과다 진입 억제).
- 포트폴리오 일일 손실 / 드로다운 서킷브레이커 (계좌 전체 기준).
- 개별 종목이 통과해도 포트폴리오 한도 초과 시 신규 진입 거부.

## Exit Triggers (pure code, AI-independent)
per-symbol:
- stop_loss: 종목별 -2 ~ -5% (전략별 설정)
- consecutive_losses: 연속 3회 손실 시 해당 종목 신규 진입 중단

portfolio (계좌 전체):
- daily_loss (soft): -2% 도달 시 신규 진입 중단
- daily_loss (hard): -3% 도달 시 Kill Switch
- drawdown_circuit_breaker: 최근 고점 대비 -5 ~ -10% 자동매매 중단

## Kill Switch
- 트리거: hard daily loss, 잔고 불일치, 주문 API 연속 실패/비정상, 레버리지 종목 청산 실패.
- 발동 시: 신규 진입 즉시 금지. 기존 포지션은 stop-loss/take-profit 코드 경로 유지.
- 극단 상황(잔고 불일치, hard 트리거): 강제 청산 옵션.
- 재개: 자동 재개 금지. postmortem(원인/주문로그/포지션/API 상태) 후 운영자 수동 승인.

## Overnight Policy (leverage-tiered)
- leverage_factor == 1 (일반주): 오버나잇 보유 허용. 갭 리스크 반영해 사이징.
- leverage_factor > 1 (레버리지 ETF): 오버나잇 미보유. 갭 + 구조적 감쇠 리스크.
- 레버리지 종목은 마지막 허용 세션(애프터마켓) 종료 전 flatten → 진짜 오버나잇 갭 회피.

## Leverage Flatten (레버리지 종목 당일 청산 메커니즘)
- 대상: leverage_factor > 1 종목만. 세션 종료 5분 전(Flatten Window)부터 신규 진입 금지 + 청산.
- 주문 유형: 지정가 우선, 유동성/시간대 따라 시장가.
- AI 완전 독립.
- 청산 실패 시: 즉시 재시도 -> 재시도 실패 시 긴급 알림 -> 강제 오버나잇 발생 시 Kill Switch,
  다음 거래일 자동 재개 금지.

## Extended-hours Guards (프리/애프터마켓)
- 유동성 얇음 → 지정가 우선(시장가 지양/브로커 미지원 가능), 최소 거래량/스프레드 게이트.
- 연장시간 진입은 사이즈 축소 + 슬리피지 보수적 가정.
- 유동성 게이트 미달 종목은 연장시간 진입 금지.

## Principle
생존이 수익보다 우선. 리스크는 종목의 레버리지에 비례한다.
오버나잇 갭·청산실패는 예외 상황 -> 반드시 기록 + 알림.
