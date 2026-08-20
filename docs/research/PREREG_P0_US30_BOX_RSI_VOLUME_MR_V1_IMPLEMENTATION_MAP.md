# 구현 매핑 — 박스권 RSI·거래량 평균회귀 v1

> 사전등록 커밋: `0d04fbd`, 지표 관례 확정: `fd8d55a`, ATR 기계 계약 보강: `d97bcd4`

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

## 결과 후 독립 리뷰 교정

PR #117 독립 리뷰에서 정본 5분봉 timestamp가 봉 **시작**을 가리키는데도 신호 허용 구간을
그 값에 직접 적용한 오류를 발견했다. 사전등록 §3.1의 “10:30~15:30 ET의 완료봉”을 실행하기
위해 `start_label + 5분`을 완료 시각으로 비교한다. 이는 임계값·신호 정의 변경이 아니라 기존
문면의 실행 교정이며, 성과 확인 뒤 발견됐으므로 결과를 재생성하고 `execution_audit`에 공개한다.
blind OOS는 계속 닫는다.
