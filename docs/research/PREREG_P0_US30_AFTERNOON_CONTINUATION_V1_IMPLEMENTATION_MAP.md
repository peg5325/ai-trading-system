# 구현 매핑 — 오전 강세·점심 눌림·오후 재상승 v1

> 사전등록 커밋: `0d0b295`

| 사전등록 | 구현 |
| --- | --- |
| 계약 해시·development 경계 | `src/research/afternoon_continuation/contract.py` |
| OOS 보호 로딩·완전 정규장 | `src/research/afternoon_continuation/loading.py` |
| EMA·ATR·session VWAP·median | `src/research/afternoon_continuation/indicators.py` |
| 오전 기준선·신호·청산·비용·게이트 | `src/research/afternoon_continuation/development.py` |
| 결과 문서 | `src/research/afternoon_continuation/report.py` |
| 공식 CLI | `scripts/run_afternoon_continuation_development.py` |

성과 로더는 `research_load(... purpose="performance", track=TRACK_INTRADAY, market="US")`만
사용하며 OOS unlock을 받지 않는다.
