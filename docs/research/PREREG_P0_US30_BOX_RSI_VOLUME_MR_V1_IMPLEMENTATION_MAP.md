# 구현 매핑 — 박스권 RSI·거래량 평균회귀 v1

> 사전등록 커밋: `0d04fbd`, 지표 관례 확정: `fd8d55a`

| 사전등록 | 구현 |
| --- | --- |
| 계약·hash·development 경계 | `src/research/box_mean_reversion/contract.py` |
| OOS 게이트 로딩·완전 정규장 | `src/research/box_mean_reversion/loading.py` |
| EMA·RSI·ATR·median | `src/research/box_mean_reversion/indicators.py` |
| 신호·진입·청산·포트폴리오·비용·게이트 | `src/research/box_mean_reversion/development.py` |
| 결정론적 결과 문서 | `src/research/box_mean_reversion/report.py` |
| 공식 development CLI | `scripts/run_box_mean_reversion_development.py` |

성과 로더는 `research_load(... purpose="performance", track=TRACK_INTRADAY, market="US")`만
사용한다. 실행기는 OOS grant를 받지 않으며 결과 envelope에 `blind_oos_opened=false`를 기록한다.
