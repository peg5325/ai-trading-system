# 결과 — SPY 첫 30분 → 마지막 30분 장중 모멘텀

> 판정: **NO_SELECTION** · development-only · blind OOS 미개봉

## 핵심 결과

- 평가 거래일: 670일
- 거래: 662건 / 거래 발생일 662일
- 신호: LONG 393 / SHORT 269 / ZERO 0
- official 순손익: -229,428.385228
- official 거래당 기대값: -346.568558
- official PF: 0.024407
- official MDD: 2.294284
- 양수 fold: 0/10
- 실패 게이트: D3, D4, D5, D6, D7

## 비용별 결과

| 비용 | 누적 순손익 | 기대값 | PF | MDD | 양수 fold |
|---|---:|---:|---:|---:|---:|
| low | -97,408.793271 | -147.143192 | 0.150423 | 0.974088 | 0/10 |
| official | -229,428.385228 | -346.568558 | 0.024407 | 2.294284 | 0/10 |
| stress | -453,861.691557 | -685.591679 | 0.003993 | 4.538617 | 0/10 |

## LONG / SHORT 기여 (official)

| 방향 | 거래 | 누적 순손익 | 기대값 |
|---|---:|---:|---:|
| LONG | 393 | -137,497.226726 | -349.865717 |
| SHORT | 269 | -91,931.158502 | -341.751519 |

## 데이터 품질

- 불완전 SPY 거래일: 8 / 670 (1.1940%)
- 직전 거래일 일봉 종가 누락: 0 (0.0000%)
- 신규 충돌: 0
- 중대 결함: 0

## 해석 경계

- 결과 전에 커밋한 primary 한 개만 실행했다. 추가 필터·민감도 탐색은 하지 않았다.
- 논문의 역사적 성과나 비용을 재현한다는 뜻이 아니다. 프로젝트의 보수적 비용을 적용했다.
- SPY 단일 종목, 2023-05-18~2026-01-20 development 구간에만 해당한다.
- 2026-01-21~2026-07-21 blind OOS는 로드하지 않았다.
- Paper/실거래·주문·위험·config 변경은 없다.
- 결과 payload SHA-256: `eeb1285ccd4236faba7bfd51a4c7611342b81672a891c284a52bc17541302002`
- payload SHA-256은 해당 해시 필드를 넣기 전 canonical payload에 대해 계산했다.

## 실행 감사

- post_result_merge_review: ['full_daily_identity_scanned_blind_oos_ohlcv', 'last_complete_bar_exit_timestamp_used_start_label'] → ['tracked_full_receipts_plus_live_development_only_daily_hash', 'last_bar_start_label_plus_one_minute']; 성과 사전열람=True, 전략 규칙 변경=False
