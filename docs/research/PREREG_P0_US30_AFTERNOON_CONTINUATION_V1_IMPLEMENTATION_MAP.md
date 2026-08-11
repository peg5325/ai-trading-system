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

## 결과 후 병합 검수 교정

병합 전 검수에서 정본 5분봉 timestamp가 시작 라벨인데도 §3.2의 “11:00~14:30 ET의 완료
5분봉” 구간을 시작 라벨에 직접 적용한 오류를 발견했다. 오전 창은 §3.1이 09:30~10:25
시작봉을 명시하므로 그대로 두고, 신호 창만 `start_label + 5분` 완료 시각으로 비교한다.
성과 열람 뒤 발견된 실행 교정이므로 전체 결과를 재생성하고 `execution_audit`에 공개한다.
