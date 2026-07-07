# Backtest Engine

Live 이전 필수. IndicatorEngine 은 live 와 100% 동일 모듈 공유.

## Execution Model (필수)
- slippage: 고정 bps 또는 변동성 기반.
- bar_execution: 신호 봉 종가 체결 금지. 다음 봉 시가 또는 보수적 체결가.
- partial_fill: NEW/PARTIALLY_FILLED/FILLED/CANCELED/REJECTED 상태 모델.
- fees_and_tax: 브로커별 수수료/세금 모델 분리.
- look_ahead_bias 방지: 해당 시점 이전 데이터만 전달, 이후 캔들은 검증에만.

## Validation Plan
- in-sample / out-of-sample 분리.
- walk-forward: 한 구간에서 파라미터 정하고 다음 구간에서 검증.
- minimum_trades: 100회 미만이면 결론 보류.

## Metrics
- CAGR / 총수익률, Sharpe, Sortino, MDD, Win rate, Profit factor,
  Avg win / Avg loss, Expectancy, Max consecutive losses,
  Exposure time, Slippage-adjusted return.

## Anti-overfitting
- 지표 조합 과다 금지.
- 파라미터 탐색 범위 제한.
- 테스트 기간 외 검증.
- 거래비용/슬리피지 포함.
- 유사 종목(TQQQ, TECL) 비교 검증.

## Acceptance Rule
단순 수익률만으로 합격 불가. MDD + 손익비 + 기대값 + 슬리피지 반영 성과를 함께 본다.
합격해야만 Paper Trading 진입.
