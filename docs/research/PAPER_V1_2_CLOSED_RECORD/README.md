# premarket_freeze_long_intraday_v1 — v1.2 종료 기록

v1.2 Paper 평가 구간(예정 2026-10-01~10-14)은 첫 세션 2026-10-01 뒤에 사용자 결정(2026-10-02)으로
종료하고 v1.3 새 구간으로 넘어간다. 근거는 Charter §4.5다: 결과를 본 뒤 규칙을 바꾸면 새 버전과 새 검증 기간으로 평가한다.

v1.3 영수증으로 교체하면 러너가 공용 장부 `reports/premarket-freeze-paper-ledger.json`을 새 구간으로 다시 쓴다.
그래서 그 전에(2026-10-02 KST 오전) 운영 체크아웃 `reports/`의 원본을 그대로 복사해 보존한다.
`reports/`는 gitignore 대상이다.

| 파일 | SHA-256 |
|---|---|
| `premarket-freeze-paper-ledger.json` | `35235ce52b2103a76afc0240bbdf0af342050c72c7f9e94086b90e774cc40fc9` |
| `premarket-freeze-paper.20261001.json` | `bfcc42ba861f8d81ed2f0c75f7664aff7391dfd7320bfcf1944f340c30d1b5b5` |
| `premarket-freeze-paper.20261001.md` | `b262fa5aa1fdd5109dc1b414404ba23de2dda4e27729710c32311581703025b0` |
| `premarket-operation.20261001.paper.json` | `5d2c9a12df6651125b539456b36fcef31686f5f405f449cba7fc9fb4592ca93d` |

## 2026-10-01 요약

- `TRADED`, 평가액 2000.00 → 1995.284703, 대조 OK.
- MSFT 1주: 09:35 진입, 10:35 TIME_EXIT.
- AMZN 1주: 09:35 진입, 09:45 STOP_LOSS.
- SPY: `EXPOSURE_ROOM`으로 미진입.

이 결과는 v1.3 판정에 합산하지 않는다. v1.2 계약, 영수증, 코드는 git 기록에 남아 있다.
- 계약 JSON `e173e140…`, MD `1af71544…`.
- 영수증 artifact `810c5a03…`.
- 코드 `160bbf4`, 활성화 `ee372c7`.
